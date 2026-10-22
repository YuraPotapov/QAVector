"""Subjects: everything any cycle did about one key, and what a person wrote about it."""

import json
import os
import threading

import pytest

from cycle import checkpoints, cyclefile, memory, model, planner, subjects
from cycle import run as records
from domain.cycle import (CANCELLED, FAILED, RUNNING, SKIPPED, SUCCESS, WAITING,
                          Artifact, CycleRun, StepRun)


# ------------------------------------------------------------------ fixtures
def _row(root, run_id, status=SUCCESS, key="", cycle="dev", name="Development",
         started=0.0, reached="", message="", title=None, **extra):
    """One run directory and its index row, without running anything."""
    os.makedirs(os.path.join(root, run_id), exist_ok=True)
    subject = ({"kind": "task", "key": key,
                "title": ("T " + key) if title is None else title} if key else {})
    return dict({"id": run_id, "cycle": cycle, "name": name, "status": status,
                 "started_at": started, "ended_at": started + 1, "subject": subject,
                 "reached": reached, "message": message}, **extra)


def _index(root, rows):
    records._atomic(records.index_path(str(root)),
                    {"schema": records.SCHEMA, "runs": rows})


def _step(step_id, plugin="command.shell", status=SUCCESS, at=0.0, outputs=None,
          message="", files=(), **extra):
    artifacts = [Artifact("log", path, step_id, name=os.path.basename(path), bytes=3)
                 for path in files]
    return StepRun(step_id=step_id, plugin=plugin, status=status, started_at=at,
                   ended_at=at + 1 if at is not None else None, message=message,
                   outputs=dict(outputs or {}), artifacts=artifacts, **extra)


def _saved(root, run_id, steps=(), key="QA-1", cycle="dev", started=100.0,
           status=SUCCESS, message="", labels=None, events=None, messages=None,
           **fields):
    """A saved run as a run leaves it: metadata, graph, event log, messages."""
    where = os.path.join(root, run_id)
    os.makedirs(where, exist_ok=True)
    run = CycleRun(id=run_id, cycle_id=cycle, cycle_name="Development", status=status,
                   started_at=started, ended_at=started + 50, message=message,
                   subject=({"kind": "task", "key": key, "title": "T " + key}
                            if key else {}), **fields)
    for one in steps:
        run.steps[one.step_id] = one
    records.save(run, where)
    labels = labels or {}
    records._atomic(os.path.join(where, "graph.json"), {"nodes": [
        {"id": one.step_id, "plugin": one.plugin,
         "label": labels.get(one.step_id, one.step_id.capitalize())} for one in steps]})
    if events is not None:
        os.makedirs(os.path.join(where, "logs"), exist_ok=True)
        with open(os.path.join(where, "logs", "cycle.jsonl"), "w", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(dict({"run_id": run_id}, **event)) + "\n")
    if messages is not None:
        os.makedirs(os.path.join(where, ".execution"), exist_ok=True)
        records._atomic(os.path.join(where, ".execution", "interactions.json"),
                        {"schema": 1, "run_id": run_id, "seq": len(messages),
                         "messages": messages, "snapshots": []})
    return records.summary(run)


def _ids(found):
    return [one["id"] for one in found["subjects"]]


@pytest.fixture
def mem(tmp_path):
    return str(tmp_path / "memory" / "cyclememory.json")


# ------------------------------------------------------------------- identity
def test_one_subject_is_every_run_that_named_it_whichever_cycle_made_it(tmp_path, mem):
    root = str(tmp_path / "runs")
    _index(root, [_row(root, "r3", key="QA-1", cycle="review", name="Review", started=30),
                  _row(root, "r2", key="QA-2", started=20),
                  _row(root, "r1", key="QA-1", started=10)])

    found = subjects.listing(root, mem)

    assert _ids(found) == ["QA-1", "QA-2"]
    first = found["subjects"][0]
    assert first["runs"] == 2
    assert [one["id"] for one in first["cycles"]] == ["review", "dev"]
    # The newest session is the one described: what the Cycles page would open.
    assert first["session"]["id"] == "review:QA-1"
    assert first["latest"]["id"] == "r3"


