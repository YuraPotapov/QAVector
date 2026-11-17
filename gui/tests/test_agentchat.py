"""Integrations -> Agents -> Chat: a conversation as it is shown and as it is held.

Three promises are tested harder than the rest. Nothing is sent twice. Nothing
typed is lost - a message that could not be said goes back into the box. And
the feed shows the record: an event is a row, a tool's call and its outcome are
one row, and what the agent said is laid out as text, never run as markup.

No agent is asked anything: a turn is a stand-in the test drives by hand.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QObject, QSettings, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog, QWidget

from cms_gui import widgets
from cms_gui.pages import agentchat
from cms_gui.pages.agentchat import AnswerText, ChatFeed, MessageCard
from cms_gui.pages.agents import ConnectionDialog
from cms_gui.pages.integrations import IntegrationsPage
from cms_gui.settings import Settings
from test_agents_page import FakeCore, _connection, _session, _turn

QUESTION = "Why does the login test fail?"
ANSWER = "Because the **session** expires."


def _events(question=QUESTION, answer=ANSWER, first=1, began=1794411900.0):
    found = [
        {"type": "user", "text": question, "mode": "read", "at": began},
        {"type": "started", "model": "claude-sonnet", "provider_session": "p-1"},
        {"type": "thinking", "text": "Let me look at the test."},
        {"type": "tool.started", "id": "t1", "name": "Read", "title": "Read test_login.py",
         "detail": "file_path: /repo/tests/test_login.py"},
        {"type": "file.written", "path": "/repo/notes.md"},
        {"type": "tool.completed", "id": "t1", "ok": True, "detail": "def test_login(): ..."},
        {"type": "text", "text": answer},
        {"type": "completed", "text": answer, "steps": 3, "cost_usd": 0.0412,
         "provider_session": "p-1", "at": began + 42},
    ]
    return [dict(one, seq=first + index, turn=1, at=one.get("at", began + index))
            for index, one in enumerate(found)]


class FakeTurn(QObject):
    """A turn the test takes by hand: what the core would say, when it is told to."""

    event = Signal(dict)
    answered = Signal(dict)
    made = []

    def __init__(self, core, session_id, document, parent=None):
        super().__init__(parent)
        self.core, self.session_id, self.document = core, session_id, dict(document)
        self.started = self.stopped = False
        FakeTurn.made.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def shutdown(self, wait_ms=0):
        self.stopped = True

    def say(self, *events):
        for one in events:
            self.event.emit(dict(one))

    def finish(self, state="completed", error="", phase=""):
        """End the turn as the core would: the record changed, and says how -
        and, after a planning turn, what the conversation waits for."""
        session = self.core._session(self.session_id)
        session["phase"] = phase
        session["turns"] = list(session.get("turns") or []) + [{"n": 1, "state": state}]
        session["title"] = session.get("title") or self.document["message"].splitlines()[0]
        session["mode"] = self.document.get("mode", session.get("mode"))
        session["state"] = state
        answer = {"ok": state == "completed", "session": dict(session),
                  "turn": {"n": len(session["turns"]), "state": state, "error": error}}
        if state != "completed":
            answer.update(kind=state, error=error or "The turn was stopped.")
        self.answered.emit(answer)

    def refuse(self, kind, error):
        """The turn was never started: one line saying why, and no turn in it."""
        self.answered.emit({"ok": False, "kind": kind, "error": error})


@pytest.fixture
def settings(tmp_path):
    made = Settings()
    made._qs = QSettings(str(tmp_path / "gui.ini"), QSettings.IniFormat)
    return made


@pytest.fixture
def make(qapp, settings, dispose, tmp_path):
    pages = []
    FakeTurn.made = []

    def build(core=None, folder=True):
        if folder:
            # The folder a person chose last time, so a message can be sent.
            settings.save_integrations_state({"agents": {"folders": [str(tmp_path)]}})
        page = IntegrationsPage(settings)
        page.agents.chat.turn_factory = FakeTurn
        page.resize(1200, 800)
        page.set_core(core or FakeCore())
        page.open_provider("agents")
        page.show()
        qapp.processEvents()
        pages.append(page)
        return page

    yield build
    for page in pages:
        dispose(page)


def _say(chat, text):
    chat.composer.set_draft(text)
    return chat.composer.send()


# -- the feed ---------------------------------------------------------------------
def test_a_turn_reads_as_what_was_asked_what_was_done_and_what_was_said(qapp):
    feed = ChatFeed()
    feed.load(_events())
    assert feed.kinds() == ["you", "thinking", "tool", "answer", "end"]
    assert feed.said() == [QUESTION, "Thinking", "Read test_login.py", ANSWER, "Finished"]
    end = feed._rows[-1][1]
    assert end.stage["detail"] == "3 steps · est. $0.04 · 42 s"


def test_the_last_line_is_not_said_twice_but_one_that_differs_is_said(qapp):
    feed = ChatFeed()
    feed.load(_events())
    assert feed.kinds().count("answer") == 1
    events = _events()
    events[-1]["text"] = "A summary nobody has read yet."
    feed.load(events)
    assert feed.said()[-3:] == [ANSWER, "A summary nobody has read yet.", "Finished"]


def test_a_tool_s_call_and_its_outcome_are_one_row(qapp):
    feed = ChatFeed()
    events = _events()
    for one in events[:4]:
        feed.append(one)
    row = feed._rows[2][1]
    assert row.stage["status"] == "running"
    feed.append(events[5])
    assert len(feed._rows) == 3
    assert feed._rows[2][1].stage["status"] == "done"
    assert feed._rows[2][1].stage["detail"] == "file_path: /repo/tests/test_login.py"


def test_a_tool_that_failed_says_what_came_back(qapp):
    feed = ChatFeed()
    events = _events()
    events[5] = dict(events[5], ok=False, detail="No such file")
    feed.load(events[:6])
    stage = feed._rows[2][1].stage
    assert stage["status"] == "failed"
    assert stage["detail"] == "file_path: /repo/tests/test_login.py\nNo such file"


@pytest.mark.parametrize("mode, name, said", [
    ("read", "Bash", "Not allowed in Read: commands need Full access."),
    ("edit", "Bash", "Not allowed in Edit: commands need Full access."),
    ("read", "Write", "Not allowed in Read: changing files needs Edit or Full access."),
    ("full", "Bash", "Not run: the agent program would not run this without approval, "
                     "which a conversation cannot give. This command requires approval"),
])
def test_a_refused_call_says_what_the_mode_allows_not_that_it_awaits_approval(
        qapp, mode, name, said):
    feed = ChatFeed()
    events = _events()
    events[0] = dict(events[0], mode=mode)
    events[3] = dict(events[3], name=name, title="git status", detail="command: git status")
    events[5] = dict(events[5], ok=False, refused=True,
                     reason="This command requires approval",
                     detail="This command requires approval")
    feed.load(events[:6])
    stage = feed._rows[2][1].stage
    # A caution, not a failure: nothing broke, the conversation did not allow it.
    assert (stage["status"], stage["kind"]) == ("done", "caution")
    assert stage["detail"] == "command: git status\n" + said


@pytest.mark.parametrize("end, title, status", [
    ({"type": "failed", "error": "Claude Code ended without an answer (exit 3)."},
     "Failed", "failed"),
    ({"type": "stopped", "error": ""}, "Stopped", None),
])
def test_a_turn_that_did_not_finish_says_how_and_leaves_no_tool_running(qapp, end, title,
                                                                        status):
    feed = ChatFeed()
    feed.load(_events()[:4] + [end])
    assert feed.kinds() == ["you", "thinking", "tool", "end"]
    assert feed._rows[2][1].stage["status"] != "running"
    stage = feed._rows[-1][1].stage
    assert (stage["title"], stage.get("status")) == (title, status)
    if status == "failed":
        assert stage["detail"] == "Claude Code ended without an answer (exit 3)."


def test_what_is_not_on_record_yet_waits_under_what_is(qapp):
    feed = ChatFeed()
    feed.load(_events()[:1])
    feed.set_waiting("on its way", ["second", "third"])
    assert feed.waiting() == [("SENDING", "on its way"), ("QUEUED", "second"),
                              ("QUEUED", "third")]
    feed.append(_events()[2])
    # A new event of the turn going on goes above what is still waiting.
    order = [feed._column.indexOf(widget) for _kind, widget in feed._rows]
    assert max(order) < min(feed._column.indexOf(row) for row in feed._waiting)
    removed = []
    feed.unqueued.connect(removed.append)
    assert feed._waiting[0].body.remove is None        # what is being sent cannot be taken back
    feed._waiting[2].body.remove.click()
    assert removed == [1]
    feed.set_waiting("", [])
    assert feed.waiting() == []


def test_the_line_joins_every_row_and_stops_at_both_ends(qapp):
    feed = ChatFeed()
    feed.load(_events())
    feed.set_waiting("more", [])
    rows = [widget for _kind, widget in feed._rows] + feed._waiting
    assert [row.marker._first for row in rows] == [True] + [False] * (len(rows) - 1)
    assert [row.marker._last for row in rows] == [False] * (len(rows) - 1) + [True]


def test_an_empty_feed_says_so_and_stops_saying_it_at_the_first_row(qapp):
    feed = ChatFeed()
    feed.show()
    qapp.processEvents()
    assert feed._empty.isVisible() and feed._empty.text() == agentchat.NEW
    feed.set_waiting("hello", [])
    assert not feed._empty.isVisible()
    feed.clear()
    assert feed._empty.isVisible() and feed.kinds() == []
    feed.close()


def test_the_feed_follows_the_newest_until_somebody_scrolls_up(qapp):
    feed = ChatFeed()
    feed.resize(500, 200)
    feed.show()
    feed.load(_events() * 6)
    for _pass in range(3):              # the rows are laid out a pass after they go in
        qapp.processEvents()
    bar = feed.verticalScrollBar()
    assert bar.maximum() > 0 and bar.value() == bar.maximum() and feed.at_bottom()
    bar.setValue(0)
    feed.append({"type": "text", "text": "more"})
    qapp.processEvents()
    assert bar.value() == 0 and not feed.at_bottom()
    feed.close()


def test_what_the_agent_said_is_laid_out_and_never_run(qapp):
    text = AnswerText("Because the **session** expires.\n\n<b onclick='x()'>raw</b>\n\n"
                      "![shot](/etc/hostname)\n\n```\ncode here\n```")
    plain = text.toPlainText()
    assert "Because the session expires." in plain          # the emphasis is drawn
    assert "<b onclick='x()'>raw</b>" in plain              # markup is shown as written
    assert "code here" in plain
    assert text.loadResource(2, "/etc/hostname") is None    # an image is never fetched
    assert not text.openLinks() and not text.openExternalLinks()


def test_an_answer_is_as_tall_as_what_it_says(qapp):
    short, long = AnswerText("One line."), AnswerText("\n\n".join(["A paragraph."] * 12))
    for one in (short, long):
        one.resize(400, 10)
        one.show()
    qapp.processEvents()
    assert long.height() > short.height() * 5
    assert short.verticalScrollBarPolicy() == Qt.ScrollBarAlwaysOff
    short.close()
    long.close()


def test_an_answer_keeps_its_first_line_in_sight_when_the_feed_narrows(qapp):
    """Measured as its text alone, the box was a line short under the
    stylesheet's padding and scrolled its own first line away."""
    feed = ChatFeed()
    feed.resize(700, 400)
    feed.show()
    feed.load([{"type": "text", "text": "First line.\n\n" + "A long sentence that wraps. " * 12}])
    answer = feed._rows[0][1].body
    for width in (700, 420, 900):
        feed.resize(width, 400)
        for _pass in range(3):
            qapp.processEvents()
        assert answer.verticalScrollBar().value() == 0
        assert answer.viewport().height() >= int(answer.document().size().height())
        assert answer.viewport().width() == answer.width()
    feed.close()


