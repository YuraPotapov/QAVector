"""A person talking to a running cycle: kept, routed, delivered - or said not to be.

No agent and no GUI anywhere here. The plugin that listens is a test plugin
with no idea what an agent is, which is the point: whatever an agent step does
with a message, it gets it through exactly the API this one uses.

The tests that need a message to arrive *while* a step runs have the step send
it itself, through the same entry point the control channel uses, and wait for
the executor to judge it. That makes "sent during the step" a fact of the test
rather than a race with a sleep in it.
"""

import json
import os
import subprocess
import sys
import threading
import time

import pytest

from cycle import bus, executor, interactions, model, registry
from cycle.interactions import (DELIVERED, PENDING, REJECTED, UNCERTAIN,
                                UNDELIVERED, RunInteractions, parse_request)
from domain.cycle import RUNNING, SUCCESS, CycleRun, StepRun

RUN = "r1"


class Listener(registry.CyclePlugin):
    """Takes messages at the points its script names, and writes down what it read."""

    def __init__(self, script, accepts=True, plugin_id="test.listener", recoverable=False):
        self.metadata = registry.PluginMetadata(
            id=plugin_id, name="Listener", recoverable=recoverable,
            accepts=(registry.accepts("text", "Note", when="Before its next turn"),)
            if accepts else ())
        self.script = script
        self.read = {}
        self.runs = 0

    def execute(self, context, step):
        self.runs += 1
        inbox = context.inbox(step.id)

        def point(name, deliver=True, last=False):
            snapshot = inbox.take(name, last=last)
            self.read[(step.id, name)] = [one["text"] for one in snapshot.items]
            if deliver:
                inbox.delivered(snapshot, "")
            return snapshot

        self.script(context, step, point)
        return registry.succeeded()


class Plugins:
    def __init__(self, *plugins):
        self.plugins = {one.metadata.id: one for one in plugins}

    def get(self, name):
        return self.plugins.get(name)


class Watch(bus.Recorder):
    """A recorder that can be waited on for a message to reach a state."""

    def __init__(self):
        super().__init__()
        self._changed = threading.Condition()

    def interaction(self, message):
        super().interaction(message)
        with self._changed:
            self._changed.notify_all()

    def interaction_refused(self, message_id, reason):
        super().interaction_refused(message_id, reason)
        with self._changed:
            self._changed.notify_all()

    def latest(self, message_id):
        found = [one for one in self.of("interaction") if one.get("id") == message_id]
        return found[-1] if found else None

    def state(self, message_id):
        return (self.latest(message_id) or {}).get("state")

    def wait_for(self, message_id, timeout=10):
        """Until the run has judged this message, one way or the other."""
        deadline = time.monotonic() + timeout
        with self._changed:
            while self.latest(message_id) is None and not self.refused(message_id):
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._changed.wait(left)
        return self.latest(message_id)

    def refused(self, message_id):
        return [one["reason"] for one in self.of("interaction_refused")
                if one["id"] == message_id]


def cycle_of(*steps):
    return model.parse_cycle({"id": "demo", "steps": list(steps)}, "demo")


def step(step_id, plugin="test.listener", **extra):
    return dict(id=step_id, plugin=plugin, **extra)


def say(message_id, text="Also called from cron", scope="step", step_id="a", **extra):
    request = {"id": message_id, "run_id": RUN, "scope": scope, "step": step_id,
               "kind": "text", "payload": {"text": text}, "who": "tester"}
    request.update(extra)
    return executor.submit_interaction(request)


def go(cycle, plugins, workspace, watch, **extra):
    return executor.run_cycle(cycle, str(workspace), registry=plugins, observer=watch,
                              run_id=RUN, **extra)


def saved(workspace):
    """Messages as the run's own file holds them - the truth, not the events."""
    path = os.path.join(str(workspace), ".execution", interactions.FILE)
    with open(path, encoding="utf-8") as handle:
        return {one["id"]: one for one in json.load(handle)["messages"]}


# ------------------------------------------------------------------ the format
def test_a_request_must_have_the_shape_of_one():
    good = {"id": "m-1", "run_id": RUN, "scope": "step", "step": "a",
            "kind": "text", "payload": {"text": "x"}}
    assert parse_request(good)[1] == ""
    for broken, words in (
            (dict(good, id="has space"), "id"),
            (dict(good, run_id=""), "run"),
            (dict(good, scope="everyone"), "scope"),
            (dict(good, step=""), "name the step"),
            (dict(good, execution=0), "execution"),
            (dict(good, kind="shell"), "kind"),
            (dict(good, payload="x"), "payload"),
            ("not an object", "JSON object")):
        request, problem = parse_request(broken)
        assert request is None and words in problem