def test_a_run_without_a_subject_is_a_subject_of_its_own(tmp_path, mem):
    root = str(tmp_path / "runs")
    _index(root, [_row(root, "r1"), _row(root, "r2")])
    assert sorted(_ids(subjects.listing(root, mem))) == ["run:r1", "run:r2"]
    assert subjects.subject_id({"id": "r9", "subject": {"key": "  "}}) == "run:r9"


def test_the_newest_title_wins_but_a_run_without_one_does_not_erase_it(tmp_path, mem):
    root = str(tmp_path / "runs")
    _index(root, [_row(root, "r3", key="QA-1", started=30, title=""),
                  _row(root, "r2", key="QA-1", started=20, title="Fix the login"),
                  _row(root, "r1", key="QA-1", started=10, title="Old name")])
    assert subjects.listing(root, mem)["subjects"][0]["title"] == "Fix the login"


# ------------------------------------------------------------- where it stands
def _asking(root, run_id, key, started):
    row = _row(root, run_id, status=RUNNING, key=key, started=started, reached="approve")
    records._atomic(os.path.join(root, run_id, "graph.json"), {"nodes": [
        {"id": "approve", "label": "Agree to the plan", "plugin": "approval.gate"}]})
    return row


def test_what_waits_for_its_person_comes_first_then_what_is_going(tmp_path, mem):
    root = str(tmp_path / "runs")
    rows = [_row(root, "done", key="QA-0", started=90),
            _row(root, "going", status=RUNNING, key="QA-1", started=80, reached="work"),
            _asking(root, "asking", "QA-2", 10),
            _row(root, "paused", status=WAITING, key="QA-3", started=20, paused=True),
            _row(root, "parked", status=CANCELLED, key="QA-4", started=30,
                 message=subjects.PARKED),
            _row(root, "planned", status=FAILED, key="QA-5", started=40),
            _row(root, "stopped", status=CANCELLED, key="QA-6", started=50,
                 message="interrupted")]
    records._atomic(os.path.join(root, "going", "graph.json"), {"nodes": [
        {"id": "work", "label": "Work", "plugin": "command.shell"}]})
    _index(root, rows)
    planner.add_approval("dev", "planned", "approve", "Start work?",
                         {"key": "QA-5"}, memory_path=mem)

    with checkpoints.lock(os.path.join(root, "going")), \
            checkpoints.lock(os.path.join(root, "asking")):
        found = subjects.listing(root, mem)

    states = {one["id"]: (one["state"], one["needs_you"], one["active"])
              for one in found["subjects"]}
    assert states["QA-2"] == ("asking", True, True)
    assert states["QA-3"] == ("paused", True, True)
    assert states["QA-4"] == (subjects.PARKED, True, True)
    assert states["QA-5"] == ("planned", True, True)
    assert states["QA-1"] == ("running", False, True)
    assert states["QA-6"][1:] == (False, False)
    # Needs you first, newest activity first among them; then what is going;
    # then the rest.
    assert _ids(found) == ["QA-5", "QA-4", "QA-3", "QA-2", "QA-1", "QA-0", "QA-6"]
    assert found["counts"] == {"needs_you": 4, "active": 5, "all": 7}


def test_a_run_the_index_calls_running_but_nobody_holds_is_not_going(tmp_path, mem):
    root = str(tmp_path / "runs")
    _index(root, [_row(root, "r1", status=RUNNING, key="QA-1")])
    one = subjects.listing(root, mem)["subjects"][0]
    assert not one["active"] and one["state"] == "interrupted"