def test_what_a_person_wrote_is_text_always(qapp):
    card = MessageCard("<b>not bold</b> **nor this**", "10:42")
    assert card.text.textFormat() == Qt.PlainText
    assert card.text.text() == "<b>not bold</b> **nor this**"
    assert card.meta.text() == "10:42" and not card.state.isVisibleTo(card)


# -- the list -----------------------------------------------------------------------
def test_the_tab_lists_the_conversations_and_opens_on_a_new_one(make):
    core = FakeCore(sessions=[_session(), _session("20261110-090000-aaaaaa", "Refund flow")])
    chat = make(core).agents.chat
    assert [chat.table.item(row, 0).text() for row in range(chat.table.rowCount())] == [
        "Flaky login test", "Refund flow"]
    assert chat.current is None and chat.title.text() == "New conversation"
    assert "session" not in core.asked()                # none is read until one is picked
    assert not chat.rename_button.isEnabled() and not chat.delete_button.isEnabled()


def test_no_conversation_yet_says_so_and_the_box_is_ready(make, qapp):
    chat = make(FakeCore()).agents.chat
    assert chat.note.isVisible() and chat.note.text() == "No conversations in this project yet."
    assert chat.feed._empty.isVisible()
    assert chat.composer.isVisible() and chat.composer.text.isEnabled()


def test_picking_a_conversation_reads_it_once_and_shows_it(make):
    session = _session(model="opus", mode="edit")
    core = FakeCore(sessions=[session], events={session["id"]: _events()})
    chat = make(core).agents.chat
    chat.table.selectRow(0)
    assert (("session", session["id"]), {"limit": agentchat.EVENTS_SHOWN}) in core.calls
    assert chat.title.text() == "Flaky login test"
    assert chat.sub.text() == "Claude · /home/someone/src/shop"
    assert chat.feed.kinds() == ["you", "thinking", "tool", "answer", "end"]
    composer = chat.composer
    assert composer.settings() == {"connection": "claude", "folder": "/home/someone/src/shop",
                                   "mode": "edit", "model": "opus",
                                   "effort": "", "plan": False, "others": "deny", "wake": "ask"}
    # With one agent, in one folder: neither changes once it has begun.
    assert not composer.connection.isEnabled() and not composer.folder.isEnabled()
    assert not composer.browse.isEnabled()
    chat.table.selectRow(0)
    assert core.asked().count("session") == 1
    assert chat.rename_button.isEnabled() and chat.delete_button.isEnabled()


def test_the_conversation_that_was_open_is_open_again_next_time(make, settings):
    session = _session()
    core = FakeCore(sessions=[session], events={session["id"]: _events()})
    chat = make(core).agents.chat
    chat.table.selectRow(0)
    assert settings.integrations_state()["agents"]["session"] == session["id"]
    again = make(FakeCore(sessions=[session], events={session["id"]: _events()}),
                 folder=False).agents.chat
    assert again.current["id"] == session["id"]
    assert again.feed.kinds()[0] == "you"


def test_only_the_newest_part_of_a_long_conversation_is_read_until_asked(make):
    session = _session()
    core = FakeCore(sessions=[session], events={session["id"]: _events(first=40)})
    chat = make(core).agents.chat
    chat.table.selectRow(0)
    assert chat.earlier.isVisible()
    chat.earlier.click()
    assert (("session", session["id"]), {"limit": 0}) in core.calls
    assert not chat.earlier.isVisible()


def test_a_search_that_hides_every_conversation_says_so(make, qapp):
    chat = make(FakeCore(sessions=[_session()])).agents.chat
    chat.search.box(0).setText("nothing like it")
    qapp.processEvents()
    assert chat.note.isVisible() and chat.note.text() == "No conversation matches the search."


