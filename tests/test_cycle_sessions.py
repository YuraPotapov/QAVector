"""What a cycle works on: the subject a run finds, and the sessions it makes."""

import json
import os

import pytest

from cycle import bus, checkpoints, cyclefile, executor, memory, model, registry
from cycle import run as records
from cycle import sessions
from domain.cycle import FAILED, RUNNING, SUCCESS


class Plugin(registry.CyclePlugin):
    def __init__(self, action, reusable=True):
        self.metadata = registry.PluginMetadata(id="test.node", name="Node",
                                                reusable=reusable)
        self.action, self.calls = action, []

    def execute(self, context, step):
        self.calls.append(step.id)
        return self.action(context, step)


class Plugins:
    def __init__(self, **plugins):
        self.plugins = plugins

    def get(self, name):
        return self.plugins.get(name, registry.get(name))


def node(name, **extra):
    return dict(id=name, plugin=name, **extra)


SUBJECT = {"kind": "task", "key": "${steps.todo.outputs.key}",
           "title": "${steps.todo.outputs.title}",
           "memory": "dev/${steps.todo.outputs.key}", "pin": "task_key"}


def workflow(*steps, subject=SUBJECT, **extra):
    raw = dict(id="demo", steps=list(steps), **extra)
    if subject is not None:
        raw["subject"] = subject
        raw.setdefault("variables", {"task_key": ""})
    return model.parse_cycle(raw, "demo")


def todo_cycle(**extra):
    return workflow(node("todo", **{"with": {"issue": "${vars.task_key}"}}),
                    node("work", needs=["todo"]), **extra)


def picking(key="QA-1", title="Fix the login"):
    return Plugin(lambda c, s: registry.succeeded(key=key, title=title))


def execute(cycle, plugins, where, observer=None, **kwargs):
    return executor.run_cycle(cycle, str(where), registry=plugins,
                              observer=observer, **kwargs)


# ----------------------------------------------------------------- the file
def test_a_subject_is_read_from_the_file():
    subject = todo_cycle().subject
    assert (subject.kind, subject.key, subject.pin) == (
        "task", "${steps.todo.outputs.key}", "task_key")


def test_a_cycle_without_one_has_none():
    assert workflow(node("a"), subject=None).subject is None


def test_a_subject_that_is_not_a_mapping_cannot_be_read():
    with pytest.raises(model.CycleError, match="subject must be a mapping"):
        model.parse_cycle({"id": "demo", "subject": "QA-1", "steps": []}, "demo")


def test_a_clean_subject_has_no_problems():
    assert model.problems(todo_cycle()) == []


@pytest.mark.parametrize("change, expected", [
    ({"kind": ""}, "kind is empty"),
    ({"key": ""}, "key is empty"),
    ({"key": "${steps.nowhere.outputs.key}"}, "'nowhere', which is not a step"),
    ({"pin": "missing"}, "not one of the cycle's variables"),
])
def test_what_is_wrong_with_a_subject_is_said(change, expected):
    cycle = workflow(node("todo", **{"with": {"issue": "${vars.task_key}"}}),
                     subject=dict(SUBJECT, **change))
    assert any(expected in one for one in model.problems(cycle))


def test_a_pin_no_step_reads_could_not_change_anything():
    cycle = workflow(node("todo"))
    assert any("no step reads ${vars.task_key}" in one
               for one in model.problems(cycle))


def test_a_secret_never_becomes_a_subject():
    cycle = model.parse_cycle({
        "id": "demo", "variables": {"token": {"secret": True}},
        "subject": {"kind": "task", "key": "${vars.token}"},
        "steps": [node("a")]}, "demo")
    assert any("secret 'token'" in one for one in model.problems(cycle))


def test_an_unknown_subject_key_is_reported():
    raw = {"id": "demo", "subject": dict(SUBJECT, owner="me"),
           "variables": {"task_key": ""},
           "steps": [node("todo", **{"with": {"issue": "${vars.task_key}"}})]}
    cycle = model.parse_cycle(raw, "demo")
    assert any("subject: unknown key 'owner'" in one
               for one in model.problems(cycle, raw=raw))