def test_a_message_to_the_run_names_no_step_whatever_it_said():
    request, _ = parse_request({"id": "m", "run_id": RUN, "scope": "run",
                                "step": "a", "execution": 2, "payload": {"text": "x"}})
    assert request["step"] == "" and request["execution"] is None


# --------------------------------------------------------- the store, by itself
@pytest.fixture
def store(tmp_path):
    """A store over a run where step a is going and step b has yet to start."""
    plugin = Listener(lambda *_: None)
    plain = Listener(lambda *_: None, accepts=False, plugin_id="test.plain")
    cycle = cycle_of(step("a"), step("b", needs=["a"]), step("p", plugin="test.plain"),
                     step("done"))
    run = CycleRun(id=RUN, cycle_id="demo", steps={
        "a": StepRun("a", plugin="test.listener", status=RUNNING),
        "b": StepRun("b", plugin="test.listener"),
        "p": StepRun("p", plugin="test.plain"),
        "done": StepRun("done", plugin="test.listener", status=SUCCESS)})
    watch = Watch()
    found = RunInteractions(str(tmp_path / ".execution"), RUN, watch, str(tmp_path))
    found.open(run, cycle, Plugins(plugin, plain))
    found.begin("a", 1, plugin)
    found.watch, found.run, found.cycle, found.plugins = watch, run, cycle, Plugins(plugin, plain)
    return found


def request(message_id, text="Also called from cron", **extra):
    raw = {"id": message_id, "run_id": RUN, "scope": "step", "step": "a",
           "kind": "text", "payload": {"text": text}}
    raw.update(extra)
    parsed, problem = parse_request(raw)
    assert not problem
    return parsed


def test_a_message_is_saved_before_it_is_announced(store, tmp_path):
    seen_on_disk = []
    store.watch.interaction = lambda message: seen_on_disk.append(
        message["id"] in saved(tmp_path))
    record, refusal = store.accept(request("m1"))
    assert refusal == "" and record["state"] == PENDING
    assert seen_on_disk == [True]


def test_sending_the_same_message_again_is_the_same_message(store):
    first, _ = store.accept(request("m1"))
    again, refusal = store.accept(request("m1"))
    assert refusal == "" and again["seq"] == first["seq"]
    assert [one["id"] for one in store.messages()] == ["m1"]


def test_the_same_id_with_other_words_is_refused_and_the_first_stands(store):
    store.accept(request("m1"))
    record, refusal = store.accept(request("m1", text="something else"))
    assert record is None and "different content" in refusal
    assert store.messages()[0]["payload"]["text"] == "Also called from cron"
    assert store.watch.refused("m1") == [refusal]


@pytest.mark.parametrize("extra, words", [
    ({"step": "ghost"}, "ghost is not a step of this run"),
    ({"step": "done"}, "done has already finished (success)"),
    ({"step": "p"}, "p (test.plain) does not take messages"),
    ({"kind": "answer"}, "only text is"),
    ({"execution": 2}, "has been sent round again since"),
    ({"payload": {"text": "   "}}, "needs some text"),
    ({"payload": {"text": "x" * (interactions.MAX_TEXT + 1)}}, "at most"),
])
def test_a_request_that_cannot_go_is_recorded_as_refused_with_the_reason(store, extra, words):
    record, refusal = store.accept(request("m1", **extra))
    assert words in refusal
    assert record["state"] == REJECTED and record["reason"] == refusal


def test_a_refusal_made_on_an_out_of_date_list_says_so(store):
    record, refusal = store.accept(request("m1", step="done", offer=0))
    assert "out of date" in refusal


def test_a_step_yet_to_start_can_be_written_to(store):
    record, refusal = store.accept(request("m1", step="b"))
    assert refusal == "" and record["execution"] == 1
    assert store.offers()["steps"]["b"]["when"] == interactions.WHEN_PENDING