def test_the_list_has_a_search_box_over_every_column(make, qapp):
    chat = make(FakeCore(sessions=[_session()])).agents.chat
    header = chat.table.horizontalHeader()
    for column in range(chat.table.columnCount()):
        box = chat.search.box(column)
        assert box is not None and box.isVisible()
        assert box.x() == header.sectionViewportPosition(column)
        assert box.width() == header.sectionSize(column) + 1


# -- saying something ---------------------------------------------------------------
def test_the_first_message_makes_the_conversation_and_takes_the_first_turn(make, tmp_path):
    core = FakeCore()
    chat = make(core).agents.chat
    assert _say(chat, QUESTION)
    args, document = next(call for call in core.calls if call[0][0] == "session-new")
    assert document == {"connection": "claude", "folder": str(tmp_path), "mode": "read",
                        "model": "", "effort": "", "plan": False, "others": "deny", "wake": "ask", "restrictions": {},
                        "skills": []}
    (turn,) = FakeTurn.made
    assert turn.started and turn.session_id == chat.current["id"]
    assert turn.document == {"message": QUESTION, "mode": "read", "model": "",
                               "effort": "", "plan": False}
    # Shown at once, as on its way - and the box is free for the next thought.
    assert chat.feed.waiting() == [("SENDING", QUESTION)]
    assert chat.composer.draft() == "" and chat.composer.stop.isVisible()
    assert chat.table.item(0, 1).text() == "Answering"


def test_the_message_is_a_row_once_the_core_has_it_on_record(make):
    chat = make(FakeCore()).agents.chat
    _say(chat, QUESTION)
    turn = FakeTurn.made[0]
    events = _events()
    turn.say(events[0])
    assert chat.feed.waiting() == [] and chat.feed.kinds() == ["you"]
    turn.say(*events[1:])
    assert chat.feed.kinds() == ["you", "thinking", "tool", "answer", "end"]
    turn.finish()
    assert not chat.composer.stop.isVisible() and chat.turns == {}
    assert chat.title.text() == QUESTION                # called by how it began
    assert chat.table.item(0, 0).text() == QUESTION
    assert chat.table.item(0, 1).text() != "Answering"