def test_views_narrow_and_counts_cover_every_subject(tmp_path, mem):
    root = str(tmp_path / "runs")
    _index(root, [_row(root, "a", key="QA-1", status=WAITING, paused=True, started=3),
                  _row(root, "b", key="QA-2", started=2),
                  _row(root, "c", key="QA-3", started=1)])

    needs = subjects.listing(root, mem, view=subjects.NEEDS_YOU)
    assert _ids(needs) == ["QA-1"] and needs["total"] == 1
    assert needs["counts"] == {"needs_you": 1, "active": 1, "all": 3}
    assert _ids(subjects.listing(root, mem, view=subjects.ACTIVE)) == ["QA-1"]
    with pytest.raises(subjects.SubjectError, match="view is one of"):
        subjects.listing(root, mem, view="mine")


def test_a_page_is_cut_after_the_order_is_decided(tmp_path, mem):
    root = str(tmp_path / "runs")
    _index(root, [_row(root, "r%d" % n, key="QA-%d" % n, started=n) for n in range(5)])
    page = subjects.listing(root, mem, limit=2, offset=1)
    assert _ids(page) == ["QA-3", "QA-2"]
    assert page["total"] == 5 and page["more"] is True
    last = subjects.listing(root, mem, limit=2, offset=4)
    assert _ids(last) == ["QA-0"] and last["more"] is False


# ---------------------------------------------------------------------- Resume
def _cycle_file(cycles_dir, *step_ids, phases=None):
    raw = {"id": "dev", "steps": [{"id": one, "plugin": "command.shell",
                                   "with": {"command": "true"}} for one in step_ids]}
    if phases:
        raw["phases"] = phases
        for step in raw["steps"]:
            step["phase"] = phases[0]["id"]
    os.makedirs(cycles_dir, exist_ok=True)
    with open(os.path.join(cycles_dir, "dev.yaml"), "w", encoding="utf-8") as handle:
        handle.write(cyclefile.render(model.to_document(model.parse_cycle(raw, "dev"))))


def _resumable(root, run_id, *step_ids, manifest=True):
    row = _row(root, run_id, status=FAILED, key="QA-1")
    where = os.path.join(root, run_id)
    records._atomic(os.path.join(where, "graph.json"),
                    {"nodes": [{"id": one, "plugin": "command.shell"} for one in step_ids]})
    if manifest:
        os.makedirs(os.path.join(where, ".execution"), exist_ok=True)
        records._atomic(os.path.join(where, ".execution", "manifest.json"), {"phases": []})
    return row


def test_resume_is_offered_where_the_cycles_page_offers_it(tmp_path, mem):
    root, cycles_dir = str(tmp_path / "runs"), str(tmp_path / "cycles")
    _cycle_file(cycles_dir, "a", "b")
    _index(root, [_resumable(root, "r2", "a", "b"),
                  _row(root, "r1", key="QA-2", status=SUCCESS)])
    found = {one["id"]: one for one in
             subjects.listing(root, mem, cycles_dir=cycles_dir)["subjects"]}
    assert found["QA-1"]["resume"] == {"run_id": "r2", "cycle": "dev",
                                       "run_dir": os.path.join(root, "r2"), "problem": ""}
    assert found["QA-2"]["resume"] == {}


@pytest.mark.parametrize("arrange, expected", [
    (lambda c, r: None, "no longer there"),
    (lambda c, r: _cycle_file(c, "a", "b", "c"), "steps changed"),
    (lambda c, r: (_cycle_file(c, "a", "b"),
                   os.remove(os.path.join(r, ".execution", "manifest.json"))), "predates Resume"),
    (lambda c, r: _cycle_file(c, "a", "b", phases=[{"id": "only", "title": "Only"}]),
     "phase contract changed"),
])
def test_resume_says_why_it_would_be_refused_before_anybody_asks(tmp_path, mem, arrange,
                                                                  expected):
    """The list must not offer a Resume that the core is certain to refuse."""
    root, cycles_dir = str(tmp_path / "runs"), str(tmp_path / "cycles")
    os.makedirs(cycles_dir, exist_ok=True)
    _index(root, [_resumable(root, "r1", "a", "b")])
    arrange(cycles_dir, os.path.join(root, "r1"))
    one = subjects.listing(root, mem, cycles_dir=cycles_dir)["subjects"][0]
    assert expected in one["resume"]["problem"]


