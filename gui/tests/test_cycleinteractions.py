"""Talking to a running cycle from the Stages tab.

Three things are defended here. The recipient is only ever one the core
offered, and nothing but the person moves it. A message is one row however
many times its state changes, and that row says what actually became of it.
And the whole way round - composer, the control pipe, the run's scheduler, a
real ``agent.implement`` step, the event stream, back into the row - works
against the real core, with a fake Claude CLI in place of a paid one.
"""

import json
import os
import queue
import stat
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import cyclereplay                                   # noqa: E402
from cms_gui.cycleinteractions import (RUN_RECIPIENT,             # noqa: E402
                                       InteractionComposer)
from cms_gui.runner import RunState                               # noqa: E402
from cms_gui.stages import InteractionRow, StageList               # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GRAPH = {"id": "demo", "name": "Demo", "nodes": [
    {"id": "impl", "label": "Make the change", "plugin": "agent.implement",
     "needs": [], "layer": 0, "row": 0},
    {"id": "commit", "label": "Commit", "plugin": "git.commit",
     "needs": ["impl"], "layer": 1, "row": 0}], "edges": []}


def started(run_id="r"):
    state = RunState()
    state.handle({"kind": "cycle.run.start", "run_id": run_id, "cycle": "demo",
                  "name": "Demo", "graph": GRAPH, "jobs": 1, "variables": {}})
    return state


def offers(state, run_id="r", **steps):
    state.handle({"kind": "cycle.interaction.offers", "run_id": run_id, "version": 3,
                  "run": {"kinds": ["text"], "when": "every later step"} if steps else None,
                  "steps": {name: dict({"step": name, "execution": 1, "kinds": ["text"],
                                        "state": "running", "when": "Before its next call"},
                                       **extra)
                            for name, extra in steps.items()}})


def message(message_id="m1", state="pending", run_id="r", **extra):
    record = {"id": message_id, "run_id": run_id, "seq": 1, "scope": "step",
              "step": "impl", "execution": 1, "kind": "text",
              "payload": {"text": "Also called from cron"}, "who": "tester",
              "accepted_at": 1000.0, "state": state, "reason": "", "deliveries": []}
    record.update(extra)
    return record


def said(state, record):
    state.handle({"kind": "cycle.interaction", "run_id": record["run_id"],
                  "message": record})


def rows(state):
    return [stage for _step, stage in state.cycle["stages"]
            if stage.get("kind") == "interaction"]


# --------------------------------------------------------------------- model
def test_a_message_is_one_row_however_often_its_state_changes(qapp):
    state = started()
    said(state, message(state="pending"))
    first = rows(state)[0]
    said(state, message(state="delivered", deliveries=[
        {"step": "impl", "execution": 1, "point": "attempt-2", "state": "delivered"}]))
    assert len(rows(state)) == 1
    # The very same entry, updated, so the list redraws the row in place.
    assert rows(state)[0] is first and first["body"]["state"] == "delivered"
    assert list(state.cycle["interactions"]) == ["m1"]


def test_a_message_for_another_run_is_not_this_run_s(qapp):
    state = started()
    said(state, message(run_id="elsewhere"))
    assert rows(state) == [] and state.cycle["interactions"] == {}


def test_sent_is_not_taken_until_the_run_says_so(qapp):
    state = started()
    state.interaction_sending({"id": "m1", "run_id": "r", "scope": "step",
                               "step": "impl", "execution": 1, "kind": "text",
                               "payload": {"text": "x"}})
    assert state.cycle["interactions"]["m1"]["state"] == "sending"
    state.handle({"kind": "cycle.interaction.refused", "run_id": "r", "id": "m1",
                  "reason": "The run is finishing; it no longer takes messages."})
    assert state.cycle["interactions"]["m1"]["state"] == "rejected"
    assert "finishing" in state.cycle["interactions"]["m1"]["reason"]


def test_a_refused_retry_does_not_overwrite_the_message_that_was_taken(qapp):
    state = started()
    said(state, message(state="delivered"))
    state.handle({"kind": "cycle.interaction.refused", "run_id": "r", "id": "m1",
                  "reason": "already sent with different content"})
    record = state.cycle["interactions"]["m1"]
    assert record["state"] == "delivered" and "different content" in record["request_error"]


