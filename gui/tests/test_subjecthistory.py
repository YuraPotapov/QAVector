"""How a subject reads: its row's words, what to do next, and its story as a page."""

import re

import pytest

from cms_gui import subjecthistory as story
from cms_gui import theme

LIVE = "r-live"


def subject(state="", latest=None, resume=None, **extra):
    base = {"id": "QA-1", "key": "QA-1", "title": "Fix the login", "state": state,
            "needs_you": state in ("asking", "node_approval", "paused", "parked", "planned"),
            "active": state in ("asking", "node_approval", "paused", "parked", "planned",
                                "running", "pausing"),
            "latest": latest if latest is not None else {"id": "r1", "cycle": "dev"},
            "session": {"status": "success", "subject": {"key": "QA-1"}},
            "resume": resume or {}, "cycles": [{"id": "dev", "name": "Development"},
                                               {"id": "rev", "name": "Review"}],
            "runs": 3, "started_at": 0}
    base.update(extra)
    return base


def plain(page):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page)).replace("&quot;", '"').strip()


# ----------------------------------------------------------------- the row
def test_a_subject_is_called_by_its_key_and_title():
    assert story.title(subject()) == "QA-1  Fix the login"
    assert story.title(subject(title="")) == "QA-1"


@pytest.mark.parametrize("state, latest, expected", [
    ("asking", {"reached_label": "Agree to the plan"}, "Waiting: Agree to the plan"),
    ("node_approval", {}, "Waiting: node approval"),
    ("planned", {"reached_label": "Push"}, "Waiting: Push - not answered"),
    ("parked", {"reached_label": "Push"}, "Parked - Resume continues it"),
    ("parked", {}, "Parked - Resume continues it"),
    ("paused", {"next_action": "Continue after Implement"}, "Paused - Continue after Implement"),
    ("running", {"reached_label": "Make the change"}, "Running: Make the change"),
])
def test_the_state_column_says_where_it_stands(state, latest, expected):
    assert story.state_text(subject(state, latest=dict(latest, id="r1"))) == expected


def test_a_finished_subject_says_it_the_way_the_cycles_page_does():
    from cms_gui import cyclesubjects

    session = {"status": "success", "outcome": "workflow_completed", "subject": {"key": "QA-1"}}
    words = story.state_text(subject("workflow_completed", session=session))
    assert words.lower() == cyclesubjects.status_word(session).lower()
    assert words[0].isupper()


def test_a_subject_with_only_notes_says_so():
    lonely = subject(latest={}, session={}, notes=2)
    assert story.state_text(lonely) == "Notes only"


def test_what_waits_for_you_is_written_in_the_warning_ink():
    assert story.state_colour(subject("asking")) == theme.WARN
    assert story.state_colour(subject("running")) == theme.ACCENT


def test_the_cycles_are_named_newest_first_and_the_note_is_one_line():
    assert story.cycles_text(subject()) == "Development, Review"
    noted = subject(note={"text": "Line one\n\n  line two"})
    assert story.note_text(noted) == "Line one line two"


# --------------------------------------------------------- what to do next
def test_a_question_in_the_live_run_is_answered_in_its_cockpit():
    asking = subject("asking", latest={"id": LIVE, "running": True})
    assert story.next_action(asking, LIVE, busy=True)[:3] == ("cockpit", "Answer in Cockpit", True)
    assert story.can_park(asking, LIVE)


def test_the_live_run_working_is_opened_and_can_be_parked():
    going = subject("running", latest={"id": LIVE, "running": True})
    assert story.next_action(going, LIVE, busy=True)[:3] == ("open", "Open run", True)
    assert story.can_park(going, LIVE)


def test_a_run_another_window_is_going_with_cannot_be_parked_from_here():
    elsewhere = subject("running", latest={"id": "r-other", "running": True})
    assert not story.can_park(elsewhere, LIVE)


def test_a_stopped_run_is_resumed_unless_another_is_going():
    offer = {"run_id": "r1", "cycle": "dev", "run_dir": "/runs/r1", "problem": ""}
    stopped = subject("cancelled", resume=offer)
    assert story.next_action(stopped)[:3] == ("resume", "Resume", True)
    verb, _label, enabled, why = story.next_action(stopped, LIVE, busy=True)
    assert verb == "resume" and not enabled and "Park or stop it first" in why


def test_a_run_resume_would_refuse_is_opened_and_says_why():
    offer = {"run_id": "r1", "cycle": "dev", "run_dir": "/runs/r1",
             "problem": "The cycle's steps changed since this run."}
    verb, _label, enabled, why = story.next_action(subject("failed", resume=offer))
    assert (verb, enabled) == ("open", True) and "steps changed" in why


def test_a_subject_with_no_run_offers_nothing_to_do():
    assert story.next_action(subject(latest={})) is None