# ----------------------------------------------------------------- the history
def _story(root):
    """One run of QA-1 with something of everything a history shows."""
    return _saved(
        root, "r1", started=100.0,
        steps=[_step("todo", "jira.issues", at=101, files=["steps/todo/jira.html"]),
               _step("plan", "agent.review", at=110,
                     outputs={"summary": "Two packages.", "risk": "medium",
                              "issues": [{"severity": "low", "description": "x"}],
                              "out_of_scope": ["the export"], "cost_usd": 0.61}),
               _step("approve", "approval.gate", at=120,
                     outputs={"approved": True, "answer": "Start work", "who": "ann"}),
               _step("admit", "check.gate", at=104, message="all 2 checks held"),
               _step("tests", "command.shell", status=FAILED, at=130, message="exited 1",
                     files=["steps/tests/stdout.log"]),
               _step("skip", "command.shell", status=SKIPPED, at=None),
               _step("note", "memory.remember", at=140)],
        labels={"approve": "Agree to the plan", "plan": "Work out the change"},
        events=[{"ts": 100.0, "kind": "cycle.run.start"},
                {"ts": 100.1, "kind": "cycle.run.mode", "mode": "fresh", "resume_count": 0},
                {"ts": 104.5, "kind": "cycle.step.stage", "step": "admit", "phase": "gate",
                 "body": {"checks": [{"name": "there is a task", "held": True}]}},
                {"ts": 111.0, "kind": "cycle.step.stage", "step": "plan", "phase": "tool",
                 "title": "Read x"},
                {"ts": 150.0, "kind": "cycle.run.end", "status": "failed",
                 "message": "tests failed: exited 1"}],
        messages=[{"id": "m1", "seq": 1, "scope": "step", "step": "plan", "kind": "text",
                   "payload": {"text": "Cover the empty list too"}, "who": "ann",
                   "accepted_at": 112.0, "state": "delivered",
                   "deliveries": [{"step": "plan", "state": "delivered", "at": 113.0}]}],
        revisions=[{"number": 1, "gate": "approve", "target": "plan",
                    "feedback": "Keep it to one report", "who": "ann",
                    "created_at": 115.0}],
        control_history=[{"action": "pause", "reason": "phase_boundary", "at": 125.0},
                         {"action": "continue", "at": 126.0}],
        node_approvals={"n1": {"step": "todo", "label": "Read the task",
                               "status": "approved", "created_at": 100.5,
                               "decided_at": 100.6},
                        "n2": {"step": "tests", "label": "Run the tests",
                               "status": "declined", "created_at": 128.0,
                               "decided_at": 129.0}},
        status=FAILED, message="tests failed: exited 1")


def test_a_history_tells_what_passed_between_a_person_and_the_cycles_in_order(tmp_path, mem):
    root = str(tmp_path / "runs")
    _index(root, [_story(root)])
    subjects.add_note("QA-1", "Ask the analyst about the roles", who="ann", memory_path=mem)

    told = subjects.history("QA-1", root, mem, scope=subjects.CONVERSATION)

    kinds = [one["kind"] for one in told["entries"]]
    assert kinds == ["run.start", "agent", "message", "revision", "ask", "control",
                     "control", "node_approval", "run.end", "note"]
    by_kind = {one["kind"]: one for one in told["entries"]}
    assert by_kind["run.start"]["mode"] == "fresh"
    assert by_kind["agent"]["label"] == "Work out the change"
    assert by_kind["agent"]["summary"] == "Two packages." and by_kind["agent"]["risk"] == "medium"
    assert by_kind["agent"]["out_of_scope"] == ["the export"]
    assert by_kind["message"]["text"] == "Cover the empty list too"
    assert by_kind["message"]["delivered"] == 1
    assert by_kind["revision"]["feedback"] == "Keep it to one report"
    assert by_kind["revision"]["target_label"] == "Work out the change"
    assert (by_kind["ask"]["answer"], by_kind["ask"]["who"]) == ("Start work", "ann")
    assert by_kind["ask"]["label"] == "Agree to the plan"
    # A declined node is a decision; the approved ones are not repeated here.
    assert by_kind["node_approval"]["status"] == "declined"
    assert by_kind["run.end"]["message"] == "tests failed: exited 1"
    assert by_kind["note"]["text"] == "Ask the analyst about the roles"
    assert told["subject"] == "QA-1"
    assert all(one["run"] == "r1" for one in told["entries"] if one["kind"] != "note")