def test_when_the_run_ends_nobody_can_be_written_to_any_more(qapp):
    state = started()
    offers(state, impl={})
    state.interaction_sending({"id": "m2", "run_id": "r", "scope": "run",
                               "kind": "text", "payload": {"text": "x"}})
    state.handle({"kind": "cycle.run.end", "run_id": "r", "status": "success"})
    assert state.cycle["interaction_offers"] == {}
    assert state.cycle["interactions"]["m2"]["state"] == "unconfirmed"


# ------------------------------------------------------------------ composer
IMPL, COMMIT, RUN = ("step", "impl", 1), ("step", "commit", 1), ("run", "", None)
NAMES = {"impl": "Make the change", "commit": "Commit"}


@pytest.fixture
def composer(qapp):
    made = InteractionComposer()
    yield made
    made.deleteLater()


def shown(composer, state, live=True, selected=""):
    composer.set_context(state.cycle, live, NAMES, selected=selected)
    return composer


def write(composer, text="Also called from cron"):
    composer.text.setPlainText(text)


def enter(widget, modifiers=None):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    QTest.keyClick(widget, Qt.Key_Return, modifiers or Qt.NoModifier)


def sent_from(composer):
    """Send the draft the way a person does: Enter in the text box."""
    sent = []
    composer.submitted.connect(sent.append)
    enter(composer.text)
    return sent


def test_with_no_run_there_is_nothing_to_write_to(composer):
    assert composer.text.isHidden() and not composer.can_send()
    assert "while it runs" in composer.closed.text()


def test_the_step_that_is_working_is_the_recipient_without_being_asked(composer):
    """What somebody watching a run means when they start typing, nearly
    every time - and said to be automatic, so it is not taken for a choice."""
    state = started()
    offers(state, impl={}, commit={"state": "pending"})
    shown(composer, state)
    assert composer.current_key() == IMPL and composer.is_automatic()
    assert composer.recipient.text() == "Make the change"
    assert not composer.mode.isHidden() and composer.automatic.isHidden()
    assert composer.when.text() == "Before its next call"
    write(composer)
    assert composer.can_send()


def test_with_nobody_working_the_rest_of_the_run_is_told(composer):
    state = started()
    offers(state, commit={"state": "pending"})
    shown(composer, state)
    assert composer.current_key() == RUN
    assert composer.recipient.text() == RUN_RECIPIENT
    assert composer.when.text().startswith("No step is working now.")


def test_with_two_steps_working_it_asks_rather_than_guesses(composer):
    state = started()
    offers(state, impl={}, commit={})
    shown(composer, state)
    assert composer.current_key() is None
    assert composer.recipient.property("attention") == "true"
    assert "Choose one of 2" in composer.recipient.text()
    write(composer)
    assert not composer.can_send()
    # Selecting one of them on the graph settles it...
    shown(composer, state, selected="commit")
    assert composer.current_key() == COMMIT and composer.can_send()


def test_picking_from_the_list_settles_it_too(composer):
    state = started()
    offers(state, impl={}, commit={})
    shown(composer, state)
    assert composer.choose(IMPL)
    assert composer.current_key() == IMPL and not composer.is_automatic()
    assert composer.mode.isHidden() and not composer.automatic.isHidden()
    composer.automatic.click()
    assert composer.is_automatic() and composer.current_key() is None


def test_the_list_offers_what_the_core_offers_and_nothing_else(composer):
    state = started()
    offers(state, impl={}, commit={"state": "pending"})
    shown(composer, state)
    assert composer.recipients() == ["Make the change", "Commit (not started)",
                                     RUN_RECIPIENT]


def test_a_draft_keeps_its_recipient_when_the_work_moves_on(composer):
    """Begun for the step that was working; that step finishing and the next
    one starting must not carry the text to the next one."""
    state = started()
    offers(state, impl={}, commit={"state": "pending"})
    shown(composer, state)
    write(composer)
    offers(state, commit={})
    shown(composer, state)
    assert composer.current_key() == IMPL
    assert "no longer takes messages" in composer.when.text()
    assert not composer.can_send()
    assert composer.text.toPlainText() == "Also called from cron"
    # Moving it on is offered in one click, and is the person's own act.
    assert composer.automatic.text() == "Send to Commit instead"
    composer.automatic.click()
    assert composer.current_key() == COMMIT and composer.can_send()


def test_an_empty_draft_follows_whoever_is_working(composer):
    state = started()
    offers(state, impl={}, commit={"state": "pending"})
    shown(composer, state)
    offers(state, commit={})
    shown(composer, state)
    assert composer.current_key() == COMMIT