def test_a_breathing_dot_and_the_turn_s_time_say_the_agent_is_working(make, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(agentchat.time, "monotonic", lambda: clock[0])
    chat = make(FakeCore()).agents.chat
    composer = chat.composer
    assert composer.working_dot.isHidden() and composer.working_text() == ""
    _say(chat, QUESTION)
    assert composer.working_dot.isVisible() and composer.working_dot.running()
    assert composer.working_text() == "Working · 0 s  ·"
    clock[0] += 75
    composer._tick()
    assert composer.working_text() == "Working · 1 min 15 s  ·"
    # Off screen, nothing breathes or ticks; back on it, it goes on.
    composer.hide()
    assert not composer.working_dot.running() and not composer._working_clock.isActive()
    composer.show()
    assert composer.working_dot.running() and composer._working_clock.isActive()
    FakeTurn.made[0].say(*_events())
    FakeTurn.made[0].finish()
    assert composer.working_dot.isHidden() and not composer.working_dot.running()
    assert composer.working_text() == "" and not composer._working_clock.isActive()


def test_enter_sends_and_shift_enter_starts_a_line(make):
    chat = make(FakeCore()).agents.chat
    box = chat.composer.text
    box.setPlainText("first line")
    QTest.keyClick(box, Qt.Key_Return, Qt.ShiftModifier)
    assert FakeTurn.made == [] and "\n" in box.toPlainText()
    QTest.keyClick(box, Qt.Key_Return)
    assert len(FakeTurn.made) == 1 and box.toPlainText() == ""


def test_nothing_typed_is_nothing_sent(make):
    core = FakeCore()
    chat = make(core).agents.chat
    assert not _say(chat, "   \n ")
    assert FakeTurn.made == [] and "session-new" not in core.asked()


def test_without_a_folder_nothing_is_started_and_the_box_says_what_is_missing(make):
    """Never a guess: an agent let loose in the wrong folder is the mistake."""
    core = FakeCore()
    chat = make(core, folder=False).agents.chat
    _say(chat, QUESTION)
    assert chat.composer.note.text() == "Choose the folder the agent works in."
    assert chat.composer.note.property("bad") == "true"
    assert "session-new" not in core.asked() and FakeTurn.made == []
    assert chat.composer.draft() == QUESTION


def test_the_mode_and_model_chosen_go_with_the_turn(make):
    session = _session()
    chat = make(FakeCore(sessions=[session], events={session["id"]: _events()})).agents.chat
    chat.table.selectRow(0)
    chat.composer.mode.set_current("Full")
    chat.composer.model.set_value("opus")
    _say(chat, "run the tests")
    assert FakeTurn.made[0].document == {"message": "run the tests", "mode": "full",
                                         "model": "opus", "effort": "", "plan": False}


def test_a_message_written_while_the_agent_answers_waits_its_turn(make):
    chat = make(FakeCore()).agents.chat
    _say(chat, "first")
    first = FakeTurn.made[0]
    first.say(_events("first")[0])
    _say(chat, "second")
    _say(chat, "third")
    assert len(FakeTurn.made) == 1                       # nothing is sent over a turn
    assert chat.feed.waiting() == [("QUEUED", "second"), ("QUEUED", "third")]
    first.finish()
    assert [turn.document["message"] for turn in FakeTurn.made] == ["first", "second"]
    assert chat.feed.waiting() == [("SENDING", "second"), ("QUEUED", "third")]
    FakeTurn.made[1].say(_events("second", first=10)[0])
    FakeTurn.made[1].finish()
    assert [turn.document["message"] for turn in FakeTurn.made] == ["first", "second", "third"]


@pytest.mark.parametrize("state", ["stopped", "completed"])
def test_interrupt_sends_selected_message_after_turn_ends(make, state):
    chat = make(FakeCore()).agents.chat
    _say(chat, "first")
    first = FakeTurn.made[0]
    first.say(_events("first")[0])
    _say(chat, "later")
    _say(chat, "do not commit")
    chat.feed._waiting[-1].body.interrupt.click()
    assert first.stopped and len(FakeTurn.made) == 1
    first.finish(state)
    assert FakeTurn.made[1].session_id == first.session_id
    assert FakeTurn.made[1].document["message"] == "do not commit"
    assert chat._queued[first.session_id][0]["message"] == "later"


def test_interrupt_from_composer_preserves_files_and_does_not_retry_failure(make, tmp_path):
    chat = make(FakeCore()).agents.chat
    _say(chat, "first")
    first = FakeTurn.made[0]
    first.say(_events("first")[0])
    path = tmp_path / "instructions.txt"
    path.write_text("Do not commit")
    chat.composer.set_draft("use these instructions")
    chat.composer.set_attachments([str(path)])
    chat.composer.interrupt.click()
    assert first.stopped and len(FakeTurn.made) == 1
    first.finish("failed")
    assert len(FakeTurn.made) == 1
    assert chat.composer.draft() == "use these instructions"
    assert chat.composer.attachments() == [str(path)]


def test_stop_cancels_interrupt_resume(make):
    chat = make(FakeCore()).agents.chat
    _say(chat, "first")
    first = FakeTurn.made[0]
    first.say(_events("first")[0])
    _say(chat, "do not commit")
    chat.interrupt_queued(0)
    chat.stop()
    first.finish("stopped")
    assert len(FakeTurn.made) == 1
    assert chat.composer.draft() == "do not commit"


def test_a_queued_message_can_be_taken_back(make):
    chat = make(FakeCore()).agents.chat
    _say(chat, "first")
    FakeTurn.made[0].say(_events("first")[0])
    _say(chat, "second")
    chat.feed._waiting[0].body.remove.click()
    assert chat.feed.waiting() == []
    FakeTurn.made[0].finish()
    assert len(FakeTurn.made) == 1


@pytest.mark.parametrize("state", ["failed", "stopped"])
def test_after_a_turn_that_went_wrong_what_was_waiting_goes_back_into_the_box(make, state):
    """A person decides what is said next to an agent that just surprised them."""
    chat = make(FakeCore()).agents.chat
    _say(chat, "first")
    turn = FakeTurn.made[0]
    turn.say(_events("first")[0])
    _say(chat, "second")
    _say(chat, "third")
    chat.composer.set_draft("and a thought")
    turn.say({"type": state, "error": "boom"})
    turn.finish(state, error="boom")
    assert len(FakeTurn.made) == 1
    assert chat.composer.draft() == "second\n\nthird\n\nand a thought"
    assert chat.feed.waiting() == [] and chat.feed.kinds()[-1] == "end"
    assert not chat.composer.stop.isVisible()


def test_a_first_message_that_could_not_be_said_leaves_no_conversation_behind(make, tmp_path):
    core = FakeCore()
    chat = make(core).agents.chat
    chat.composer.mode.set_current("Edit")
    _say(chat, QUESTION)
    made = chat.current["id"]
    FakeTurn.made[0].refuse("not_installed", "Claude Code is not installed.")
    assert core.calls[-1] == (("session-delete", made), None)
    assert chat.current is None and chat.table.rowCount() == 0
    assert chat.composer.draft() == QUESTION
    assert chat.composer.note.text() == "Claude Code is not installed."
    assert chat.composer.note.property("bad") == "true"
    # What was chosen for it is still chosen.
    assert chat.composer.settings() == {"connection": "claude", "folder": str(tmp_path),
                                        "mode": "edit", "model": "", "effort": "", "plan": False,
                                        "others": "deny", "wake": "ask"}
    assert chat.composer.folder.isEnabled()


def test_a_message_refused_in_a_conversation_already_begun_goes_back_and_it_stays(make):
    session = _session()
    core = FakeCore(sessions=[session], events={session["id"]: _events()})
    chat = make(core).agents.chat
    chat.table.selectRow(0)
    _say(chat, "and then?")
    FakeTurn.made[0].refuse("busy", "The agent is still answering in this session.")
    assert "session-delete" not in core.asked()
    assert chat.current["id"] == session["id"] and chat.table.rowCount() == 1
    assert chat.composer.draft() == "and then?"
    assert chat.composer.note.text() == "The agent is still answering in this session."
    assert chat.feed.waiting() == []


def test_stop_asks_the_turn_to_stop_and_the_feed_says_it_was(make):
    chat = make(FakeCore()).agents.chat
    _say(chat, "a long job")
    turn = FakeTurn.made[0]
    turn.say(*_events("a long job")[:4])
    chat.composer.stop.click()
    assert turn.stopped and not chat.composer.stop.isEnabled()
    turn.say({"type": "stopped", "error": ""})
    turn.finish("stopped")
    assert chat.feed.said()[-1] == "Stopped"
    assert not chat.composer.stop.isVisible()
    # And the conversation goes on.
    _say(chat, "carry on")
    assert FakeTurn.made[1].document["message"] == "carry on"
    assert chat.composer.stop.isEnabled()


# -- more than one conversation ------------------------------------------------------
def test_a_turn_going_on_elsewhere_does_not_write_into_the_one_being_read(make):
    other = _session("20261110-090000-aaaaaa", "Refund flow")
    core = FakeCore(sessions=[other], events={other["id"]: _events("How do refunds work?")})
    page = make(core)
    chat = page.agents.chat
    _say(chat, QUESTION)
    turn = FakeTurn.made[0]
    turn.say(_events()[0])
    chat.table.selectRow(1)                             # read the other one meanwhile
    assert chat.current["id"] == other["id"]
    shown = chat.feed.said()
    turn.say(*_events()[1:])
    assert chat.feed.said() == shown
    assert not chat.composer.stop.isVisible()           # nothing is being answered here
    assert chat.table.item(0, 1).text() == "Answering"
    assert page.busy_reason() == ("An agent is still answering. Stop it, or wait for it "
                                  "to finish.")
    turn.finish()
    assert page.busy_reason() == ""
    assert chat.current["id"] == other["id"]


def test_what_was_typed_stays_with_the_conversation_it_was_typed_in(make):
    one, two = _session(), _session("20261110-090000-aaaaaa", "Refund flow")
    core = FakeCore(sessions=[one, two], events={one["id"]: _events(), two["id"]: _events()})
    page = make(core)
    chat = page.agents.chat
    chat.composer.set_draft("for a new one")
    chat.table.selectRow(0)
    assert chat.composer.draft() == ""
    chat.composer.set_draft("for the first")
    chat.table.selectRow(1)
    assert chat.composer.draft() == ""
    chat.table.selectRow(0)
    assert chat.composer.draft() == "for the first"
    chat.new_button.click()
    assert chat.composer.draft() == "for a new one"
    assert page.has_unsaved() is True
    chat.composer.set_draft("")
    chat.table.selectRow(0)
    chat.composer.set_draft("")
    assert page.has_unsaved() is False


def test_a_conversation_held_by_something_else_is_shown_not_carried_on(make):
    session = _session(source={"label": "Cycle nightly · step review", "detail": "Run 7"})
    core = FakeCore(sessions=[session], events={session["id"]: _events()})
    chat = make(core).agents.chat
    chat.table.selectRow(0)
    composer = chat.composer
    assert composer.closed.isVisible() and not composer.text.isVisible()
    assert composer.closed.text() == ("This conversation was held by Cycle nightly · step "
                                      "review. It is shown here, not carried on.")
    assert chat.sub.text().endswith("Cycle nightly · step review")
    chat.new_button.click()
    assert composer.text.isVisible() and not composer.closed.isVisible()


def test_a_conversation_opened_elsewhere_to_talk_in_goes_on_here(make):
    """The core says which conversations are records. One opened by another
    part of QAVector - a task handed over - is carried on like a chat; a
    record is read even though it says nothing but where it came from."""
    talk = _session(source={"label": "Jira QA-1", "detail": "Login fails",
                            "ref": "jira:work:QA-1", "record": False})
    talk["record"] = False
    kept = _session("20261110-090000-aaaaaa", "Kept",
                    source={"label": "Jira QA-1", "record": True})
    kept["record"] = True
    core = FakeCore(sessions=[talk, kept], events={talk["id"]: _events(),
                                                   kept["id"]: _events()})
    chat = make(core).agents.chat
    chat.table.selectRow(0)
    assert chat.composer.text.isVisible() and not chat.composer.closed.isVisible()
    assert chat.sub.text().endswith("Jira QA-1")
    chat.table.selectRow(1)
    assert chat.composer.closed.isVisible() and not chat.composer.text.isVisible()


def test_a_conversation_with_a_connection_since_removed_still_says_whom_it_was_with(make):
    session = _session(connection="gone", connection_name="Old laptop")
    chat = make(FakeCore(sessions=[session], events={session["id"]: _events()})).agents.chat
    chat.table.selectRow(0)
    assert chat.composer.connection.currentText() == "Old laptop (removed)"
    assert chat.sub.text().startswith("Old laptop")


def test_a_turn_seen_in_the_log_opens_its_conversation(make):
    session = _session()
    core = FakeCore(sessions=[session], events={session["id"]: _events()},
                    turns=[_turn(session=session["id"])])
    view = make(core).agents
    view.tabs.setCurrentWidget(view.activity)
    table = view.activity.table
    table.itemDoubleClicked.emit(table.item(0, 3))
    assert view.tabs.currentWidget() is view.chat
    assert view.chat.current["id"] == session["id"]


def test_another_project_ends_what_was_going_on_and_shows_none_of_it(make):
    page = make(FakeCore())
    chat = page.agents.chat
    _say(chat, QUESTION)
    turn = FakeTurn.made[0]
    page.set_core(FakeCore(sessions=[_session("20261110-090000-aaaaaa", "Refund flow")]))
    assert turn.stopped and chat.turns == {}
    assert chat.current is None and chat.feed.kinds() == [] and chat.feed.waiting() == []
    assert [chat.table.item(row, 0).text() for row in range(chat.table.rowCount())] == [
        "Refund flow"]


# -- what it is called, and removing it ----------------------------------------------
def test_renaming_changes_what_it_is_called_everywhere(make, monkeypatch):
    session = _session()
    core = FakeCore(sessions=[session], events={session["id"]: _events()})
    chat = make(core).agents.chat
    chat.table.selectRow(0)
    monkeypatch.setattr(widgets, "ask_text", lambda *args, **kw: "Login, again")
    chat.rename_button.click()
    assert core.calls[-1] == (("session-set", session["id"]), {"title": "Login, again"})
    assert chat.title.text() == "Login, again"
    assert chat.table.item(0, 0).text() == "Login, again"


def test_deleting_asks_first_and_says_what_is_left_alone(make, monkeypatch):
    asked = []
    monkeypatch.setattr(widgets, "confirm", lambda *args, **kw: asked.append(args) or True)
    session = _session()
    core = FakeCore(sessions=[session], events={session["id"]: _events()})
    chat = make(core).agents.chat
    chat.table.selectRow(0)
    chat.delete_button.click()
    assert asked[0][2] == "Delete Flaky login test?"
    assert "Files the agent changed are left as they are." in asked[0][3]
    assert core.calls[-1] == (("session-delete", session["id"]), None)
    assert chat.table.rowCount() == 0 and chat.current is None
    assert chat.title.text() == "New conversation"


def test_a_conversation_being_answered_cannot_be_deleted(make, monkeypatch):
    monkeypatch.setattr(widgets, "confirm", lambda *args, **kw: True)
    core = FakeCore()
    chat = make(core).agents.chat
    _say(chat, QUESTION)
    assert not chat.delete_button.isEnabled()
    chat.delete()
    assert "session-delete" not in core.asked()
    FakeTurn.made[0].say(_events()[0])
    FakeTurn.made[0].finish()
    assert chat.delete_button.isEnabled()


# -- nobody to ask ----------------------------------------------------------------------
def test_with_no_connection_the_box_gives_way_to_how_to_make_one(make, monkeypatch, qapp):
    opened = []
    monkeypatch.setattr(ConnectionDialog, "exec", lambda dialog: opened.append(dialog) or
                        QDialog.Rejected)
    view = make(FakeCore(connections=[])).agents
    chat = view.chat
    assert chat.setup.isVisible() and not chat.composer.isVisible()
    chat.setup_button.click()
    assert view.tabs.currentWidget() is view.connections_tab and len(opened) == 1
    # One is made: the box is back, with it chosen.
    view.save_connection({"connection": {"name": "Claude Code", "kind": "claude"}})
    view.tabs.setCurrentWidget(chat)
    qapp.processEvents()
    assert chat.composer.isVisible() and not chat.setup.isVisible()
    assert chat.composer.connection.currentText() == "Claude Code"


# -- restrictions ------------------------------------------------------------------------
class _Window:
    """The restrictions window, answered for: the person chose ``saved``."""

    saved = {"files": "read_only"}
    opened = []

    def __init__(self, title, folder, level, own=None, **kwargs):
        _Window.opened.append((title, folder, level, dict(own or {})))

    def exec(self):
        return QDialog.Accepted

    def deleteLater(self):
        pass


def test_restrictions_chosen_before_the_first_message_go_with_the_conversation(
        make, monkeypatch, tmp_path):
    _Window.opened = []
    monkeypatch.setattr(agentchat.restrictions_mod, "RestrictionsWindow", _Window)
    core = FakeCore()
    chat = make(core).agents.chat
    assert chat.composer.restrict.isEnabled()
    chat.composer.restrict.click()
    assert _Window.opened[0][1:] == (str(tmp_path), "project", {})
    assert "session-set" not in core.asked()            # there is nothing to save it on yet
    assert chat.composer.restrict.property("restricted") == "true"
    _say(chat, QUESTION)
    document = next(call[1] for call in core.calls if call[0][0] == "session-new")
    assert document["restrictions"] == {"files": "read_only"}


def test_restrictions_of_a_conversation_begun_are_saved_on_it(make, monkeypatch):
    _Window.opened = []
    monkeypatch.setattr(agentchat.restrictions_mod, "RestrictionsWindow", _Window)
    session = _session()
    core = FakeCore(sessions=[session], events={session["id"]: _events()})
    chat = make(core).agents.chat
    chat.table.selectRow(0)
    chat.composer.restrict.click()
    assert core.calls[-1] == (("session-set", session["id"]),
                              {"restrictions": {"files": "read_only"}})
    assert chat.current["restrictions"] == {"files": "read_only"}


def test_restrictions_wait_for_a_folder_to_hold_the_agent_to(make):
    chat = make(FakeCore(), folder=False).agents.chat
    assert not chat.composer.restrict.isEnabled()
    chat.composer.folder.setEditText("/somewhere")
    assert chat.composer.restrict.isEnabled()


# -- remembering, and shape ---------------------------------------------------------------
def test_the_next_conversation_starts_from_the_last_one_s_choices(make, settings, tmp_path):
    core = FakeCore(connections=[_connection(), _connection("second", "Second")])
    chat = make(core).agents.chat
    chat.composer.connection.setCurrentIndex(1)
    chat.composer.mode.set_current("Edit")
    other = tmp_path / "other"
    other.mkdir()
    chat.composer.folder.setEditText(str(other))
    _say(chat, QUESTION)
    FakeTurn.made[0].say(_events()[0])
    FakeTurn.made[0].finish()
    memory = settings.integrations_state()["agents"]
    assert memory["folders"] == [str(other), str(tmp_path)]
    chat.new_button.click()
    assert chat.composer.settings() == {"connection": "second", "folder": str(other),
                                        "mode": "edit", "model": "", "effort": "", "plan": False,
                                        "others": "deny", "wake": "ask"}
    assert [chat.composer.folder.itemText(index)
            for index in range(chat.composer.folder.count())] == [str(other), str(tmp_path)]


def test_the_model_box_says_what_blank_means_for_the_agent_chosen(make):
    core = FakeCore(connections=[_connection(model="sonnet", effort="high"), _connection("second", "Second")])
    chat = make(core).agents.chat
    # The connection's default reads as itself; where it comes from is the tooltip's to say.
    assert chat.composer.model.currentText() == "sonnet"
    assert chat.composer.model.itemData(0, Qt.ToolTipRole) == "The connection's default model."
    assert chat.composer.effort.currentText() == "high" and chat.composer.effort.currentData() == ""
    # And the folded line says what the turn will run with, inherited or not.
    assert "sonnet" in chat.composer.context_summary.text()
    assert "effort high" in chat.composer.context_summary.text()
    chat.composer.connection.setCurrentIndex(1)
    chat.composer.connection.activated.emit(1)
    assert chat.composer.model.currentText() == "Use Claude Code settings"


def test_chat_reads_the_selected_connections_models_only_when_the_list_opens(make):
    core = FakeCore()
    chat = make(core).agents.chat
    assert "models" not in core.asked()
    chat.composer.set_options_open(True)
    picker = chat.composer.model
    picker.showPopup()
    assert core.calls[-1] == (("models", "claude"), None)
    picker.setCurrentIndex(picker.findData("opus"))
    picker.hidePopup()
    assert picker.currentText() == "Opus from CLI"
    _say(chat, "Please review")
    assert FakeTurn.made[0].document["model"] == "opus"


def test_the_composer_s_choices_stand_on_one_line_each(make, qapp):
    chat = make(FakeCore()).agents.chat
    composer = chat.composer
    composer.settings_button.click()
    qapp.processEvents()

    def centre(widget):
        return widget.mapTo(composer, widget.rect().center()).y()

    first = [centre(widget) for widget in (composer.folder, composer.restrict,
                                           composer.browse)]
    assert max(first) - min(first) <= 1, first
    second = [centre(widget) for widget in (composer.connection, composer.skills_button,
                                            composer.model)]
    assert max(second) - min(second) <= 1, second
    # The box is as wide as the rows of choices over it.
    assert composer.text.width() == composer.where.width() == composer.how.width()


def test_settings_fold_but_message_access_and_context_stay_visible(make, qapp):
    chat = make(FakeCore()).agents.chat
    composer = chat.composer
    assert not composer.options.isVisible()
    assert composer.context_summary.isVisible()
    assert composer.mode.isVisible() and composer.text.isVisible()
    composer.settings_button.click()
    qapp.processEvents()
    assert composer.options.isVisible() and composer.folder.isVisible()
    composer.model.set_value("opus")
    composer.set_skills(["review"])
    composer.settings_button.click()
    assert not composer.options.isVisible()
    assert "opus" in composer.context_summary.text()
    assert "1 skill" in composer.context_summary.text()


def test_new_conversation_without_a_folder_opens_settings(make):
    composer = make(FakeCore(), folder=False).agents.chat.composer
    assert composer.options.isVisible() and composer.folder.isVisible()


def test_a_long_path_and_a_long_name_do_not_widen_the_page(qapp, settings, dispose, tmp_path):
    """A combo is as wide as its longest item unless told otherwise, and both
    of these hold names from a person's own data. The page has to fit beside
    the rail on a 1366 px laptop, whatever they are called."""
    deep = tmp_path
    for part in ("a-very-long-folder-name-indeed",) * 5:
        deep = deep / part
    deep.mkdir(parents=True)
    settings.save_integrations_state({"agents": {"folders": [str(deep)]}})
    long_name = "Claude Code for the reviews of the nightly regression suite"
    session = _session(folder=str(deep), title="A title " * 20, connection_name=long_name)
    page = IntegrationsPage(settings)
    try:
        page.set_core(FakeCore(connections=[_connection(name=long_name)], sessions=[session],
                               events={session["id"]: _events()}))
        page.open_provider("agents")
        page.show()
        qapp.processEvents()
        new = page.agents.minimumSizeHint().width()
        page.agents.chat.table.selectRow(0)
        qapp.processEvents()
        assert max(new, page.agents.minimumSizeHint().width()) <= 1000
    finally:
        dispose(page)


def test_the_box_grows_with_what_is_typed_and_stops(make, qapp):
    chat = make(FakeCore()).agents.chat
    box = chat.composer.text
    one = box.height()
    box.setPlainText("a\nb\nc")
    qapp.processEvents()
    three = box.height()
    box.setPlainText("\n".join("line %d" % index for index in range(40)))
    qapp.processEvents()
    assert one < three < box.height()
    assert box.height() <= one * agentchat.MAX_LINES


def test_stop_prevents_the_queue_even_if_the_current_turn_finishes_successfully(make):
    chat = make(FakeCore()).agents.chat
    _say(chat, "first")
    first = FakeTurn.made[0]
    first.say(_events("first")[0])
    _say(chat, "second")
    chat.stop()
    chat._fill()                       # a refresh must not re-enable Stop
    assert not chat.composer.stop.isEnabled()
    first.finish("completed")         # Stop races with the last provider response
    assert len(FakeTurn.made) == 1
    assert chat.composer.draft() == "second"


def test_queued_messages_keep_the_access_and_model_chosen_when_submitted(make):
    chat = make(FakeCore()).agents.chat
    _say(chat, "first")
    first = FakeTurn.made[0]
    first.say(_events("first")[0])
    chat.composer.model.set_value("sonnet")
    _say(chat, "read this")
    chat.composer.mode.set_current("Full")
    chat.composer.model.set_value("opus")
    first.finish()
    assert FakeTurn.made[1].document == {"message": "read this", "mode": "read",
                                         "model": "sonnet", "effort": "", "plan": False}


def test_a_late_conversation_read_does_not_replace_new_or_its_draft(make, monkeypatch):
    session = _session()
    chat = make(FakeCore(sessions=[session])).agents.chat
    pending = []
    monkeypatch.setattr(chat, "ask", lambda kind, work, done: pending.append(done))
    chat.open_session(session["id"])
    chat.new_conversation()
    chat.composer.set_draft("a new thought")
    pending[0]({"ok": True, "session": session, "events": _events()})
    assert chat.current is None
    assert chat.composer.draft() == "a new thought"


def test_double_submission_while_creating_starts_only_one_conversation(make, monkeypatch):
    chat = make(FakeCore()).agents.chat
    pending = []
    monkeypatch.setattr(chat, "ask", lambda kind, work, done: pending.append((kind, done)))
    _say(chat, "first")
    chat.submit("first")
    assert [kind for kind, done in pending] == ["session-new"]
    assert not chat.new_button.isEnabled() and not chat.table.isEnabled()
    pending[0][1]({"ok": False, "error": "Could not create the session."})
    assert chat.composer.draft() == "first" and chat.composer.text.isEnabled()


def test_files_can_be_picked_removed_and_sent_without_text(make, monkeypatch, tmp_path):
    path = tmp_path / "données.txt"
    path.write_text("some data")
    chat = make(FakeCore()).agents.chat
    monkeypatch.setattr(widgets, "pick_paths", lambda *args: [str(path)])
    assert not chat.composer.send_button.isEnabled()
    chat.composer.attach.click()
    assert chat.composer.attachments() == [str(path)]
    assert chat.host.has_unsaved()
    assert chat.composer.send_button.isEnabled()
    chat.composer.send_button.click()
    assert FakeTurn.made[0].document["attachments"] == [str(path)]
    assert FakeTurn.made[0].document["message"] == ""
    assert chat.composer.attachments() == []
    assert chat.feed._waiting[0].body.files[0].text() == "File: données.txt"


def test_dropped_files_are_attachments_and_plain_paste_stays_text(make, tmp_path):
    from PySide6.QtCore import QMimeData, QUrl
    path = tmp_path / "report.pdf"
    path.write_bytes(b"example")
    chat = make(FakeCore()).agents.chat
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path))])
    assert chat.composer.text.canInsertFromMimeData(mime)
    chat.composer.text.insertFromMimeData(mime)
    chat.composer.text.insertFromMimeData(mime)
    assert chat.composer.attachments() == [str(path)]
    assert chat.composer.draft() == ""
    plain = QMimeData()
    plain.setText("inspect this")
    chat.composer.text.insertFromMimeData(plain)
    assert chat.composer.draft() == "inspect this"