def test_all_adds_checks_failures_and_folds_the_steps_that_passed(tmp_path, mem):
    root = str(tmp_path / "runs")
    _index(root, [_story(root)])

    told = subjects.history("QA-1", root, mem)

    by_kind = {}
    for one in told["entries"]:
        by_kind.setdefault(one["kind"], []).append(one)
    assert by_kind["check"][0]["checks"] == [{"name": "there is a task", "held": True}]
    assert by_kind["failed"][0]["label"] == "Tests"
    assert by_kind["failed"][0]["files"][0]["path"] == "steps/tests/stdout.log"
    folded = by_kind["steps"][0]
    assert folded["count"] == 2 and [one["step"] for one in folded["steps"]] == ["todo", "note"]
    assert by_kind["node_approvals"][0]["count"] == 1
    # A step that never started is nowhere: it is where the run did not go.
    assert not any(one.get("step") == "skip" for one in told["entries"])


def test_each_start_and_end_of_a_resumed_run_is_told(tmp_path, mem):
    root = str(tmp_path / "runs")
    _index(root, [_saved(root, "r1", events=[
        {"ts": 1.0, "kind": "cycle.run.start"},
        {"ts": 1.1, "kind": "cycle.run.mode", "mode": "fresh", "resume_count": 0},
        {"ts": 2.0, "kind": "cycle.run.end", "status": "cancelled", "message": "parked"},
        {"ts": 3.0, "kind": "cycle.run.start"},
        {"ts": 3.1, "kind": "cycle.run.mode", "mode": "resume", "resume_count": 1},
        {"ts": 4.0, "kind": "cycle.run.end", "status": "success", "message": ""}])])

    told = subjects.history("QA-1", root, mem, scope=subjects.CONVERSATION)["entries"]

    assert [(one["kind"], one.get("mode") or one.get("message")) for one in told] == [
        ("run.start", "fresh"), ("run.end", "parked"),
        ("run.start", "resume"), ("run.end", "")]


def test_a_borrowed_step_is_not_told_again(tmp_path, mem):
    root = str(tmp_path / "runs")
    _index(root, [_saved(root, "r1", steps=[
        _step("plan", "agent.review", at=10, source_run="r0"),
        _step("again", "agent.review", at=11, message="reused from r0 (success)")])])
    told = subjects.history("QA-1", root, mem)["entries"]
    assert not any(one["kind"] == "agent" for one in told)


def test_each_time_a_question_was_asked_is_told_with_what_it_showed(tmp_path, mem):
    """Asked, sent back, asked again: three entries, each with its own words."""
    root = str(tmp_path / "runs")
    _index(root, [_saved(root, "r1", steps=[
        _step("approve", "approval.gate", at=5, outputs={"answer": "Start work"},
              message="approved: Start work")])])
    asked = [{"question": "Start work on QA-1?", "detail": "The plan, v1.",
              "options": ["Start work", "Leave it"], "answered": False, "revision": True,
              "feedback": "Smaller, please", "who": "ann", "at": 5.5},
             {"question": "Start work on QA-1?", "detail": "The plan, v2.",
              "options": ["Start work", "Leave it"], "answered": True,
              "answer": "Start work", "who": "ann", "at": 6.5}]
    records._atomic(os.path.join(root, "r1", "steps", "approve", subjects.ASKED_FILE),
                    {"asked": asked})

    told = [one for one in subjects.history("QA-1", root, mem)["entries"]
            if one["kind"] == "ask"]

    assert [(one["at"], one["detail"]) for one in told] == [(5.5, "The plan, v1."),
                                                            (6.5, "The plan, v2.")]
    assert told[0]["revision"] and told[0]["feedback"] == "Smaller, please"
    assert told[0]["status"] == "" and told[1]["status"] == SUCCESS
    assert (told[1]["answer"], told[1]["who"], told[1]["kept"]) == ("Start work", "ann", True)