def test_a_draft_begun_before_anyone_could_be_named_keeps_the_first_it_gets(composer):
    """Typed while two steps were working; one finishes, which names the
    other. From then on that is the draft's recipient, whoever starts next."""
    state = started()
    offers(state, impl={}, commit={})
    shown(composer, state)
    write(composer)
    offers(state, commit={})
    shown(composer, state)
    assert composer.current_key() == COMMIT
    offers(state, impl={"execution": 2})
    shown(composer, state)
    assert composer.current_key() == COMMIT
    assert "no longer takes messages" in composer.when.text()


def test_selecting_another_node_while_typing_does_not_move_the_draft(composer):
    state = started()
    offers(state, impl={}, commit={})
    shown(composer, state, selected="impl")
    write(composer)
    shown(composer, state, selected="commit")
    assert composer.current_key() == IMPL


def test_a_new_stage_does_not_move_it_either(composer):
    state = started()
    offers(state, impl={}, commit={"state": "pending"})
    shown(composer, state)
    state.handle({"kind": "cycle.step.stage", "run_id": "r", "step": "commit",
                  "phase": "tool", "title": "Commit", "status": "done"})
    shown(composer, state)
    assert composer.current_key() == IMPL


def test_sending_says_who_what_and_to_which_execution(composer):
    state = started()
    offers(state, impl={})
    shown(composer, state)
    write(composer)
    [request] = sent_from(composer)
    assert request["command"] == "cycle.message" and request["run_id"] == "r"
    assert (request["scope"], request["step"], request["execution"]) == IMPL
    assert request["payload"] == {"text": "Also called from cron"}
    assert request["offer"] == 3 and request["kind"] == "text"
    assert "who" in request and request["id"]
    # Locked while it is on its way; sending again is the same message.
    assert composer.text.isReadOnly() and not composer.recipient.isEnabled()
    sent = []
    composer.submitted.connect(sent.append)
    enter(composer.text)
    assert sent[0]["id"] == request["id"]
    assert "Enter sends it again" in composer.note.text()


def test_there_is_no_send_button_enter_sends_and_shift_enter_breaks_a_line(composer):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPushButton
    state = started()
    offers(state, impl={})
    shown(composer, state)
    assert not [one for one in composer.findChildren(QPushButton)
                if one.text() in ("Send", "Send clarification")]
    write(composer, "first line")
    composer.text.moveCursor(composer.text.textCursor().MoveOperation.End)
    sent = []
    composer.submitted.connect(sent.append)
    enter(composer.text, Qt.ShiftModifier)
    assert sent == [] and "first line" in composer.text.toPlainText()
    assert composer.text.toPlainText() == "first line\n"
    enter(composer.text)
    assert len(sent) == 1
    assert "Enter sends" in composer.text.placeholderText()


def test_ctrl_enter_is_left_to_the_window_where_it_means_run(composer):
    from PySide6.QtCore import Qt
    state = started()
    offers(state, impl={})
    shown(composer, state)
    write(composer)
    sent = []
    composer.submitted.connect(sent.append)
    enter(composer.text, Qt.ControlModifier)
    assert sent == []


def test_once_the_run_has_saved_it_the_draft_is_cleared(composer):
    state = started()
    offers(state, impl={})
    shown(composer, state)
    write(composer)
    [request] = sent_from(composer)
    said(state, message(request["id"], state="pending"))
    shown(composer, state)
    assert composer.text.toPlainText() == "" and not composer.text.isReadOnly()
    assert composer.note.text().startswith("Sent to Make the change.")
    assert composer.is_automatic()


def test_a_refused_message_keeps_its_draft_and_says_why(composer):
    state = started()
    offers(state, impl={})
    shown(composer, state)
    write(composer)
    [request] = sent_from(composer)
    said(state, message(request["id"], state="rejected",
                        reason="impl has no point left at which it could read a message."))
    shown(composer, state)
    assert composer.text.toPlainText() == "Also called from cron"
    assert "no point left" in composer.note.text()
    assert composer.note.property("bad") == "true"


def test_the_text_box_grows_with_what_is_written_and_then_stops(composer):
    state = started()
    offers(state, impl={})
    shown(composer, state)
    one = composer.text.height()
    write(composer, "one\ntwo\nthree")
    three = composer.text.height()
    write(composer, "\n".join("line %d" % number for number in range(12)))
    assert one < three < composer.text.height()
    # Four lines at most; past that it scrolls rather than pushing the feed away.
    assert composer.text.height() < 6 * composer.text.fontMetrics().lineSpacing()