def test_file_drafts_stay_with_their_conversation(make, tmp_path):
    path = tmp_path / "report.txt"
    path.write_text("data")
    session = _session()
    chat = make(FakeCore(sessions=[session])).agents.chat
    chat.composer.add_attachments([str(path)])
    chat.table.selectRow(0)
    assert chat.composer.attachments() == []
    chat.new_conversation()
    assert chat.composer.attachments() == [str(path)]


def test_a_queued_file_is_restored_after_stop(make, tmp_path):
    path = tmp_path / "report.txt"
    path.write_text("data")
    chat = make(FakeCore()).agents.chat
    _say(chat, "first")
    first = FakeTurn.made[0]
    first.say(_events("first")[0])
    chat.composer.add_attachments([str(path)])
    chat.composer.send()
    assert chat.composer.attachments() == []
    first.finish("stopped")
    assert chat.composer.attachments() == [str(path)]
    assert len(FakeTurn.made) == 1


def test_refused_attachments_are_preserved_for_retry(make, tmp_path):
    path = tmp_path / "report.txt"
    path.write_text("data")
    chat = make(FakeCore()).agents.chat
    chat.composer.add_attachments([str(path)])
    chat.composer.send()
    FakeTurn.made[0].refuse("attachments", "The file is no longer readable.")
    assert chat.current is None
    assert chat.composer.attachments() == [str(path)]
    assert "no longer readable" in chat.composer.note.text()