# ----------------------------------------------------------------- the story
def answer(*entries, earlier=0, runs=None):
    return {"entries": list(entries), "earlier": earlier,
            "runs": runs or [{"run": "r1", "cycle": "dev", "started_at": 1},
                             {"run": "r2", "cycle": "dev", "started_at": 2}],
            "files": []}


def test_a_history_says_who_did_what_in_order_with_runs_numbered():
    page, _actions = story.history_html(answer(
        {"kind": "run.start", "at": 1, "run": "r1", "mode": "fresh",
         "execution_mode": "auto"},
        {"kind": "message", "at": 2, "run": "r1", "label": "Make the change",
         "text": "It is also called from cron", "state": "delivered", "delivered": 1},
        {"kind": "revision", "at": 3, "run": "r1", "target_label": "Work out the change",
         "number": 1, "feedback": "Keep it to one report"},
        {"kind": "run.end", "at": 4, "run": "r1", "status": "cancelled",
         "message": "parked"},
        {"kind": "run.start", "at": 5, "run": "r2", "mode": "resume", "resume_count": 1},
        {"kind": "note", "at": 6, "text": "Ask the analyst"}))
    text = plain(page)
    assert "Run 1 started - fresh - Auto mode" in text
    assert 'To "Make the change": "It is also called from cron" - delivered' in text
    assert 'Sent "Work out the change" back (round 1): "Keep it to one report"' in text
    assert "Run 1 parked - Resume continues it" in text
    assert "Run 2 resumed (time 1)" in text
    assert "Note: Ask the analyst" in text
    assert text.index("Run 1 started") < text.index("Note: Ask the analyst")


def test_an_answer_unfolds_where_it_is_and_the_toggle_says_which_entry():
    agent = {"kind": "agent", "at": 1, "run": "r1", "label": "Work out the change",
             "status": "success", "summary": "Two packages. " + "x" * 400,
             "risk": "medium", "issues": [{"severity": "low", "description": "y",
                                           "file": "a.py", "line": 3}],
             "cost_usd": 0.61, "effort": "high"}
    folded, actions = story.history_html(answer(agent))
    assert "risk medium, 1 issue." in plain(folded)
    assert "[low] y (a.py:3)" not in plain(folded)
    assert {"do": "toggle", "index": 0} in actions

    opened, _actions = story.history_html(answer(agent), opened={0})
    assert "[low] y (a.py:3)" in plain(opened)
    assert "$0.61" in plain(opened) and "high effort" in plain(opened)


def test_a_question_shows_what_was_asked_when_the_run_kept_it_and_says_when_not():
    kept = {"kind": "ask", "at": 1, "run": "r1", "label": "Agree to the plan",
            "status": "success", "answer": "Start work", "who": "ann", "kept": True,
            "answered": True, "question": "Start work on QA-1?",
            "detail": "The plan in full.", "options": ["Start work", "Leave it"]}
    page, actions = story.history_html(answer(kept))
    text = plain(page)
    assert "Agree to the plan - Start work on QA-1?" in text
    assert "Start work / Leave it" in text and 'Answered "Start work" by ann' in text
    assert {"do": "toggle", "index": 0} in actions
    assert "The plan in full." in plain(story.history_html(answer(kept), opened={0})[0])

    old = dict(kept, kept=False, question="", detail="", options=[])
    assert "did not record the question" in plain(story.history_html(answer(old))[0])


def test_a_plan_sent_back_reads_as_what_the_person_said():
    sent = {"kind": "ask", "at": 1, "run": "r1", "label": "Agree to the plan",
            "kept": True, "revision": True, "feedback": "Smaller, please", "who": "ann",
            "question": "Start work?", "detail": "v1"}
    assert 'Sent back by ann: "Smaller, please"' in plain(story.history_html(answer(sent))[0])
    stopped = dict(sent, revision=False, feedback="", status="cancelled",
                   message="the run was stopped")
    assert "not answered - the run was stopped" in plain(story.history_html(answer(stopped))[0])


def test_what_came_before_is_offered_once_and_counted():
    page, actions = story.history_html(answer(
        {"kind": "note", "at": 1, "text": "x"}, earlier=1))
    assert "Show 1 earlier entry" in plain(page)
    assert actions[0] == {"do": "earlier"}
    assert "Show 5 earlier entries" in plain(story.history_html(answer(earlier=5))[0])


def test_an_empty_history_says_nothing_has_happened():
    assert "Nothing has happened here yet." in plain(story.history_html(answer())[0])


def test_a_failure_links_its_files_and_every_link_says_what_it_does():
    failed = {"kind": "failed", "at": 1, "run": "r1", "run_dir": "/runs/r1",
              "label": "Tests", "message": "exited 1",
              "files": [{"name": "stdout.log", "path": "steps/tests/stdout.log"}]}
    page, actions = story.history_html(answer(failed))
    assert "Tests - exited 1" in plain(page)
    assert actions == [{"do": "file", "run_dir": "/runs/r1",
                        "path": "steps/tests/stdout.log"}]
    assert "href='a:0'" in page