def test_the_list_is_not_rebuilt_when_nothing_about_it_changed(composer, monkeypatch):
    """A running cycle repaints several times a second; a menu rebuilt each
    time would close under the hand of somebody choosing from it."""
    state = started()
    offers(state, impl={})
    shown(composer, state)
    cleared = []
    monkeypatch.setattr(composer.recipient.menu(), "clear", lambda: cleared.append(1))
    for _ in range(5):
        state.handle({"kind": "cycle.step.log", "run_id": "r", "step": "impl",
                      "stream": "out", "lines": ["working"]})
        shown(composer, state)
    assert cleared == []


def test_a_finished_run_is_read_not_written_to(composer):
    state = started()
    offers(state, impl={})
    shown(composer, state, live=False)
    assert composer.text.isHidden() and not composer.can_send()
    assert "over" in composer.closed.text()


def test_each_run_keeps_its_own_draft(composer):
    first, second = started("r"), started("r2")
    offers(first, impl={})
    shown(composer, first)
    write(composer, "for the first run")
    shown(composer, second)
    assert composer.text.toPlainText() == ""
    shown(composer, first)
    assert composer.text.toPlainText() == "for the first run"


# ---------------------------------------------------------------- the row
def test_the_row_says_what_became_of_the_message_and_is_redrawn_in_place(qapp):
    state = started()
    said(state, message(state="pending"))
    stages = StageList()
    stages.set_stages(state.cycle["stages"], named=True, labels={"impl": "Make the change"})
    [row] = [one for one in stages._rows if isinstance(one, InteractionRow)]
    assert row.title.text() == "to Make the change"
    assert row.meta.text().startswith("tester")
    assert row.state.text() == "WAITING"
    said(state, message(state="delivered", deliveries=[
        {"step": "impl", "execution": 1, "point": "attempt-2", "state": "delivered"}]))
    stages.set_stages(state.cycle["stages"], named=True, labels={"impl": "Make the change"})
    assert [one for one in stages._rows if isinstance(one, InteractionRow)] == [row]
    assert row.state.text() == "DELIVERED" and row.card.property("state") == "delivered"
    assert row.delivery.text() == "Read at attempt-2."
    # What "delivered" means is said once, on the pill - not under every row.
    assert "not that what it asks for was done" in row.state.toolTip()
    stages.deleteLater()


def test_a_message_to_the_run_says_who_has_read_it_not_that_it_is_done(qapp):
    row = InteractionRow({"kind": "interaction", "body": message(
        scope="run", step="", execution=None, state="delivered", deliveries=[
            {"step": "impl", "execution": 1, "point": "attempt-2", "state": "delivered"},
            {"step": "commit", "execution": 1, "point": "", "state": "undelivered"}])},
        labels=NAMES)
    assert row.state.text() == "READ BY 1"
    assert "Read by Make the change (attempt-2)." in row.delivery.text()
    assert "Not read by Commit" in row.delivery.text()
    row.deleteLater()


def test_a_message_that_did_not_arrive_is_marked_as_one(qapp):
    from cms_gui import stages as stages_mod
    row = InteractionRow({"kind": "interaction", "body": message(
        state="undelivered", reason="impl ended (success) before another point.")})
    assert stages_mod.colour_of(row.marker._stage) == stages_mod.ink("BAD")
    assert row.state.text() == "NOT DELIVERED"
    assert row.delivery.property("bad") == "true"
    assert "ended (success)" in row.delivery.text()
    row.deleteLater()


def test_what_somebody_wrote_is_shown_as_text_never_as_markup(qapp):
    row = InteractionRow({"kind": "interaction", "body": message(
        payload={"text": "<b>bold</b> & <a href='x'>link</a>"})})
    assert row.detail.text() == "<b>bold</b> & <a href='x'>link</a>"
    from PySide6.QtCore import Qt
    assert row.detail.textFormat() == Qt.PlainText
    row.deleteLater()


def test_the_feed_leaves_a_message_card_its_own_colours(qapp):
    """The feed's ground was set on its body unscoped, which reached every
    row inside and painted the cards and their pills in the page colour."""
    stages = StageList()
    assert stages._body.styleSheet().startswith("#")
    stages.deleteLater()