def test_history_shows_attached_filenames(qapp):
    feed = ChatFeed()
    feed.load([{"type": "user", "text": "Look at this", "attachments": [
        {"name": "report.pdf", "path": "attachments/batch/01-report.pdf", "size": 10}]}])
    assert feed._rows[0][1].body.files[0].text() == "File: report.pdf"


def test_action_buttons_share_a_row_and_height(make, qapp):
    chat = make(FakeCore()).agents.chat
    _say(chat, "first")
    qapp.processEvents()
    buttons = (chat.composer.attach, chat.composer.stop, chat.composer.send_button)
    assert len({button.height() for button in buttons}) == 1
    assert len({button.mapTo(chat.composer, button.rect().center()).y() for button in buttons}) == 1
    assert chat.composer.stop.width() == chat.composer.send_button.width()


def test_events_arriving_while_history_loads_are_kept_once(make, monkeypatch):
    session = _session()
    chat = make(FakeCore(sessions=[session])).agents.chat
    pending = []
    monkeypatch.setattr(chat, "ask", lambda kind, work, done: pending.append(done))
    chat.open_session(session["id"])
    events = _events()
    chat._heard(session["id"], events[-1])
    pending[0]({"ok": True, "session": session, "events": events[:-1]})
    assert chat.feed.said()[-1] == "Finished"
    chat._heard(session["id"], events[-1])  # already in the history snapshot
    assert chat.feed.kinds().count("end") == 1