def test_the_graph_says_what_the_cycle_works_on_and_who_decides():
    graph = model.to_graph(todo_cycle())
    assert graph["subject"]["kind"] == "task"
    assert graph["subject"]["sources"] == ["todo"]
    assert model.to_graph(workflow(node("a"), subject=None))["subject"] is None


def test_a_subject_survives_being_written_back():
    document = model.to_document(todo_cycle())
    assert document["subject"] == SUBJECT
    text = cyclefile.render(document)
    assert text.index("variables:") < text.index("subject:") < text.index("steps:")
    import yaml
    again = model.parse_cycle(yaml.safe_load(text), "demo")
    assert again.subject == todo_cycle().subject


# ------------------------------------------------------------------ the run
def test_the_run_finds_its_subject_once_the_step_that_takes_it_is_done(tmp_path):
    recorder = bus.Recorder()
    plugins = Plugins(todo=picking(), work=Plugin(lambda c, s: registry.succeeded()))
    run = execute(todo_cycle(), plugins, tmp_path / "run", recorder)

    assert run.subject == {"kind": "task", "key": "QA-1", "title": "Fix the login",
                           "memory": "dev/QA-1", "pin": "task_key", "step": "todo"}
    said = recorder.of("subject")
    assert len(said) == 1 and said[0]["key"] == "QA-1"
    kinds = recorder.kinds()
    assert kinds.index("subject") > kinds.index("step_end")
    saved = records.load(str(tmp_path / "run"))
    assert saved.subject["key"] == "QA-1"


def test_nothing_found_is_no_subject(tmp_path):
    recorder = bus.Recorder()
    plugins = Plugins(todo=picking(key=""), work=Plugin(lambda c, s: registry.succeeded()))
    run = execute(todo_cycle(), plugins, tmp_path / "run", recorder)
    assert run.subject == {}
    assert recorder.of("subject") == []


def test_a_cycle_without_a_subject_says_none(tmp_path):
    recorder = bus.Recorder()
    plugins = Plugins(a=Plugin(lambda c, s: registry.succeeded(key="X")))
    run = execute(workflow(node("a"), subject=None), plugins, tmp_path / "run", recorder)
    assert run.subject == {} and recorder.of("subject") == []


def test_a_fresh_run_says_it_is_fresh(tmp_path):
    recorder = bus.Recorder()
    plugins = Plugins(todo=picking(), work=Plugin(lambda c, s: registry.succeeded()))
    execute(todo_cycle(), plugins, tmp_path / "run", recorder)
    mode = recorder.of("run_mode")[0]
    assert mode["mode"] == "fresh" and mode["kept"] == [] and mode["rerun"] == []


def test_a_resume_says_what_it_kept_and_why_the_rest_runs_again(tmp_path):
    work = Plugin(lambda c, s: registry.failed("broke"))
    plugins = Plugins(todo=picking(), work=work,
                      after=Plugin(lambda c, s: registry.succeeded()))
    cycle = workflow(node("todo", **{"with": {"issue": "${vars.task_key}"}}),
                     node("work", needs=["todo"]), node("after", needs=["work"]))
    execute(cycle, plugins, tmp_path / "run")

    work.action = lambda c, s: registry.succeeded()
    recorder = bus.Recorder()
    run = execute(cycle, plugins, tmp_path / "run", recorder, resume=True)

    mode = recorder.of("run_mode")[0]
    assert mode["mode"] == "resume" and mode["resume_count"] == 1
    assert mode["kept"] == ["todo"]
    assert mode["rerun"] == [
        {"step": "work", "reason": "failed last time"},
        {"step": "after", "reason": "waits on a step that runs again"}]
    # Kept, so the subject is the one the first attempt found - said again
    # at the start, because a front-end watching this run never saw it.
    assert recorder.of("subject")[0]["key"] == "QA-1"
    assert run.subject["key"] == "QA-1"