# ---------------------------------------------------------------- history
def test_a_restored_run_takes_its_messages_from_the_saved_record(tmp_path, qapp):
    """The events are diagnostics and may have missed one; the record did not."""
    run_dir = tmp_path / "run"
    (run_dir / "logs").mkdir(parents=True)
    (run_dir / ".execution").mkdir()
    lines = [{"kind": "cycle.run.start", "run_id": "r", "cycle": "demo", "name": "Demo",
              "graph": GRAPH, "jobs": 1, "variables": {}},
             {"kind": "cycle.interaction.offers", "run_id": "r", "version": 1,
              "run": None, "steps": {"impl": {"execution": 1, "kinds": ["text"]}}},
             {"kind": "cycle.interaction", "run_id": "r",
              "message": message("m1", state="pending")}]
    (run_dir / "logs" / "cycle.jsonl").write_text(
        "".join(json.dumps(one) + "\n" for one in lines))
    (run_dir / ".execution" / "interactions.json").write_text(json.dumps({
        "schema": 1, "run_id": "r", "seq": 2, "snapshots": [],
        "messages": [message("m1", state="delivered"),
                     message("m2", seq=2, state="undelivered", scope="run", step="")]}))
    state = RunState()
    assert cyclereplay.restore(str(run_dir), state)
    assert {one: m["state"] for one, m in state.cycle["interactions"].items()} == {
        "m1": "delivered", "m2": "undelivered"}
    assert len(rows(state)) == 2
    # History is read, never written to.
    assert state.cycle["interaction_offers"] == {}


# ------------------------------------------------------ the whole way round
CORE_PYTHON = os.path.join(ROOT, ".venv", "bin", "python")


def _fake_claude(where, python):
    """A Claude CLI that holds its first call open until told, so a message is
    certain to arrive while an attempt is running."""
    path = os.path.join(where, "claude")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!%s\n" % python + r'''
import json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
if "auth" in sys.argv:
    print(json.dumps({"loggedIn": True, "email": "a@example.invalid",
                      "subscriptionType": "pro"}))
    sys.exit(0)
counter = os.path.join(HERE, "turns")
turn = int(open(counter).read()) if os.path.exists(counter) else 0
open(counter, "w").write(str(turn + 1))
open(os.path.join(HERE, "prompt-%d.txt" % turn), "w").write(
    sys.argv[sys.argv.index("-p") + 1])
if turn == 0:
    open(os.path.join(HERE, "started"), "w").write("1")
    for _ in range(1200):
        if os.path.exists(os.path.join(HERE, "go")):
            break
        time.sleep(0.05)