def test_the_files_and_the_runs_are_listed_newest_first_and_open():
    told = answer(runs=[{"run": "r2", "cycle": "dev", "cycle_name": "Development",
                         "run_dir": "/runs/r2", "started_at": 2, "status": "success"},
                        {"run": "r1", "cycle": "dev", "cycle_name": "Development",
                         "run_dir": "/runs/r1", "started_at": 1, "status": "cancelled",
                         "message": "parked"}])
    told["files"] = [{"run": "r2", "run_dir": "/runs/r2", "started_at": 2, "steps": [
        {"step": "report", "label": "Save the report",
         "files": [{"name": "run.html", "path": "reports/run.html"}]}]}]
    files, file_actions = story.files_html(told)
    assert "Run 2" in plain(files) and "Save the report" in plain(files)
    assert file_actions == [{"do": "file", "run_dir": "/runs/r2", "path": "reports/run.html"}]
    runs, run_actions = story.runs_html(told)
    assert plain(runs).index("Run 2") < plain(runs).index("Run 1")
    assert "parked - Resume continues it" in plain(runs)
    assert {"do": "run", "cycle": "dev", "run_dir": "/runs/r1"} in run_actions
    assert {"do": "files", "run_dir": "/runs/r1"} in run_actions


def test_nothing_to_list_is_said_in_words():
    assert "No step of these runs kept a file." in story.files_html({"files": []})[0]
    assert "No run is left of this subject." in story.runs_html({"runs": []})[0]


def test_links_take_the_theme_s_accent():
    assert "%s" in story.LINK_CSS and "a {" in story.LINK_CSS


# ------------------------------------------------------------ putting a run aside
def live(**steps):
    return {"graph": {"nodes": [{"id": name, "label": name.capitalize()} for name in steps]},
            "steps": {name: {"status": status, "plugin": plugin}
                      for name, (status, plugin) in steps.items()}}


def test_a_run_asking_only_a_person_can_be_parked_at_once():
    asking = live(plan=("success", "agent.review"), approve=("running", "approval.gate"))
    assert story.park_point(asking)
    assert story.park_point(dict(live(work=("running", "agent.implement")), paused=True))


def test_a_run_with_an_agent_mid_call_waits_for_it():
    busy = live(approve=("running", "approval.gate"), work=("running", "agent.implement"))
    assert not story.park_point(busy)
    assert story.working(busy) == ["Work"]


def test_park_says_what_stays_what_waits_and_what_finishes_first():
    lines = story.park_lines(live(plan=("success", "agent.review"),
                                  approve=("running", "approval.gate"),
                                  work=("running", "agent.implement")))
    assert lines[0] == "1 finished step stays finished."
    assert lines[1] == '"Approve" is waiting for you - it asks again when you resume.'
    assert lines[2] == '"Work" finishes what it is doing first.'
    assert "Resume it from Subjects" in lines[-1]


def test_a_parked_subject_names_the_step_resume_goes_on_at():
    parked = subject("parked", latest={"id": "r1", "reached_label": "Slow work"},
                     resume_at="Agree to go on")
    assert story.state_text(parked) == "Parked - resume at Agree to go on"


def test_an_agent_s_account_is_shown_under_it_call_by_call():
    agent = {"kind": "agent", "at": 1, "run": "r1", "step": "plan", "run_dir": "/runs/r1",
             "label": "Work out the change", "status": "success", "summary": "Two."}
    page, actions = story.history_html(answer(agent))
    assert {"do": "transcript", "index": 0, "run": "r1", "step": "plan"} in actions

    waiting = story.history_html(answer(agent), told={0}, transcripts={("r1", "plan"): None})[0]
    assert "Reading its account..." in plain(waiting)

    told = {"cut": 2, "calls": [{"prompt": "steps/plan/operations/abc/prompt.md", "stages": [
        {"kind": "thinking", "detail": "Which field?"},
        {"kind": "tool", "title": "Read a.py"},
        {"kind": "text", "detail": "The resolution field."}]}]}
    page, actions = story.history_html(answer(agent), told={0},
                                       transcripts={("r1", "plan"): told})
    text = plain(page)
    assert "2 earlier stages left out." in text and "Call 1" in text
    assert "Thinking: Which field?" in text and "Used Read a.py" in text
    assert "Said: The resolution field." in text
    assert {"do": "file", "run_dir": "/runs/r1",
            "path": "steps/plan/operations/abc/prompt.md"} in actions


def test_a_long_message_is_cut_to_a_line_not_poured_into_the_history():
    blob = "accept failed: agent exited 1: " + '{"usage": "%s"}' % ("x" * 900)
    page = story.history_html(answer({"kind": "run.end", "at": 1, "run": "r1",
                                      "status": "failed", "message": blob}))[0]
    line = plain(page)
    assert "accept failed: agent exited 1:" in line and line.endswith("...")
    assert len(line) < 260