def test_a_snapshot_never_changes_and_what_came_after_goes_to_the_next_point(store):
    store.accept(request("m1", text="first"))
    one = store.take("a", "one")
    store.accept(request("m2", text="second"))
    assert [item["text"] for item in store.take("a", "one").items] == ["first"]
    assert [item["text"] for item in one.items] == ["first"]
    assert [item["text"] for item in store.take("a", "two").items] == ["first", "second"]


def test_an_empty_point_is_recorded_too_so_it_stays_empty(store):
    assert not store.take("a", "one")
    store.accept(request("m1"))
    assert not store.take("a", "one")
    assert [one["key"] for one in store.snapshots()] == ["a#1/one"]


def test_whether_a_message_falls_before_or_after_a_snapshot_is_never_luck(store):
    """Messages pour in while points are taken; every snapshot holds exactly
    what had been accepted when it was taken, and nothing after."""
    stop = threading.Event()

    def sending():
        for number in range(60):
            store.accept(request("m%d" % number, text="n%d" % number))
        stop.set()

    sender = threading.Thread(target=sending)
    sender.start()
    points = 0
    while not stop.is_set() or points < 5:
        store.take("a", "p%d" % points)
        points += 1
    sender.join()
    seqs = {one["id"]: one["seq"] for one in store.messages()}
    for snapshot in store.snapshots():
        expected = sorted((name for name, seq in seqs.items() if seq <= snapshot["upto"]),
                          key=seqs.get)
        assert snapshot["items"] == expected


def test_a_step_message_reaches_only_its_own_step(store):
    store.accept(request("to-a"))
    store.begin("b", 1, store.plugins.get("test.listener"))
    assert [item["id"] for item in store.take("b", "one").items] == []
    assert [item["id"] for item in store.take("a", "one").items] == ["to-a"]


def test_a_run_message_is_a_delivery_of_its_own_for_every_step(store):
    store.accept(request("all", scope="run"))
    store.begin("b", 1, store.plugins.get("test.listener"))
    for name in ("a", "b"):
        store.delivered(store.take(name, "one"), "")
    record = store.messages()[0]
    assert record["state"] == DELIVERED
    assert sorted(one["step"] for one in record["deliveries"]) == ["a", "b"]


def test_confirming_twice_is_confirming_once(store):
    store.accept(request("m1"))
    snapshot = store.take("a", "one")
    store.delivered(snapshot, "")
    store.delivered(snapshot, "")
    assert len(store.messages()[0]["deliveries"]) == 1


def test_a_later_point_still_holds_what_an_earlier_one_was_given(store):
    """An agent call starts from nothing, so the second must be told again."""
    store.accept(request("m1"))
    store.delivered(store.take("a", "one"), "")
    two = store.take("a", "two")
    assert [(item["id"], item["new"]) for item in two.items] == [("m1", False)]


def test_a_step_that_ends_first_says_the_message_never_arrived(store):
    store.accept(request("m1"))
    store.finish("a", SUCCESS)
    record = store.messages()[0]
    assert record["state"] == UNDELIVERED
    assert "a ended (success) before it reached another point" in record["reason"]
    assert "a" not in store.offers()["steps"]


def test_a_step_that_was_skipped_says_so(store):
    store.accept(request("m1", step="b"))
    store.finish("b", "skipped", started=False)
    assert store.messages()[0]["reason"] == "b was skipped, so it never read it."


def test_the_last_point_withdraws_the_offer_and_a_later_message_is_refused(store):
    store.take("a", "only", last=True)
    record, refusal = store.accept(request("m1"))
    assert "no point left" in refusal and record["state"] == REJECTED


def test_a_call_that_never_began_gives_its_messages_back(store):
    store.accept(request("m1"))
    snapshot = store.take("a", "one")
    store.bind(snapshot, "steps/a/operations/x")
    store.release(snapshot, "never began")
    record = store.messages()[0]
    assert record["state"] == PENDING and record["deliveries"] == []


def test_a_point_reached_before_messages_were_recorded_reads_nothing(store):
    store.accept(request("m1"))
    assert not store.take("a", "one", reached=True)
    assert store.messages()[0]["state"] == PENDING


def test_closing_the_run_closes_what_has_not_arrived(store):
    store.accept(request("to-a"))
    store.accept(request("all", scope="run"))
    store.close("The run was stopped before it was read.")
    states = {one["id"]: (one["state"], one["reason"]) for one in store.messages()}
    assert states["to-a"] == (UNDELIVERED, "The run was stopped before it was read.")
    assert states["all"] == (UNDELIVERED, "No step read it before the run ended.")
    assert store.offers()["steps"] == {} and store.offers()["run"] is None
    record, refusal = store.accept(request("late"))
    assert record is None and "no longer takes messages" in refusal