def test_an_unanswered_core_never_deletes_a_potentially_started_session(make):
    core = FakeCore()
    chat = make(core).agents.chat
    _say(chat, "first")
    session_id = chat.current["id"]
    FakeTurn.made[0].answered.emit({"ok": False, "kind": "error",
                                   "record_unknown": True, "error": "The core crashed."})
    assert chat.current["id"] == session_id
    assert "session-delete" not in core.asked()
    assert chat.feed.said()[-1] == "Interrupted"
    assert chat.composer.draft() == "first"


def test_a_tool_without_an_outcome_is_not_marked_successful(qapp):
    feed = ChatFeed()
    feed.load(_events()[:4] + [{"type": "stopped"}])
    stage = feed._rows[2][1].stage
    assert stage["status"] == "unknown" and stage["kind"] == "caution"
    assert "No result was received" in stage["detail"]


# -- planning, and what changed ------------------------------------------------------
QUESTIONS = {"type": "questions", "items": [
    {"question": "Raise or return None?", "options": ["Raise", "Return None"],
     "multiple": False},
    {"question": "Which files?", "options": ["calc.py", "tests"], "multiple": True}]}
PLAN = "# Plan\n\n1. Guard `divide()`.\n2. Test it."


def _planned(*more, began=1794411900.0):
    """A planning turn's record: asked, looked, answered with ``more``."""
    found = [{"type": "user", "text": "Make divide safe", "mode": "read", "plan": True,
              "at": began}] + list(more) + [
        {"type": "completed", "text": "", "steps": 2, "cost_usd": 0.01, "at": began + 9}]
    return [dict(one, seq=index + 1, turn=1) for index, one in enumerate(found)]


def test_questions_are_a_card_that_answers_them_as_the_next_planning_turn(make):
    session = _session(phase="questions", plan=True)
    core = FakeCore(sessions=[session], events={session["id"]: _planned(QUESTIONS)})
    chat = make(core).agents.chat
    chat.table.selectRow(0)
    (card,) = chat.feed.cards()
    assert chat.feed.kinds() == ["you", "questions", "end"]
    assert not card.send.isHidden() and not card.send.isEnabled()
    first, second = card.choices
    first[0][0].click()                              # Raise
    second[0][1].click()                             # tests
    second[1].click()                                # and one of my own
    second[2].setText("README")
    assert card.send.isEnabled()
    card.send.click()
    (turn,) = FakeTurn.made
    assert turn.document["plan"] is True and turn.document["mode"] == "read"
    assert turn.document["message"] == (
        "Answers to your questions:\n1. Raise or return None?\n   Raise\n"
        "2. Which files?\n   tests; README")
    assert card.send.isHidden()                      # one answer, then it is history
    assert chat.composer.plan.isChecked()


def test_a_plan_is_approved_with_the_access_chosen_and_the_comments_as_notes(make):
    session = _session(phase="plan", plan=True)
    core = FakeCore(sessions=[session],
                    events={session["id"]: _planned({"type": "plan", "text": PLAN})})
    chat = make(core).agents.chat
    chat.table.selectRow(0)
    (card,) = chat.feed.cards()
    assert card.kind.text() == "PLAN · VERSION 1" and card.said == PLAN
    assert not card.start_edit.isHidden() and not card.revise_button.isEnabled()
    card.comments.setPlainText("Keep the old message too.")
    card.start_full.click()
    (turn,) = FakeTurn.made
    assert (turn.document["plan"], turn.document["mode"]) == (False, "full")
    assert turn.document["message"] == ("The plan is approved. Carry it out.\n\nNotes:\n"
                                        "Keep the old message too.")
    assert not chat.composer.plan.isChecked()
    assert chat.composer.settings()["mode"] == "full"


def test_comments_quote_the_part_of_the_plan_they_are_about_and_send_it_back(make):
    session = _session(phase="plan", plan=True)
    core = FakeCore(sessions=[session],
                    events={session["id"]: _planned({"type": "plan", "text": PLAN})})
    chat = make(core).agents.chat
    chat.table.selectRow(0)
    (card,) = chat.feed.cards()
    cursor = card.text.textCursor()
    found = card.text.document().find("Test it.")
    cursor.setPosition(found.selectionStart())
    cursor.setPosition(found.selectionEnd(), cursor.MoveMode.KeepAnchor)
    card.text.setTextCursor(cursor)
    card.quote.click()
    card.comments.insertPlainText("Use pytest.")
    card.revise_button.click()
    (turn,) = FakeTurn.made
    assert turn.document["plan"] is True
    assert turn.document["message"] == "Revise the plan:\n\n> Test it.\nUse pytest."


def test_a_revised_plan_folds_the_one_before_and_only_the_newest_waits(make):
    session = _session(phase="plan", plan=True)
    events = _planned({"type": "plan", "text": "# Plan one"})
    events += [dict(one, seq=one["seq"] + len(events), turn=2) for one in
               _planned({"type": "plan", "text": "# Plan two"}, began=1794412000.0)]
    core = FakeCore(sessions=[session], events={session["id"]: events})
    chat = make(core).agents.chat
    chat.table.selectRow(0)
    old, new = chat.feed.cards()
    assert (old.version, new.version) == (1, 2)
    assert old.body.isHidden() and not old.fold.isHidden() and old.meta.text() == "revised below"
    assert old.actions.isHidden() and not new.actions.isHidden()
    old.fold.click()
    assert not old.body.isHidden() and old.fold.text() == "Hide"