def test_an_unanswered_approval_says_the_question_it_put_on_the_plan(tmp_path, mem):
    root = str(tmp_path / "runs")
    _index(root, [_saved(root, "r1", status=FAILED, steps=[
        _step("approve", "approval.gate", status=FAILED, at=5, message="nobody answered")])])
    planner.add_approval("dev", "r1", "approve", "Push QA-1?", {"key": "QA-1"},
                         memory_path=mem)
    entry = [one for one in subjects.history("QA-1", root, mem)["entries"]
             if one["kind"] == "ask"][0]
    assert entry["question"] == "Push QA-1?" and entry["status"] == FAILED


def test_an_agent_names_the_prompts_its_calls_were_given(tmp_path, mem):
    root = str(tmp_path / "runs")
    _index(root, [_saved(root, "r1", steps=[_step("plan", "agent.review", at=5)])])
    call = os.path.join(root, "r1", "steps", "plan", "operations", "abc", "agent-1")
    os.makedirs(call)
    with open(os.path.join(call, subjects.PROMPT_FILE), "w", encoding="utf-8") as handle:
        handle.write("Plan it.")

    entry = [one for one in subjects.history("QA-1", root, mem)["entries"]
             if one["kind"] == "agent"][0]

    assert entry["prompts"] == ["steps/plan/operations/abc/agent-1/prompt.md"]


def test_a_limit_keeps_the_newest_and_says_how_many_came_before(tmp_path, mem):
    root = str(tmp_path / "runs")
    _index(root, [_story(root)])
    told = subjects.history("QA-1", root, mem, scope=subjects.CONVERSATION, limit=3)
    assert told["total"] == 9 and told["earlier"] == 6
    assert [one["kind"] for one in told["entries"]] == ["control", "node_approval", "run.end"]


def test_reading_a_history_leaves_every_run_directory_as_it_was(tmp_path, mem):
    root = str(tmp_path / "runs")
    _index(root, [_story(root), _saved(root, "r2", started=200.0)])

    def tree():
        return sorted(os.path.relpath(os.path.join(folder, name), root)
                      for folder, dirs, files in os.walk(root) for name in dirs + files)

    before = tree()
    subjects.history("QA-1", root, mem)
    subjects.listing(root, mem)
    assert tree() == before


def test_a_run_whose_directory_is_gone_is_said_once(tmp_path, mem):
    root = str(tmp_path / "runs")
    row = _saved(root, "r1")
    _index(root, [row])
    import shutil
    shutil.rmtree(os.path.join(root, "r1"))
    told = subjects.history("QA-1", root, mem)["entries"]
    assert [one["kind"] for one in told] == ["run.gone"]


def test_the_files_of_each_run_are_listed_newest_run_first(tmp_path, mem):
    root = str(tmp_path / "runs")
    _index(root, [_story(root),
                  _saved(root, "r2", started=200.0, steps=[
                      _step("report", "report.html", at=201, files=["reports/run.html"])])])
    told = subjects.history("QA-1", root, mem)
    files = told["files"]
    assert [one["run"] for one in files] == ["r2", "r1"]
    assert [one["step"] for one in files[1]["steps"]] == ["todo", "tests"]
    assert [one["run"] for one in told["runs"]] == ["r2", "r1"]
    assert told["runs"][1]["status"] == FAILED


def test_an_unknown_subject_or_scope_is_refused(tmp_path, mem):
    root = str(tmp_path / "runs")
    _index(root, [])
    with pytest.raises(subjects.SubjectError, match="no subject"):
        subjects.history("QA-9", root, mem)
    with pytest.raises(subjects.SubjectError, match="scope is one of"):
        subjects.history("QA-9", root, mem, scope="everything")