def test_a_delivered_run_message_stays_part_of_the_run_after_it_stops(store, tmp_path):
    store.accept(request("all", scope="run"))
    store.delivered(store.take("a", "one"), "")
    store.close("stopped")
    again = RunInteractions(str(tmp_path / ".execution"), RUN, None, str(tmp_path))
    again.load()
    store.run.steps["b"].status = "pending"
    again.open(store.run, store.cycle, store.plugins)
    again.begin("b", 1, store.plugins.get("test.listener"))
    assert [item["id"] for item in again.take("b", "one").items] == ["all"]


@pytest.mark.parametrize("receipt, state", [
    ({"state": "complete", "ok": True}, DELIVERED),
    ({"state": "started"}, UNCERTAIN),
    (None, PENDING),
])
def test_a_delivery_cut_off_by_a_crash_is_settled_by_its_call_s_receipt(
        store, tmp_path, receipt, state):
    store.accept(request("m1"))
    snapshot = store.take("a", "one")
    store.bind(snapshot, "steps/a/operations/key")
    if receipt is not None:
        os.makedirs(str(tmp_path / "steps" / "a" / "operations" / "key"))
        with open(str(tmp_path / "steps" / "a" / "operations" / "key" / "receipt.json"),
                  "w") as handle:
            json.dump(receipt, handle)
    # The process dies here: nothing is closed or sealed.
    again = RunInteractions(str(tmp_path / ".execution"), RUN, None, str(tmp_path))
    again.load()
    assert again.messages()[0]["state"] == state


def test_a_run_from_before_messages_existed_has_an_empty_history(tmp_path):
    found = RunInteractions(str(tmp_path / ".execution"), RUN)
    found.load()
    assert found.messages() == []


def test_a_history_from_a_newer_version_is_refused_rather_than_guessed_at(tmp_path):
    os.makedirs(str(tmp_path / ".execution"))
    with open(str(tmp_path / ".execution" / interactions.FILE), "w") as handle:
        json.dump({"schema": interactions.SCHEMA + 1, "messages": []}, handle)
    from cycle.checkpoints import CheckpointError
    with pytest.raises(CheckpointError, match="newer version"):
        RunInteractions(str(tmp_path / ".execution"), RUN).load()


def test_outside_a_run_an_inbox_reads_nothing_and_confirms_nothing():
    from cycle.context import RunContext
    inbox = RunContext(None, None, "/nowhere").inbox("a")
    snapshot = inbox.take("one")
    assert not snapshot
    inbox.bind(snapshot, "x")
    inbox.delivered(snapshot)


# ------------------------------------------------------------ through the run
def test_a_message_sent_while_a_step_runs_is_read_at_its_next_point(tmp_path):
    watch = Watch()

    def script(context, step_, point):
        point("one")
        assert say("m1") == ""
        assert watch.wait_for("m1")["state"] == PENDING
        point("two")

    plugin = Listener(script)
    run = go(cycle_of(step("a")), Plugins(plugin), tmp_path, watch)
    assert run.status == SUCCESS
    assert plugin.read == {("a", "one"): [], ("a", "two"): ["Also called from cron"]}
    record = saved(tmp_path)["m1"]
    assert record["state"] == DELIVERED
    assert [(one["step"], one["execution"], one["point"], one["state"])
            for one in record["deliveries"]] == [("a", 1, "two", DELIVERED)]
    assert watch.state("m1") == DELIVERED


def test_with_two_steps_running_only_the_addressed_one_hears(tmp_path):
    watch = Watch()
    both = threading.Barrier(2, timeout=10)

    def script(context, step_, point):
        point("one")
        both.wait()
        if step_.id == "a":
            assert say("to-b", step_id="b", text="only for b") == ""
            assert say("all", scope="run", step_id="", text="for everyone") == ""
            assert watch.wait_for("to-b") and watch.wait_for("all")
        both.wait()
        point("two")

    plugin = Listener(script)
    go(cycle_of(step("a"), step("b")), Plugins(plugin), tmp_path, watch, jobs=2)
    assert plugin.read[("a", "two")] == ["for everyone"]
    assert plugin.read[("b", "two")] == ["only for b", "for everyone"]
    everyone = saved(tmp_path)["all"]
    assert sorted(one["step"] for one in everyone["deliveries"]) == ["a", "b"]