def test_a_resume_that_takes_the_task_again_finds_out_again(tmp_path):
    todo = Plugin(lambda c, s: registry.failed("jira was down"))
    plugins = Plugins(todo=todo, work=Plugin(lambda c, s: registry.succeeded()))
    execute(todo_cycle(), plugins, tmp_path / "run")

    todo.action = lambda c, s: registry.succeeded(key="QA-2", title="Other")
    recorder = bus.Recorder()
    run = execute(todo_cycle(), plugins, tmp_path / "run", recorder, resume=True)
    assert run.subject["key"] == "QA-2"
    assert [one["key"] for one in recorder.of("subject")] == ["QA-2"]


def test_the_subject_and_where_it_got_to_are_in_the_index(tmp_path):
    root = tmp_path / "runs"
    plugins = Plugins(todo=picking(), work=Plugin(lambda c, s: registry.failed("no")))
    where = root / "20260924-100000-demo"
    execute(todo_cycle(), plugins, where, records.Persister(root=str(root)))

    row = records.index(str(root))[0]
    assert row["subject"]["key"] == "QA-1"
    assert row["reached"] == "work"
    assert row["resume_count"] == 0


def test_the_event_stream_carries_the_new_events(tmp_path):
    lines = []
    observer = bus.EventObserver(sink=lambda kind, **fields: lines.append(
        dict(fields, kind=kind)))
    plugins = Plugins(todo=picking(), work=Plugin(lambda c, s: registry.succeeded()))
    execute(todo_cycle(), plugins, tmp_path / "run", observer)

    by_kind = {one["kind"]: one for one in lines}
    assert by_kind["cycle.run.mode"]["mode"] == "fresh"
    assert by_kind["cycle.subject"]["subject"]["key"] == "QA-1"
    assert by_kind["cycle.subject"]["subject"]["kind"] == "task"
    assert by_kind["cycle.subject"]["subject"]["step"] == "todo"
    assert by_kind["cycle.run.start"]["graph"]["subject"]["sources"] == ["todo"]


# ----------------------------------------------------------------- sessions
def _row(root, run_id, status=SUCCESS, key="", cycle="demo", started=0.0,
         reached="", memory_key=""):
    """One run directory and its index row, without running anything."""
    os.makedirs(os.path.join(root, run_id), exist_ok=True)
    subject = ({"kind": "task", "key": key, "title": "T " + key,
                "memory": memory_key} if key else {})
    return {"id": run_id, "cycle": cycle, "name": "Demo", "status": status,
            "started_at": started, "ended_at": started + 1, "subject": subject,
            "reached": reached, "message": ""}


def _index(root, rows):
    records._atomic(records.index_path(str(root)),
                    {"schema": records.SCHEMA, "runs": rows})


def test_runs_on_one_subject_are_one_session_and_the_rest_stand_alone(tmp_path):
    root = str(tmp_path)
    _index(root, [_row(root, "r3", key="QA-1", started=30),
                  _row(root, "r2", started=20),
                  _row(root, "r1", status=FAILED, key="QA-1", started=10)])

    found = sessions.sessions(root, memory_path=str(tmp_path / "m.json"))
    assert [one["id"] for one in found] == ["demo:QA-1", "demo:run:r2"]
    first = found[0]
    assert [one["id"] for one in first["runs"]] == ["r3", "r1"]
    assert first["latest"] == "r3" and first["status"] == SUCCESS
    assert first["subject"]["title"] == "T QA-1"


def test_sessions_narrow_to_one_cycle(tmp_path):
    root = str(tmp_path)
    _index(root, [_row(root, "a", key="K-1", cycle="one"),
                  _row(root, "b", key="K-1", cycle="two")])
    found = sessions.sessions(root, cycle_id="two", memory_path=str(tmp_path / "m.json"))
    assert [one["id"] for one in found] == ["two:K-1"]