# --------------------------------------------------------------------- notes
def test_notes_are_added_never_rewritten_and_kept_beside_the_memory(tmp_path, mem):
    subjects.add_note("QA-1", "  First  ", who="ann", memory_path=mem)
    subjects.add_note("QA-1", "Second", who="bob", memory_path=mem)

    kept = subjects.notes("QA-1", mem)

    assert [(one["text"], one["who"]) for one in kept] == [("First", "ann"), ("Second", "bob")]
    assert os.path.dirname(subjects.notes_path(mem)) == os.path.dirname(mem)
    assert subjects.notes("QA-2", mem) == []
    # The memory itself is not where they went.
    assert memory.load(mem) == {}


@pytest.mark.parametrize("text, problem", [
    ("   ", "needs some text"),
    ("x" * (subjects.MAX_NOTE + 1), "at most"),
])
def test_an_empty_or_overlong_note_is_refused(mem, text, problem):
    with pytest.raises(subjects.SubjectError, match=problem):
        subjects.add_note("QA-1", text, memory_path=mem)


def test_two_notes_written_at_once_are_both_kept(mem):
    threads = [threading.Thread(target=subjects.add_note,
                                args=("QA-1", "note %d" % n), kwargs={"memory_path": mem})
               for n in range(8)]
    for one in threads:
        one.start()
    for one in threads:
        one.join()
    assert sorted(one["text"] for one in subjects.notes("QA-1", mem)) == [
        "note %d" % n for n in range(8)]


def test_a_note_is_activity_and_a_subject_with_only_notes_stays_listed(tmp_path, mem):
    root = str(tmp_path / "runs")
    _index(root, [_row(root, "r2", key="QA-2", started=50),
                  _row(root, "r1", key="QA-1", started=10)])
    subjects.add_note("QA-1", "Back to this tomorrow", memory_path=mem)
    subjects.add_note("QA-7", "Not started yet", memory_path=mem)

    found = subjects.listing(root, mem)

    assert _ids(found)[:2] in (["QA-7", "QA-1"], ["QA-1", "QA-7"])
    one = {item["id"]: item for item in found["subjects"]}
    assert one["QA-1"]["note"]["text"] == "Back to this tomorrow"
    assert one["QA-7"]["runs"] == 0 and one["QA-7"]["notes"] == 1
    assert subjects.history("QA-7", root, mem)["entries"][0]["kind"] == "note"


# --------------------------------------------------------------- the stop reason
def test_a_stop_from_the_window_keeps_the_reason_it_gave(monkeypatch):
    import io

    import session_launcher as sl
    from cycle import executor

    asked = []
    monkeypatch.setattr(executor, "request_stop",
                        lambda run_id=None, reason="the run was stopped":
                        asked.append((run_id, reason)) or 1)
    monkeypatch.setattr(sl.sys, "stdin", io.StringIO(
        '{"command": "cycle.stop", "run_id": "r1", "reason": "parked"}\n'
        '{"command": "cycle.stop", "run_id": "r2"}\n'))
    sl.read_commands({})

    # Then the channel closing stops whatever is left, as it always has.
    assert asked[:2] == [("r1", "parked"), ("r2", "the run was stopped")]


# ------------------------------------------------------------ the command line
def _launcher(home, *args):
    import subprocess
    import sys
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, CMS_HOME=str(home))
    done = subprocess.run([sys.executable, os.path.join(project, "session_launcher.py"),
                           *args], capture_output=True, text=True, env=env,
                          cwd=str(home), timeout=60)
    return done.returncode, json.loads(done.stdout)