def test_a_step_yet_to_start_reads_its_message_when_it_starts(tmp_path):
    watch = Watch()

    def script(context, step_, point):
        if step_.id == "a":
            assert say("for-b", step_id="b", text="when you start") == ""
            assert watch.wait_for("for-b")["state"] == PENDING
        point("start")

    plugin = Listener(script)
    go(cycle_of(step("a"), step("b", needs=["a"])), Plugins(plugin), tmp_path, watch)
    assert plugin.read[("b", "start")] == ["when you start"]
    assert saved(tmp_path)["for-b"]["state"] == DELIVERED


def test_with_no_point_left_the_message_says_so_and_nothing_runs_again(tmp_path):
    watch = Watch()

    def script(context, step_, point):
        point("only")
        assert say("m1") == ""
        assert watch.wait_for("m1")["state"] == PENDING

    plugin = Listener(script)
    run = go(cycle_of(step("a")), Plugins(plugin), tmp_path, watch)
    assert run.status == SUCCESS and plugin.runs == 1
    record = saved(tmp_path)["m1"]
    assert record["state"] == UNDELIVERED
    assert "a ended (success)" in record["reason"]


def test_stopping_the_run_closes_what_has_not_arrived(tmp_path):
    watch = Watch()

    def script(context, step_, point):
        point("one")
        assert say("m1") == ""
        assert watch.wait_for("m1")["state"] == PENDING
        executor.request_stop(RUN)
        context.cancel.wait(10)
        context.cancel.raise_if_set()

    run = go(cycle_of(step("a")), Plugins(Listener(script)), tmp_path, watch)
    assert run.status == "cancelled"
    record = saved(tmp_path)["m1"]
    assert (record["state"], record["reason"]) == (
        UNDELIVERED, "The run was stopped before it was read.")


def test_a_run_judges_what_it_is_sent_and_says_why_not(tmp_path):
    watch = Watch()
    plain = Listener(lambda *_: None, accepts=False, plugin_id="test.plain")

    def script(context, step_, point):
        if step_.id != "a":
            return
        point("one")
        for message_id, extra in (("ghost", {"step_id": "ghost"}),
                                  ("done", {"step_id": "first"}),
                                  ("plain", {"step_id": "p"}),
                                  ("answer", {"kind": "answer"}),
                                  ("stale", {"execution": 2})):
            assert say(message_id, **extra) == ""
            assert watch.wait_for(message_id)["state"] == REJECTED
        assert say("twice") == "" and say("twice") == ""
        assert say("twice", text="other words") == ""
        watch.wait_for("twice")
        deadline = time.monotonic() + 10
        while not watch.refused("twice") and time.monotonic() < deadline:
            time.sleep(0.01)

    go(cycle_of(step("first"), step("a", needs=["first"]),
                step("p", plugin="test.plain", needs=["a"])),
       Plugins(Listener(script), plain), tmp_path, watch)
    reasons = {name: record["reason"] for name, record in saved(tmp_path).items()}
    assert "ghost is not a step" in reasons["ghost"]
    assert "first has already finished" in reasons["done"]
    assert "does not take messages" in reasons["plain"]
    assert "only text" in reasons["answer"]
    assert "sent round again" in reasons["stale"]
    assert list(saved(tmp_path)).count("twice") == 1
    assert "different content" in watch.refused("twice")[0]


def test_a_message_for_a_run_that_is_not_going_is_refused_at_once():
    assert say("m1") == "No run %s is going here." % RUN
    assert "run it is for" in executor.submit_interaction(
        {"id": "m1", "payload": {"text": "x"}})


def test_the_run_says_who_can_be_told_what_as_it_goes(tmp_path):
    watch = Watch()
    plain = Listener(lambda *_: None, accepts=False, plugin_id="test.plain")
    tables = []

    def script(context, step_, point):
        tables.append(context.interactions.offers())

    go(cycle_of(step("a"), step("p", plugin="test.plain", needs=["a"])),
       Plugins(Listener(script), plain), tmp_path, watch)
    offers = watch.of("interaction_offers")
    assert offers[0]["steps"]["a"]["state"] == "pending"
    assert tables[0]["steps"]["a"]["state"] == "running"
    assert tables[0]["steps"]["a"]["when"] == "Before its next turn"
    assert "p" not in tables[0]["steps"] and tables[0]["run"] is not None
    assert offers[-1]["steps"] == {} and offers[-1]["run"] is None