def test_a_run_nobody_is_executing_is_interrupted_not_running(tmp_path):
    root = str(tmp_path)
    _index(root, [_row(root, "r1", status=RUNNING, key="K-1")])
    found = sessions.sessions(root, memory_path=str(tmp_path / "m.json"))[0]
    assert found["status"] == sessions.INTERRUPTED and not found["running"]


def test_a_run_being_executed_is_running(tmp_path):
    root = str(tmp_path)
    _index(root, [_row(root, "r1", status=RUNNING, key="K-1")])
    with checkpoints.lock(os.path.join(root, "r1")):
        found = sessions.sessions(root, memory_path=str(tmp_path / "m.json"))[0]
    assert found["status"] == RUNNING and found["running"]


def test_a_session_shows_what_its_subject_remembers(tmp_path):
    root, path = str(tmp_path), str(tmp_path / "m.json")
    memory.remember("dev/K-1", {"state": "committed"}, path)
    _index(root, [_row(root, "r1", key="K-1", memory_key="dev/K-1")])
    found = sessions.sessions(root, memory_path=path)[0]
    assert found["memory"]["fields"] == {"state": "committed"}


def test_where_a_session_got_to_is_named_as_the_graph_names_it(tmp_path):
    root = str(tmp_path)
    _index(root, [_row(root, "r1", status=RUNNING, key="K-1", reached="approve")])
    records._atomic(os.path.join(root, "r1", "graph.json"), {"nodes": [
        {"id": "approve", "label": "Agree to the plan", "plugin": "approval.gate"}]})
    found = sessions.sessions(root, memory_path=str(tmp_path / "m.json"))[0]
    assert found["reached_label"] == "Agree to the plan"
    assert found["reached_plugin"] == "approval.gate"


def test_deleting_a_session_removes_its_runs_rows_and_memory(tmp_path):
    root, path = str(tmp_path), str(tmp_path / "m.json")
    memory.remember("dev/K-1", {"state": "committed"}, path)
    memory.remember("dev/K-2", {"state": "committed"}, path)
    _index(root, [_row(root, "r2", key="K-2", memory_key="dev/K-2", started=2),
                  _row(root, "r1", key="K-1", memory_key="dev/K-1", started=1)])

    gone = sessions.delete("demo:K-1", root, path)

    assert gone == {"id": "demo:K-1", "runs": ["r1"], "memory": "dev/K-1"}
    assert not os.path.exists(os.path.join(root, "r1"))
    assert os.path.exists(os.path.join(root, "r2"))
    assert [row["id"] for row in records.index(root)] == ["r2"]
    assert not memory.known("dev/K-1", path) and memory.known("dev/K-2", path)


def test_a_running_session_is_not_deleted(tmp_path):
    root = str(tmp_path)
    _index(root, [_row(root, "r1", status=RUNNING, key="K-1")])
    with checkpoints.lock(os.path.join(root, "r1")):
        with pytest.raises(sessions.SessionError, match="still running"):
            sessions.delete("demo:K-1", root, str(tmp_path / "m.json"))
    assert os.path.exists(os.path.join(root, "r1"))


def test_an_index_row_cannot_name_a_directory_outside_the_runs(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    outside = tmp_path / "precious"
    outside.mkdir()
    _index(str(root), [dict(_row(str(root), "r1", key="K-1"), id="../precious")])
    sessions.delete("demo:K-1", str(root), str(tmp_path / "m.json"))
    assert outside.exists()


def test_an_unknown_session_is_said_to_be_unknown(tmp_path):
    with pytest.raises(sessions.SessionError, match="no session"):
        sessions.delete("demo:nothing", str(tmp_path), str(tmp_path / "m.json"))


# ------------------------------------------------------------ the command line
def _launcher(home, *args):
    import subprocess
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, CMS_HOME=str(home))
    done = subprocess.run([sys.executable, os.path.join(root, "session_launcher.py"),
                           *args], capture_output=True, text=True, env=env,
                          cwd=str(home), timeout=60)
    return done.returncode, json.loads(done.stdout)