def test_the_command_line_lists_tells_and_keeps_notes(tmp_path):
    root = str(tmp_path / "cycle-runs")
    memory_file = str(tmp_path / "m.json")
    _index(root, [_story(root)])
    flag = "--cycle-memory-file=" + memory_file

    code, noted = _launcher(tmp_path, "--cycle-subject-note=QA-1",
                            "--cycle-subject-note-text=Line one\nline two", flag)
    assert code == 0 and noted["note"]["text"] == "Line one\nline two"

    code, listed = _launcher(tmp_path, "--cycle-subject-list", "--cycle-subject-list-limit=1",
                             flag)
    assert code == 0 and [one["id"] for one in listed["subjects"]] == ["QA-1"]
    assert listed["subjects"][0]["note"]["text"] == "Line one\nline two"

    code, told = _launcher(tmp_path, "--cycle-subject-history=QA-1",
                           "--cycle-subject-history-scope=conversation",
                           "--cycle-subject-history-limit=2", flag)
    assert code == 0 and told["earlier"] == told["total"] - 2
    assert told["entries"][-1]["kind"] == "note"

    code, refused = _launcher(tmp_path, "--cycle-subject-history=QA-404", flag)
    assert code == 2 and "no subject" in refused["problems"][0]


def test_a_parked_run_says_where_resume_goes_on_not_where_it_got_to(tmp_path, mem):
    root = str(tmp_path / "runs")
    row = _saved(root, "r1", status=CANCELLED, message=subjects.PARKED, steps=[
        _step("work", at=1, outputs={}),
        _step("approve", "approval.gate", status=CANCELLED, at=2),
        _step("after", status=CANCELLED, at=None)],
        labels={"work": "Slow work", "approve": "Agree to go on", "after": "After both"})
    _index(root, [row])
    one = subjects.listing(root, mem)["subjects"][0]
    assert one["state"] == subjects.PARKED
    assert one["resume_at"] == "Agree to go on"
    assert subjects.resume_at(os.path.join(root, "nowhere")) == ""


# ------------------------------------------------------------- an agent's account
def _stream(where, call, *events):
    folder = os.path.join(where, "steps", "plan", "operations", call, "agent-1")
    os.makedirs(folder)
    with open(os.path.join(folder, "stdout.log"), "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")
    return folder


def test_an_agent_s_account_keeps_what_it_weighed_said_and_reached_for(tmp_path):
    root = str(tmp_path / "runs")
    _index(root, [_saved(root, "r1", steps=[_step("plan", "agent.review", at=5)])])
    folder = _stream(os.path.join(root, "r1"), "abc",
                     {"type": "system", "subtype": "init", "model": "opus"},
                     {"type": "assistant", "message": {"content": [
                         {"type": "thinking", "thinking": "Which field is the decision?"},
                         {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}]}},
                     {"type": "user", "message": {"content": [
                         {"type": "tool_result", "content": "a very long file"}]}},
                     {"type": "assistant", "message": {"content": [
                         {"type": "text", "text": "The decision is the resolution field."}]}},
                     {"type": "result", "num_turns": 3, "total_cost_usd": 0.5})
    with open(os.path.join(os.path.dirname(folder), subjects.PROMPT_FILE), "w") as handle:
        handle.write("Plan it.")

    told = subjects.transcript("r1", "plan", root=root)

    (call,) = told["calls"]
    assert [one["kind"] for one in call["stages"]] == ["start", "thinking", "tool", "text",
                                                       "result"]
    assert "a very long file" not in json.dumps(told)
    assert call["prompt"] == "steps/plan/operations/abc/prompt.md"


def test_a_long_account_keeps_its_end_and_says_how_much_was_left_out(tmp_path):
    root = str(tmp_path / "runs")
    _index(root, [_saved(root, "r1", steps=[_step("plan", "agent.review", at=5)])])
    _stream(os.path.join(root, "r1"), "abc", *[
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "line %d" % n}]}}
        for n in range(10)])
    told = subjects.transcript("r1", "plan", root=root, limit=4)
    assert told["cut"] == 6
    assert [one["detail"] for one in told["calls"][0]["stages"]] == [
        "line 6", "line 7", "line 8", "line 9"]


def test_an_account_is_only_read_from_a_run_under_the_runs(tmp_path):
    root = str(tmp_path / "runs")
    os.makedirs(root)
    with pytest.raises(subjects.SubjectError, match="no run"):
        subjects.transcript("../elsewhere", "plan", root=root)