def test_a_step_sent_round_again_by_a_revision_is_a_new_execution(tmp_path):
    run = CycleRun(id=RUN, cycle_id="demo", revisions=[{"steps": ["a"]}, {"steps": ["b"]},
                                                         {"steps": ["a", "b"]}])
    assert interactions.execution_of(run, "a") == 3
    assert interactions.execution_of(run, "c") == 1


# ------------------------------------------------------------- after a crash
CRASH = r'''
import os, sys, time
sys.path.insert(0, %(root)r)
from cycle import executor, model, registry
from cycle.operations import Operation

WORKSPACE = %(workspace)r

class Listener(registry.CyclePlugin):
    metadata = registry.PluginMetadata(
        id="test.listener", name="Listener", recoverable=True,
        accepts=(registry.accepts("text"),))

    def execute(self, context, step):
        inbox = context.inbox(step.id)
        inbox.delivered(inbox.take("one"))
        call = Operation(context, step, "probe", {"n": 1})
        call.begin()
        call.complete({"paid": True}, True)
        executor.submit_interaction({
            "id": "m1", "run_id": "r1", "scope": "step", "step": "a",
            "kind": "text", "payload": {"text": "Also called from cron"}})
        path = os.path.join(WORKSPACE, ".execution", "interactions.json")
        while '"m1"' not in open(path).read():
            time.sleep(0.02)
        open(os.path.join(WORKSPACE, "ready"), "w").write("1")
        time.sleep(600)

class Plugins:
    def get(self, name):
        return Listener() if name == "test.listener" else None

cycle = model.parse_cycle({"id": "demo", "steps": [
    {"id": "a", "plugin": "test.listener"}]}, "demo")
executor.run_cycle(cycle, WORKSPACE, registry=Plugins(), run_id="r1")
'''


def test_after_a_crash_resume_keeps_the_message_and_delivers_it_once(tmp_path):
    """Killed outright - no close, no seal - after the message was accepted.
    Resume must neither lose it nor change what the first point already read."""
    workspace = str(tmp_path / "run")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    child = subprocess.Popen([sys.executable, "-c", CRASH % {"root": root,
                                                             "workspace": workspace}])
    try:
        deadline = time.monotonic() + 60
        while not os.path.exists(os.path.join(workspace, "ready")):
            assert child.poll() is None, "the run ended before it was killed"
            assert time.monotonic() < deadline, "the run never got as far as the message"
            time.sleep(0.05)
    finally:
        child.kill()
        child.wait()
    assert saved(workspace)["m1"]["state"] == PENDING

    watch = Watch()
    paid = []

    def script(context, step_, point):
        point("one")
        call = __import__("cycle.operations", fromlist=["Operation"]).Operation(
            context, step_, "probe", {"n": 1})
        paid.append(call.cached() is None)
        # The same message again, as a sender that never heard back would.
        assert say("m1") == ""
        watch.wait_for("m1")
        point("two")

    plugin = Listener(script, recoverable=True)
    run = executor.run_cycle(cycle_of(step("a")), workspace, registry=Plugins(plugin),
                             observer=watch, resume=True)
    assert run.status == SUCCESS
    assert paid == [False]
    assert plugin.read[("a", "one")] == []
    assert plugin.read[("a", "two")] == ["Also called from cron"]
    messages = saved(workspace)
    assert list(messages) == ["m1"]
    assert [one["point"] for one in messages["m1"]["deliveries"]] == ["two"]