def test_the_command_line_lists_and_deletes_sessions(tmp_path):
    root = str(tmp_path / "cycle-runs")
    memory_file = str(tmp_path / "m.json")
    memory.remember("dev/K-1", {"state": "committed"}, memory_file)
    _index(root, [_row(root, "r1", key="K-1", memory_key="dev/K-1")])

    code, listed = _launcher(tmp_path, "--cycle-sessions=demo",
                             "--cycle-memory-file=" + memory_file)
    assert code == 0 and [one["id"] for one in listed["sessions"]] == ["demo:K-1"]

    code, gone = _launcher(tmp_path, "--cycle-session-delete=demo:K-1",
                           "--cycle-memory-file=" + memory_file)
    assert code == 0 and gone["runs"] == ["r1"] and gone["memory"] == "dev/K-1"

    code, refused = _launcher(tmp_path, "--cycle-session-delete=demo:K-1")
    assert code == 2 and "no session" in refused["problems"][0]


# ------------------------------------------------------------ runs from before
def test_runs_from_before_subjects_are_given_theirs_once(tmp_path):
    """An old index row has no subject key; its run's outputs still say which task."""
    root, cycles_dir = tmp_path / "runs", tmp_path / "cycles"
    cycles_dir.mkdir()
    (cycles_dir / "demo.yaml").write_text(cyclefile.render(model.to_document(todo_cycle())))
    plugins = Plugins(todo=picking(), work=Plugin(lambda c, s: registry.failed("no")))
    where = root / "20260901-100000-demo"
    before = workflow(node("todo"), node("work", needs=["todo"]), subject=None)
    execute(before, plugins, where, records.Persister(root=str(root)))
    rows = records.index(str(root))
    for row in rows:
        for name in ("subject", "reached", "resume_count"):
            row.pop(name, None)
    _index(root, rows)

    found = sessions.sessions(str(root), memory_path=str(tmp_path / "m.json"),
                              cycles_dir=str(cycles_dir))

    assert [one["id"] for one in found] == ["demo:QA-1"]
    assert found[0]["reached"] == "work"
    assert records.index(str(root))[0]["subject"]["key"] == "QA-1"
    assert sessions.backfill(str(root), str(cycles_dir)) == 0


def test_a_run_that_found_no_subject_is_not_asked_again(tmp_path):
    root = str(tmp_path)
    _index(root, [_row(root, "r1")])            # subject: {} - it looked
    assert sessions.backfill(root) == 0


def test_a_failed_run_got_to_its_first_failure_not_to_its_report(tmp_path):
    """The report runs whatever happened; it is not where the run stopped."""
    root = tmp_path / "runs"
    plugins = Plugins(todo=picking(), work=Plugin(lambda c, s: registry.failed("no")),
                      report=Plugin(lambda c, s: registry.succeeded()))
    cycle = workflow(node("todo", **{"with": {"issue": "${vars.task_key}"}}),
                     node("work", needs=["todo"], on_failure="continue"),
                     node("report", needs=["work"], **{"if": "${run.id}"}))
    execute(cycle, plugins, root / "20260924-100000-demo",
            records.Persister(root=str(root)))
    assert records.index(str(root))[0]["reached"] == "work"


def test_runs_kept_elsewhere_are_found_where_the_flag_says(tmp_path):
    """A front-end that keeps runs outside the data root names the folder on
    every call; a session list read from the default would see none of them."""
    root = str(tmp_path / "elsewhere")
    _index(root, [_row(root, "r1", key="K-1")])

    code, listed = _launcher(tmp_path, "--cycle-sessions=demo")
    assert code == 0 and listed["sessions"] == []

    code, listed = _launcher(tmp_path, "--cycle-sessions=demo",
                             "--cycle-runs-dir=" + root)
    assert code == 0 and [one["id"] for one in listed["sessions"]] == ["demo:K-1"]