body = "raise SystemExit('not yet')" if turn == 0 else "print('ok')"
open(os.path.join(HERE, "project", "main.py"), "w").write(body + "\n")
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "Wrote main.py", "total_cost_usd": 0.01}), flush=True)
''')
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


@pytest.mark.skipif(not os.path.isfile(CORE_PYTHON) or os.name == "nt",
                    reason="needs the core's own environment")
def test_a_clarification_goes_from_the_composer_into_the_next_attempt(tmp_path, qapp):
    """GUI -> control pipe -> the run's scheduler -> agent.implement -> events
    -> the row. The first attempt is held open by the fake CLI until the
    message has been taken, so the second attempt is the one that must hear it."""
    where = str(tmp_path)
    os.makedirs(os.path.join(where, "project"))
    os.makedirs(os.path.join(where, "cycles"))
    claude = _fake_claude(where, CORE_PYTHON)
    with open(os.path.join(where, "cycles", "e2e.yaml"), "w", encoding="utf-8") as handle:
        handle.write("id: e2e\nname: E2E\nsteps:\n  - id: impl\n"
                     "    plugin: agent.implement\n    with:\n"
                     "      claude: %s\n      model: sonnet\n      directory: %s\n"
                     "      task: Make main.py print ok\n      checks: %s main.py\n"
                     "      review: false\n"
                     % (claude, os.path.join(where, "project"), CORE_PYTHON))
    core = subprocess.Popen(
        [CORE_PYTHON, os.path.join(ROOT, "session_launcher.py"), "--cycle-run=e2e",
         "--cycles-dir=" + os.path.join(where, "cycles"), "--events=-", "--control=-"],
        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=dict(os.environ, CMS_HOME=os.path.join(where, "home")), text=True)
    lines = queue.Queue()
    threading.Thread(target=lambda: [lines.put(one) for one in core.stdout],
                     daemon=True).start()
    state = RunState()
    composer = InteractionComposer()
    composer.submitted.connect(lambda request: (
        state.interaction_sending(request),
        core.stdin.write(json.dumps(request) + "\n"), core.stdin.flush()))

    def pump(until, seconds=60):
        deadline = time.monotonic() + seconds
        while not until():
            assert time.monotonic() < deadline, "the run never got there"
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                assert core.poll() is None or not lines.empty(), "the core exited"
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                state.handle(event)

    try:
        pump(lambda: os.path.exists(os.path.join(where, "started"))
             and "impl" in ((state.cycle or {}).get("interaction_offers") or {})
             .get("steps", {}))
        composer.set_context(state.cycle, state.cycle_running, {})
        # Nobody chose it: the one step working is who it goes to.
        assert composer.current_key() == ("step", "impl", 1) and composer.is_automatic()
        composer.text.setPlainText("Also called from cron")
        enter(composer.text)
        pump(lambda: any(one.get("state") == "pending"
                         for one in state.cycle["interactions"].values()))
        composer.set_context(state.cycle, state.cycle_running, {})
        assert composer.text.toPlainText() == ""
        open(os.path.join(where, "go"), "w").close()
        pump(lambda: not state.cycle_running and state.cycle.get("status"))
    finally:
        core.stdin.close()
        core.wait(timeout=30)

    assert state.cycle["status"] == "success"
    [record] = state.cycle["interactions"].values()
    assert record["state"] == "delivered"
    assert [(one["point"], one["state"]) for one in record["deliveries"]] == [
        ("attempt-2", "delivered")]
    with open(os.path.join(where, "prompt-1.txt"), encoding="utf-8") as handle:
        assert "Also called from cron" in handle.read()
    with open(os.path.join(where, "prompt-0.txt"), encoding="utf-8") as handle:
        assert "Also called from cron" not in handle.read()
    stages = StageList()
    stages.set_stages(state.cycle["stages"], named=True)
    [row] = [one for one in stages._rows if isinstance(one, InteractionRow)]
    assert "attempt-2" in row.delivery.text()
    stages.deleteLater()
    composer.deleteLater()


# -------------------------------------------------------- from any window
def test_the_popup_is_the_composer_over_whatever_window_is_in_front(qapp):
    from cms_gui.cycleinteractions import SHORTCUT, ClarifyPopup
    assert SHORTCUT == "Ctrl+Shift+M"
    state = started()
    offers(state, impl={})
    popup = ClarifyPopup()
    popup.set_context(state.cycle, True, NAMES)
    popup.open_over()
    assert popup.isVisible() and popup.composer.current_key() == IMPL
    assert "Demo" in popup.run.text()
    sent, told = [], []
    popup.submitted.connect(sent.append)
    popup.sent.connect(told.append)
    write(popup.composer)
    enter(popup.composer.text)
    # On its way: still open, so a refusal would have somewhere to be said.
    assert len(sent) == 1 and popup.isVisible()
    said(state, message(sent[0]["id"], state="pending"))
    popup.set_context(state.cycle, True, NAMES)
    assert not popup.isVisible() and told == ["Clarification sent to Make the change"]
    popup.deleteLater()


def test_a_refused_message_keeps_the_popup_open_with_the_reason(qapp):
    from cms_gui.cycleinteractions import ClarifyPopup
    state = started()
    offers(state, impl={})
    popup = ClarifyPopup()
    popup.set_context(state.cycle, True, NAMES)
    popup.open_over()
    sent = []
    popup.submitted.connect(sent.append)
    write(popup.composer)
    enter(popup.composer.text)
    said(state, message(sent[0]["id"], state="rejected", reason="impl has finished."))
    popup.set_context(state.cycle, True, NAMES)
    assert popup.isVisible() and "impl has finished." in popup.composer.note.text()
    popup.deleteLater()


def test_escape_closes_the_popup_and_the_draft_waits_for_next_time(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from cms_gui.cycleinteractions import ClarifyPopup
    state = started()
    offers(state, impl={})
    popup = ClarifyPopup()
    popup.set_context(state.cycle, True, NAMES)
    popup.open_over()
    write(popup.composer, "half written")
    QTest.keyClick(popup, Qt.Key_Escape)
    assert not popup.isVisible()
    popup.open_over()
    assert popup.composer.text.toPlainText() == "half written"
    popup.deleteLater()


def test_a_row_says_which_later_steps_were_shown_what_a_step_was_told(qapp):
    row = InteractionRow({"kind": "interaction", "body": message(
        state="delivered", deliveries=[
            {"step": "impl", "execution": 1, "point": "attempt-2", "state": "delivered"},
            {"step": "commit", "execution": 1, "point": "agent", "state": "delivered"}])},
        labels=NAMES)
    assert row.delivery.text() == ("Read at attempt-2. Also shown to Commit, "
                                   "which came after it.")
    row.deleteLater()