# ---------------------------------------------------- durability and recovery
@pytest.mark.parametrize("change", ["bind", "delivered", "release", "finish", "close", "seal"])
def test_a_failed_write_never_publishes_or_keeps_an_unsaved_delivery(
        store, tmp_path, monkeypatch, change):
    from cycle.checkpoints import CheckpointError

    store.accept(request("m1"))
    snapshot = store.take("a", "one")
    if change in ("release", "seal"):
        store.bind(snapshot, "steps/a/operations/key")
    before = store.messages(), store.snapshots()
    announced = len(store.watch.of("interaction"))

    def failed_save():
        raise CheckpointError("Cannot save execution state: disk full")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_save", failed_save)
        with pytest.raises(CheckpointError, match="disk full"):
            if change == "bind":
                store.bind(snapshot, "steps/a/operations/key")
            elif change == "delivered":
                store.delivered(snapshot, "steps/a/operations/key")
            elif change == "release":
                store.release(snapshot)
            elif change == "finish":
                store.finish("a", SUCCESS)
            elif change == "close":
                store.close("stopped")
            else:
                store.seal()
    assert (store.messages(), store.snapshots()) == before
    assert len(store.watch.of("interaction")) == announced
    assert list(saved(tmp_path).values()) == before[0]
    # Retrying a delivery after the disk recovers must persist it, not skip it
    # because an earlier failed write left a delivered flag in memory.
    if change == "delivered":
        store.delivered(snapshot, "steps/a/operations/key")
        assert saved(tmp_path)["m1"]["state"] == DELIVERED


def test_the_snapshot_new_flags_do_not_change_when_later_points_are_delivered(store, tmp_path):
    store.accept(request("m1"))
    original = store.take("a", "one")
    store.delivered(store.take("a", "two"))
    assert original.items[0]["new"] is True
    assert store.take("a", "one").items == original.items

    again = RunInteractions(str(tmp_path / ".execution"), RUN)
    again.load()
    again.open(store.run, store.cycle, store.plugins)
    again.begin("a", 1, store.plugins.get("test.listener"))
    assert again.take("a", "one").items == original.items


def test_uncertainty_for_one_consumer_does_not_drop_guidance_for_the_next(store):
    store.accept(request("all", scope="run"))
    store.bind(store.take("a", "one"), "steps/a/operations/key")
    store.finish("a", "failed")
    assert store.messages()[0]["state"] == UNCERTAIN
    store.begin("b", 1, store.plugins.get("test.listener"))
    snapshot = store.take("b", "one")
    assert [one["id"] for one in snapshot.items] == ["all"]
    store.delivered(snapshot)
    assert [one["state"] for one in store.messages()[0]["deliveries"]] == [
        UNCERTAIN, DELIVERED]


def test_run_guidance_records_a_consumer_that_finished_before_reading_it(store):
    store.accept(request("all", scope="run"))
    store.delivered(store.take("a", "one"))
    store.finish("a", SUCCESS)
    store.finish("b", "skipped", started=False)
    record = store.messages()[0]
    assert record["state"] == DELIVERED
    missed = next(one for one in record["deliveries"] if one["step"] == "b")
    assert missed["state"] == UNDELIVERED and "skipped" in missed["reason"]


def test_a_plugin_without_text_capability_cannot_read_run_guidance(store):
    store.accept(request("all", scope="run"))
    store.begin("p", 1, store.plugins.get("test.plain"))
    assert not store.take("p", "one")


def test_a_revised_execution_can_read_new_messages_despite_a_legacy_call(store):
    store.take("a", "one")
    store.finish("a", SUCCESS)
    store.run.revisions.append({"steps": ["a"]})
    store.run.steps["a"].status = "pending"
    store.reoffer(["a"])
    store.begin("a", 2, store.plugins.get("test.listener"))
    store.accept(request("m1", execution=2))
    assert [one["id"] for one in store.take("a", "one", reached=True).items] == ["m1"]


@pytest.mark.parametrize("receipt", ["broken JSON", "[]", '{"state": "unknown"}'])
def test_an_unreadable_receipt_leaves_delivery_uncertain(store, tmp_path, receipt):
    store.accept(request("m1"))
    store.bind(store.take("a", "one"), "steps/a/operations/key")
    path = tmp_path / "steps" / "a" / "operations" / "key" / "receipt.json"
    path.parent.mkdir(parents=True)
    path.write_text(receipt)
    again = RunInteractions(str(tmp_path / ".execution"), RUN)
    again.load()
    assert again.messages()[0]["state"] == UNCERTAIN


def test_a_saved_call_that_never_started_does_not_acknowledge_delivery(store, tmp_path):
    store.accept(request("m1"))
    store.bind(store.take("a", "one"), "steps/a/operations/key")
    path = tmp_path / "steps" / "a" / "operations" / "key" / "receipt.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"state": "complete", "ok": False,
                               "input_delivered": False}))
    again = RunInteractions(str(tmp_path / ".execution"), RUN)
    again.load()
    assert again.messages()[0]["state"] == PENDING
    assert again.messages()[0]["deliveries"] == []


