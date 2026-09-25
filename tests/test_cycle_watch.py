"""Listening for new tasks: what a trigger offers, and what it never does."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from cycle import memory, model, registry as registry_mod, watch


class Queue(object):
    """A loopback Jira whose search answers with whatever ``issues`` holds."""

    def __init__(self):
        self.issues = []
        self.queries = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):                                  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.queries.append(body["jql"])
                raw = json.dumps({"isLast": True, "issues": [
                    {"id": key, "key": key,
                     "fields": {"summary": title, "status": {"name": "In Progress"}}}
                    for key, title in outer.issues]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.site = "http://127.0.0.1:%d" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def queue():
    made = Queue()
    yield made
    made.close()


def cycle_for(site, **trigger):
    return model.parse_cycle({
        "id": "dev",
        "variables": {"site": site, "task_key": "", "token": {"secret": True}},
        "subject": {"kind": "task", "key": "${steps.todo.outputs.key}", "pin": "task_key"},
        "triggers": [dict({"id": "new_task", "watch": "todo"}, **trigger)],
        "steps": [{"id": "todo", "plugin": "jira.issues", "with": {
            "site": "${vars.site}", "email": "e@example.invalid", "token": "${vars.token}",
            "user": "currentUser()", "role": "assignee", "jql": 'status = "In Progress"',
            "issue": "${vars.task_key}", "limit": 1}}]}, "dev")


def test_the_first_look_only_remembers_what_is_there(queue, tmp_path):
    """Turning a listener on must not greet somebody with a question per task."""
    queue.issues = [("QA-1", "Old"), ("QA-2", "Older")]
    found = watch.check(cycle_for(queue.site), memory_path=str(tmp_path / "m.json"))
    assert found["first"] and found["new"] == []


def test_an_issue_that_appears_later_is_offered(queue, tmp_path):
    store = str(tmp_path / "m.json")
    cycle = cycle_for(queue.site)
    queue.issues = [("QA-1", "Old")]
    watch.check(cycle, memory_path=store)
    queue.issues = [("QA-1", "Old"), ("QA-2", "New one")]

    found = watch.check(cycle, memory_path=store)

    assert not found["first"]
    assert [(one["key"], one["title"]) for one in found["new"]] == [("QA-2", "New one")]
    assert found["pin"] == "task_key", "the subject's pin runs it"


def test_not_now_asks_again_and_seen_does_not(queue, tmp_path):
    store = str(tmp_path / "m.json")
    cycle = cycle_for(queue.site)
    watch.check(cycle, memory_path=store)
    queue.issues = [("QA-3", "Waiting")]
    assert [one["key"] for one in watch.check(cycle, memory_path=store)["new"]] == ["QA-3"]
    assert [one["key"] for one in watch.check(cycle, memory_path=store)["new"]] == ["QA-3"]

    watch.mark_seen(cycle, "QA-3", memory_path=store)

    assert watch.check(cycle, memory_path=store)["new"] == []
    assert "QA-3" in memory.recall("watch/dev/new_task", store)["seen"]


def test_it_asks_the_queue_not_one_pinned_issue(queue, tmp_path):
    """The step may be pinned through task_key; a listener pinned to one key
    would never see another."""
    cycle = cycle_for(queue.site)
    cycle.variables["task_key"] = "QA-9"
    watch.check(cycle, memory_path=str(tmp_path / "m.json"))
    assert queue.queries and "QA-9" not in queue.queries[0]
    assert 'status = "In Progress"' in queue.queries[0]


def test_the_step_s_secret_is_used_and_nothing_starts(queue, tmp_path, monkeypatch):
    from cycle import registry
    used = {}
    plugin = registry.get("jira.issues")
    real = plugin.peek
    monkeypatch.setattr(plugin, "peek", lambda settings: used.update(settings) or real(settings))
    monkeypatch.setattr(watch.secret_store, "get",
                        lambda cycle_id, name, path=None: "sekret" if name == "token" else "")
    watch.check(cycle_for(queue.site), memory_path=str(tmp_path / "m.json"))
    assert used["token"] == "sekret"


def test_jira_unreachable_is_a_watch_error(tmp_path):
    with pytest.raises(watch.WatchError):
        watch.check(cycle_for("http://127.0.0.1:1"), memory_path=str(tmp_path / "m.json"))


# ------------------------------------------------------------------ the file
def test_a_trigger_round_trips_through_the_cycle_file():
    cycle = cycle_for("http://x.invalid", every=600)
    document = model.to_document(cycle)
    assert document["triggers"] == [{"id": "new_task", "watch": "todo", "every": 600}]
    graph = model.to_graph(cycle)
    assert graph["triggers"] == [{"id": "new_task", "watch": "todo", "every": 600,
                                  "pin": "task_key", "enabled": True}]


def test_a_trigger_turned_off_keeps_its_schedule():
    """Off is written down, not the trigger deleted - turning it back on
    does not mean writing it again."""
    cycle = cycle_for("http://x.invalid", every=600, enabled=False)
    assert cycle.triggers[0].enabled is False
    assert model.to_document(cycle)["triggers"] == [
        {"id": "new_task", "watch": "todo", "every": 600, "enabled": False}]
    assert model.parse_cycle(model.to_document(cycle), "dev").triggers[0].every == 600


@pytest.mark.parametrize("trigger,message", [
    ({"watch": "nope"}, "not a step"),
    ({"watch": ""}, "watch is empty"),
    ({"every": 5}, "the least is 60"),
    ({"pin": "missing"}, "not one of the cycle's variables"),
    ({"poll": 3}, "unknown key 'poll'"),
])
def test_a_trigger_that_cannot_work_is_refused(trigger, message):
    raw = {"id": "dev", "variables": {"task_key": ""},
           "subject": {"kind": "task", "key": "k", "pin": "task_key"},
           "triggers": [dict({"id": "t", "watch": "todo"}, **trigger)],
           "steps": [{"id": "todo", "plugin": "jira.issues"}]}
    cycle = model.parse_cycle(raw, "dev")
    found = model.problems(cycle, raw=raw)
    assert any(message in one for one in found), found


def test_only_a_step_whose_plugin_says_so_can_be_watched():
    from cycle import registry
    raw = {"id": "dev", "variables": {"task_key": ""},
           "triggers": [{"id": "t", "watch": "sh", "pin": "task_key"}],
           "steps": [{"id": "sh", "plugin": "command.shell"}]}
    found = model.problems(model.parse_cycle(raw, "dev"), registry=registry, raw=raw)
    assert any("cannot be watched" in one for one in found), found


# ------------------------------------------------- the engine knows no queue
class Board(registry_mod.CyclePlugin):
    """A watchable plugin that is not Jira: the engine must not care."""
    metadata = registry_mod.PluginMetadata(id="test.board", name="Board", watchable=True)
    items = []

    def peek(self, settings):
        return list(self.items)


class Plugins(object):
    def __init__(self, **extra):
        self.extra = extra

    def get(self, name):
        return self.extra.get(name) or registry_mod.get(name)


def test_any_watchable_plugin_can_be_listened_to(tmp_path):
    cycle = model.parse_cycle({
        "id": "b", "variables": {"card": ""},
        "triggers": [{"id": "cards", "watch": "take", "pin": "card"}],
        "steps": [{"id": "take", "plugin": "test.board"}]}, "b")
    board = Board()
    plugins = Plugins(**{"test.board": board})
    store = str(tmp_path / "m.json")
    watch.check(cycle, memory_path=store, registry=plugins)
    board.items = [{"key": "CARD-1", "title": "A card"}, {"title": "no key, dropped"}]

    found = watch.check(cycle, memory_path=store, registry=plugins)

    assert [one["key"] for one in found["new"]] == ["CARD-1"]
    assert found["pin"] == "card"


def test_a_plugin_that_did_not_say_it_can_be_watched_is_refused(tmp_path):
    cycle = model.parse_cycle({
        "id": "b", "variables": {"card": ""},
        "triggers": [{"id": "t", "watch": "sh", "pin": "card"}],
        "steps": [{"id": "sh", "plugin": "command.shell"}]}, "b")
    with pytest.raises(watch.WatchError, match="cannot be watched"):
        watch.check(cycle, memory_path=str(tmp_path / "m.json"))


def test_what_the_plugin_raises_is_what_the_listener_says(tmp_path):
    class Broken(Board):
        def peek(self, settings):
            raise RuntimeError("board is down")
    cycle = model.parse_cycle({
        "id": "b", "variables": {"card": ""},
        "triggers": [{"id": "t", "watch": "take", "pin": "card"}],
        "steps": [{"id": "take", "plugin": "test.board"}]}, "b")
    with pytest.raises(watch.WatchError, match="board is down"):
        watch.check(cycle, memory_path=str(tmp_path / "m.json"),
                    registry=Plugins(**{"test.board": Broken()}))


def test_the_engine_does_not_import_a_plugin():
    import inspect
    source = inspect.getsource(watch)
    assert "cycle.plugins" not in source and "import jira" not in source


def test_jira_says_it_can_be_watched_in_describe():
    from cycle import registry
    published = {one["id"]: one for one in registry.describe()}
    assert published["jira.issues"]["watchable"] is True
    assert published["command.shell"]["watchable"] is False


# ------------------------------------------------------------------ planned
def test_a_planned_task_is_not_offered_and_waits_on_the_plan(queue, tmp_path):
    store = str(tmp_path / "m.json")
    cycle = cycle_for(queue.site)
    watch.check(cycle, memory_path=store)
    queue.issues = [("QA-4", "Later please")]
    assert [one["key"] for one in watch.check(cycle, memory_path=store)["new"]] == ["QA-4"]

    watch.plan(cycle, "QA-4", "Later please", "https://x.invalid/QA-4",
               memory_path=store, when=100.0)

    assert watch.check(cycle, memory_path=store)["new"] == []
    assert watch.planned(store) == [{"key": "QA-4", "title": "Later please",
                                     "url": "https://x.invalid/QA-4", "at": 100.0,
                                     "cycle": "dev", "trigger": "new_task"}]


def test_planning_twice_keeps_when_it_was_first_planned(tmp_path):
    store = str(tmp_path / "m.json")
    cycle = cycle_for("http://x.invalid")
    watch.plan(cycle, "QA-4", "t", memory_path=store, when=100.0)
    watch.plan(cycle, "QA-4", "t", memory_path=store, when=200.0)
    assert [one["at"] for one in watch.planned(store)] == [100.0]


def test_starting_or_removing_takes_it_off_the_plan_for_good(queue, tmp_path):
    store = str(tmp_path / "m.json")
    cycle = cycle_for(queue.site)
    watch.check(cycle, memory_path=store)
    queue.issues = [("QA-4", "t")]
    watch.plan(cycle, "QA-4", "t", memory_path=store)

    watch.mark_seen(cycle, "QA-4", memory_path=store)

    assert watch.planned(store) == []
    assert watch.check(cycle, memory_path=store)["new"] == []


def test_the_plan_is_read_across_cycles_oldest_first(tmp_path):
    store = str(tmp_path / "m.json")
    one, two = cycle_for("http://x.invalid"), cycle_for("http://x.invalid")
    two.id = "ops"
    watch.plan(two, "OPS-1", "second", memory_path=store, when=20.0)
    watch.plan(one, "QA-1", "first", memory_path=store, when=10.0)
    assert [(p["cycle"], p["key"]) for p in watch.planned(store)] == [
        ("dev", "QA-1"), ("ops", "OPS-1")]


# ------------------------------------------------ approvals nobody answered
def test_an_unanswered_approval_goes_on_the_plan_and_off_it(tmp_path):
    from cycle import planner

    store = str(tmp_path / "m.json")
    planner.add_approval("dev", "20260924-run", "approve", "Start work on QA-4?",
                         subject={"key": "QA-4", "title": "Discounts"},
                         memory_path=store, when=50.0)
    cycle = cycle_for("http://x.invalid")
    watch.plan(cycle, "QA-9", "later", memory_path=store, when=10.0)

    found = planner.items(store)
    assert [(one["kind"], one["id"]) for one in found] == [
        ("task", "task:dev:new_task:QA-9"),
        ("approval", "approval:20260924-run:approve")]
    assert found[1]["key"] == "QA-4" and found[1]["run"] == "20260924-run"

    assert planner.remove("approval:20260924-run:approve", store)
    assert [one["kind"] for one in planner.items(store)] == ["task"]


def test_the_gate_plans_a_silence_but_not_a_refusal_or_a_stop(tmp_path, monkeypatch):
    from cycle import ask, planner, registry
    from cycle.context import RunContext
    from domain.cycle import CycleRun, CycleStep

    store = str(tmp_path / "m.json")
    gate = registry.get("approval.gate")

    def run(answer, cancel_reason=None):
        context = RunContext(CycleRun(id="r1", cycle_id="dev", subject={"key": "QA-4"}),
                             None, str(tmp_path), memory_path=store)

        def asked(*_a, **_k):
            # Stopped - or timed out - while it waited for an answer.
            if cancel_reason:
                context.cancel.set(cancel_reason)
            return answer

        monkeypatch.setattr(ask, "ask", asked)
        return gate.execute(context, CycleStep(id="approve", plugin="approval.gate",
                                               settings={"question": "Go?"}))

    assert not run({"answered": True, "answer": "Leave it"}).ok
    assert planner.items(store) == [], "a refusal is an answer"
    assert not run({"answered": False, "message": "stopped"}, "stopped by you").ok
    assert planner.items(store) == [], "a stop is somebody's decision"
    result = run({"answered": False, "message": "nobody answered"},
                 "timed out after 1800s")
    assert not result.ok, "not answered is still not approved"
    assert [one["key"] for one in planner.items(store)] == ["QA-4"]
    planner.remove(planner.items(store)[0]["id"], store)
    run({"answered": False, "message": "No application is attached"})
    assert len(planner.items(store)) == 1, "no window to ask in is a silence too"


def test_a_trigger_survives_every_save_the_application_makes(tmp_path):
    """The file writer knew a fixed list of top-level keys and triggers were
    not on it, so saving a step's dialog - even to turn listening off - wrote
    the cycle back without them."""
    from cycle import cyclefile, loader

    for enabled in (True, False):
        cycle = cycle_for("http://x.invalid", every=120, enabled=enabled)
        text = cyclefile.render(model.to_document(cycle))
        (tmp_path / "dev.yaml").write_text(text)
        back = loader.load_cycle("dev", str(tmp_path))
        assert [(one.id, one.watch, one.every, one.enabled) for one in back.triggers] == [
            ("new_task", "todo", 120, enabled)], text