def test_cards_wait_only_while_the_conversation_does_and_nobody_is_answering(make):
    session = _session(phase="", plan=False)        # it moved on since the plan
    core = FakeCore(sessions=[session],
                    events={session["id"]: _planned({"type": "plan", "text": PLAN})})
    chat = make(core).agents.chat
    chat.table.selectRow(0)
    (card,) = chat.feed.cards()
    assert card.actions.isHidden()
    held = _session("20261110-090000-aaaaaa", "Cycle", phase="plan", record=True,
                    source={"label": "Cycle nightly · step plan", "record": True})
    core.sessions.append(held)
    core.events[held["id"]] = _planned({"type": "plan", "text": PLAN})
    chat.load_sessions()
    chat.open_session(held["id"])
    (card,) = chat.feed.cards()
    assert card.actions.isHidden()                  # a record is read, not acted on


def test_plan_first_goes_with_the_turn_and_is_the_next_conversation_s_choice(make):
    chat = make().agents.chat
    chat.composer.plan.setChecked(True)
    assert _say(chat, "Make divide safe")
    args, document = next(call for call in chat.host.core.calls if call[0][0] == "session-new")
    assert document["plan"] is True
    (turn,) = FakeTurn.made
    assert turn.document["plan"] is True
    turn.finish(phase="questions")
    chat.new_button.click()
    assert chat.composer.plan.isChecked()


def test_changes_are_visible_and_refresh_before_the_first_turn_finishes(make):
    found = {"ok": True, "live": True, "files": [], "patch": "", "added": 0, "removed": 0}
    core = FakeCore(changes=lambda args, document: dict(found))
    chat = make(core).agents.chat
    _say(chat, "Change the file")
    (turn,) = FakeTurn.made
    assert not chat.current["turns"] and chat.running()
    assert not chat.changes_button.isHidden()
    assert chat.changes_button.text() == "Changes" and not chat.changes_button.parts
    found.update(files=[{"path": "app.py", "status": "modified", "added": 100, "removed": 20}],
                 added=100, removed=20,
                 patch="diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n")
    chat._changes_timer.timeout.emit()
    assert chat.changes_button.text() == "Changes +100 -20"
    chat.changes_button.click()
    dialog = chat.changes_dialog
    assert dialog.isVisible() and dialog._refresh.isActive()
    dialog.view.sections[0].set_expanded(True)
    found.update(added=101)
    dialog._refresh.timeout.emit()
    assert dialog.view.summary.text() == "1 file changed · +101 -20"
    assert chat.changes_button.text() == "Changes +101 -20"
    assert dialog.view.sections[0].is_expanded()
    reads = core.asked().count("changes")
    chat._poll_changes()
    assert core.asked().count("changes") == reads
    assert chat.running()  # inspection has not stopped or finished the agent
    found.update(live=False, added=102)
    turn.finish()
    assert chat.changes_button.text() == "Changes +102 -20"
    dialog._refresh.timeout.emit()
    assert not dialog._refresh.isActive()
    dialog.close()


def test_live_changes_coalesce_slow_reads_and_do_not_update_another_chat(make):
    chat = make().agents.chat
    ask = chat.host.ask
    pending = []

    def delayed(kind, work, done):
        if kind.startswith("changes:"):
            pending.append(done)
        else:
            ask(kind, work, done)

    chat.host.ask = delayed
    _say(chat, "Work")
    chat._poll_changes()
    chat._poll_changes()
    assert len(pending) == 1
    chat.changes_button.click()
    assert len(pending) == 1  # the window shares the button's pending read
    chat.new_conversation()
    pending.pop()({"ok": True, "live": True, "files": [], "added": 100, "removed": 20})
    assert chat.changes_button.isHidden()
    assert chat.changes_dialog.view.summary.text() == "No files changed."
    chat.changes_dialog.close()


def test_what_a_turn_changed_is_a_line_that_opens_it_file_by_file(make):
    session = _session(turns=[{"n": 1, "state": "completed",
                               "changes": {"files": 2, "added": 3, "removed": 1}}],
                       context={"used": 24813, "window": 1000000}, cost_usd=0.25,
                       tokens={"input": 10, "cache_read": 101029, "cache_write": 3948,
                               "output": 2037})
    patch = ("diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n"
             "@@ -1,2 +1,4 @@\n def divide(a, b):\n+    if b == 0:\n+        raise ValueError\n"
             "-    return a/b\n+    return a / b\n")
    found = {"ok": True, "files": [
        {"path": "calc.py", "status": "modified", "added": 3, "removed": 1, "binary": False},
        {"path": "logo.png", "status": "added", "added": 0, "removed": 0, "binary": True}],
        "patch": patch, "added": 3, "removed": 1, "truncated": False, "unkept": [],
        "kept": "all"}
    events = _events() + [{"type": "changes", "files": 2, "added": 3, "removed": 1,
                           "kept": "all", "seq": 9, "turn": 1}]
    core = FakeCore(sessions=[session], events={session["id"]: events},
                    changes=lambda args, document: found)
    chat = make(core).agents.chat
    chat.table.selectRow(0)
    assert chat.usage.text() == "context 25k of 1M · est. $0.25"
    assert chat.usage.toolTip() == "Tokens so far: 105k in (101k cached) · 2k out"
    line = chat.feed._rows[-1][1].body
    assert line.said == "Changed 2 files · +3 -1"
    line.view.click()
    dialog = chat.changes_dialog
    assert dialog.isVisible() and dialog.scope.currentData() == 1
    assert core.calls[-1] == (("changes", session["id"]), {"turn": 1})
    view = dialog.view
    assert view.summary.text() == "2 files changed · +3 -1"
    calc, logo = view.sections
    assert calc.text is None                         # laid out only when opened
    calc.set_expanded(True)
    assert "+    if b == 0:" in calc.text.toPlainText()
    logo.set_expanded(True)
    assert logo.text.text() == "A binary file: what changed in it is not shown."
    dialog.scope.setCurrentIndex(0)
    assert core.calls[-1] == (("changes", session["id"]), {})
    dialog.close()
    chat.new_button.click()                          # nothing used, nothing changed yet
    assert chat.usage.text() == "" and chat.changes_button.isHidden()


def test_effort_is_chosen_from_the_program_s_levels_and_goes_with_the_turn(make):
    """The levels come from the core's kinds, read once when the chat is first
    in front; Default leaves it to the connection, else to the program."""
    session = _session(effort="high")
    other = _session("20261110-090000-aaaaaa", "Other")
    core = FakeCore(sessions=[session, other], events={session["id"]: _events(),
                                                       other["id"]: _events()})
    chat = make(core).agents.chat
    assert [args for args, _doc in core.calls if args[0] == "kinds"] == [("kinds",)]
    chat.table.selectRow(0)
    effort = chat.composer.effort
    assert [effort.itemText(index) for index in range(effort.count())] == [
        "Default", "low", "medium", "high", "xhigh", "max"]
    assert effort.currentData() == "high"           # the conversation's own
    chat.table.selectRow(1)
    assert chat.composer.effort.currentData() == ""  # not the one shown before
    effort.setCurrentIndex(effort.findData("max"))
    assert _say(chat, "think hard")
    assert FakeTurn.made[-1].document["effort"] == "max"
    assert "effort max" in chat.composer.context_summary.text()


def test_a_row_taken_out_of_the_feed_never_becomes_a_window(qapp, dispose):
    """A row put in the layout is shown when the event loop next gets to it;
    a tool's row replaced by its result in the same pass would be shown with
    no parent - an empty window flashing on the desktop at every opening.
    Whatever the feed takes out is hidden for good before it goes."""
    from PySide6.QtWidgets import QApplication
    holder = QWidget()
    feed = ChatFeed(holder)
    holder.show()
    dropped = []
    keep = feed._drop

    def drop(widget):
        dropped.append(widget)
        keep(widget)

    feed._drop = drop
    feed.load(_events())                            # the tool's row is replaced
    assert dropped and all(widget.isHidden() for widget in dropped)
    qapp.processEvents()
    assert [one for one in QApplication.topLevelWidgets()
            if one.isVisible() and type(one).__name__ in ("StageRow", "_Row")] == []
    dispose(holder)