def test_loading_another_runs_messages_is_refused(store, tmp_path):
    store.accept(request("m1"))
    from cycle.checkpoints import CheckpointError
    with pytest.raises(CheckpointError, match="different run"):
        RunInteractions(str(tmp_path / ".execution"), "other").load()


@pytest.mark.parametrize("damage", ["schema", "duplicate_message", "missing_message"])
def test_damaged_message_history_is_not_silently_reinterpreted(store, tmp_path, damage):
    store.accept(request("m1"))
    store.take("a", "one")
    path = tmp_path / ".execution" / interactions.FILE
    document = json.loads(path.read_text())
    if damage == "schema":
        document["schema"] = "invalid"
    elif damage == "duplicate_message":
        document["messages"].append(document["messages"][0])
    else:
        document["messages"] = []
    path.write_text(json.dumps(document))
    from cycle.checkpoints import CheckpointError
    with pytest.raises(CheckpointError, match="Unreadable message history"):
        RunInteractions(str(tmp_path / ".execution"), RUN).load()


def test_accept_keeps_its_own_payload_copy(store):
    sent = request("m1")
    store.accept(sent)
    sent["payload"]["text"] = "changed after acceptance"
    assert store.take("a", "one").items[0]["text"] == "Also called from cron"


# ------------------------------------------ what an earlier step was told
def test_the_steps_after_one_hear_what_it_was_told_once_it_arrived(tmp_path):
    """review judges implement's work; if implement was told to cap a discount
    and review was not, review sends the work back for following the person."""
    watch = Watch()

    def script(context, step_, point):
        if step_.id == "a":
            point("one")
            assert say("to-a", text="Cap it at the subtotal.") == ""
            assert watch.wait_for("to-a")["state"] == PENDING
            point("two")
            # Sent after a's last point: it never arrives, so it is not passed on.
            assert say("late", text="Too late for a.") == ""
            assert watch.wait_for("late")
        else:
            point("start")

    plugin = Listener(script)
    go(cycle_of(step("a"), step("b", needs=["a"]), step("c", needs=["b"]),
                step("beside")), Plugins(plugin), tmp_path, watch, jobs=1)
    assert plugin.read[("b", "start")] == ["Cap it at the subtotal."]
    assert plugin.read[("c", "start")] == ["Cap it at the subtotal."]
    # A step that does not come after a has nothing to judge of a's.
    assert plugin.read[("beside", "start")] == []
    messages = saved(tmp_path)
    assert messages["late"]["state"] == UNDELIVERED
    assert {(one["step"], one["point"]) for one in messages["to-a"]["deliveries"]} == {
        ("a", "two"), ("b", "start"), ("c", "start")}


def test_an_earlier_step_s_message_is_marked_as_that_step_s(store):
    store.accept(request("to-a"))
    store.delivered(store.take("a", "one"), "")
    store.finish("a", SUCCESS)
    store.run.steps["a"].status = SUCCESS
    store.begin("b", 1, store.plugins.get("test.listener"))
    [item] = store.take("b", "start").items
    assert item["earlier"] is True and item["to"] == "a"


def test_what_a_step_was_told_does_not_follow_it_into_a_new_execution(store):
    """A plan revision sends a round again: its old instructions went with the
    old work, and what comes after judges the new work."""
    store.accept(request("to-a"))
    store.delivered(store.take("a", "one"), "")
    store.finish("a", SUCCESS)
    store.run.revisions = [{"steps": ["a"]}]
    store.begin("b", 1, store.plugins.get("test.listener"))
    assert not store.take("b", "start")


def test_the_prompt_says_which_messages_were_an_earlier_step_s():
    from cycle.interactions import Snapshot
    from cycle.plugins import agent_run
    prompt = agent_run.with_clarifications("Review the change.", Snapshot(
        "review#1/agent", "review", 1, "agent", [
            {"id": "m1", "scope": "step", "to": "implement", "earlier": True,
             "text": "Cap it at the subtotal."},
            {"id": "m2", "scope": "run", "to": "", "earlier": False,
             "text": "Keep the API."}]))
    assert "1. (to the earlier step implement) Cap it at the subtotal." in prompt
    assert "2. (to the whole run) Keep the API." in prompt
    assert "Judge and build on that work with it in mind." in prompt
