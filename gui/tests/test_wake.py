"""A conversation woken when what it handed on has ended (ADR-009), the window's side.

* Continue: the conversation goes on by itself with QAVector's message - the
  results - shown as QAVector's, not the person's.
* After 3 failed rounds (or 10 in all) since the person last spoke, it asks
  instead; the person's word lets it go on by itself again.
* Ask me: a card waits in the conversation; Continue goes on, Dismiss leaves it.
* A turn running: the results go first in line after it. Off: nothing.
"""

import pytest

from cms_gui.pages import agentcards

CLAUDE = {"id": "claude", "name": "Claude", "kind": "claude", "qavector_access": {},
          "others_default": "auto", "allowed_agents": [], "wake_default": "ask"}
SESSION = {"id": "s-1", "connection": "claude", "folder": "/tmp", "mode": "edit", "others": "auto",
           "source": {"label": "Chat"}, "turns": []}


def found(failed=False, state="succeeded", condition="round"):
    return {"type": "round_finished", "session": "s-1", "actor": "agent:claude/s-1",
            "condition": {"id": "w1", "kind": condition, "why": condition, "names": []},
            "running": [], "failed": failed,
            "items": [{"execution_id": "e-1", "name": "tests", "kind": "task", "target": "codex",
                       "target_name": "Codex", "goal": "Run the tests", "model": "gpt-5", "effort": "low",
                       "state": state, "answer": "12 passed", "error": "", "acceptance": [],
                       "files_changed": 0, "session_id": "child"}]}


@pytest.fixture
def chat(qapp, tmp_path):
    from PySide6.QtCore import QObject, Signal
    from cms_gui.pages import agentchat
    from cms_gui.settings import Settings
    made = []

    class Turn(QObject):
        event = Signal(dict)
        answered = Signal(dict)

        def __init__(self, core, session_id, document, parent=None):
            super().__init__(parent)
            self.session_id, self.document = session_id, document
            made.append(self)

        def start(self):
            pass

    class Host:
        def grant_agent(self, *args, **kwargs):
            return None

        def messages(self, done, **filters):
            done([])
    tab = agentchat.ChatTab(Settings())
    tab.turn_factory = Turn
    tab.core = lambda: object()
    tab.set_connections([CLAUDE])
    previous = agentchat.ChatTab.qcr_access
    agentchat.ChatTab.qcr_access = Host()
    tab.made = made

    def show(wake):
        session = dict(SESSION, wake=wake)
        tab._put(session)
        tab._show(session, [], whole=True)
        return session
    tab.show_session = show
    yield tab
    agentchat.ChatTab.qcr_access = previous
    tab.close()


def end(tab, turn, state="completed"):
    turn.answered.emit({"ok": True, "turn": {"state": state}, "session": dict(SESSION, wake="continue")})


def test_continue_goes_on_with_qavector_s_message_until_three_failed_rounds(chat):
    tab = chat
    tab.show_session("continue")
    tab.round_finished(found())
    [turn] = tab.made
    assert turn.document["trigger"] == "tasks_finished"
    said = turn.document["message"]
    assert said.startswith(agentcards.WAKE_HEAD) and "tests - Run the tests (task for Codex, gpt-5 low): done." in said
    assert "Answer: 12 passed" in said and "agents.tasks.verify" in said
    end(tab, turn)
    for _ in range(3):
        tab.round_finished(found(failed=True, state="failed"))
        end(tab, tab.made[-1])
    assert len(tab.made) == 4                   # one good round, then three failed ones
    tab.round_finished(found(failed=True, state="failed"))
    assert len(tab.made) == 4                   # the fourth waits for the person
    (card,) = tab.feed.placed("wake")
    assert "as far as allowed" in card.note.text()
    card.go_button.click()
    assert len(tab.made) == 5 and tab.feed.placed("wake") == []
    end(tab, tab.made[-1])
    tab.round_finished(found(failed=True, state="failed"))
    assert len(tab.made) == 6, "the person's word lets it go on by itself again"


def test_the_person_speaking_resets_what_it_may_do_by_itself(chat):
    tab = chat
    session = tab.show_session("continue")
    for _ in range(3):
        tab.round_finished(found(failed=True, state="failed"))
        end(tab, tab.made[-1])
    tab._start(session, "Try another way", document={"message": "Try another way"})
    end(tab, tab.made[-1])
    tab.round_finished(found(failed=True, state="failed"))
    assert len(tab.made) == 5 and tab.made[-1].document.get("trigger") == "tasks_finished"


def test_ask_me_waits_on_a_card_and_dismiss_leaves_it(chat):
    tab = chat
    tab.show_session("ask")
    tab.round_finished(found())
    assert tab.made == []
    (card,) = tab.feed.placed("wake")
    assert card.kind.text() == "TASKS FINISHED" and [l.text() for l in card.lines] == ["tests - Codex: done"]
    card.dismiss_button.click()
    assert tab.feed.placed("wake") == [] and tab.made == []
    tab.round_finished(found())
    tab.feed.placed("wake")[0].go_button.click()
    assert len(tab.made) == 1 and tab.made[0].document["trigger"] == "tasks_finished"


def test_a_turn_running_takes_the_results_first_after_it_and_off_does_nothing(chat):
    tab = chat
    session = tab.show_session("continue")
    tab._start(session, "Something else", document={"message": "Something else"})
    tab._queued["s-1"] = [{"message": "And then this"}]
    tab.round_finished(found())
    assert len(tab.made) == 1 and tab._queued["s-1"][0]["trigger"] == "tasks_finished"
    end(tab, tab.made[0])
    assert tab.made[1].document["trigger"] == "tasks_finished"
    tab.show_session("off")
    tab.round_finished(found())
    assert len(tab.made) == 2 and tab.feed.placed("wake") == []


def test_qavector_s_message_reads_as_qavector_s(chat):
    tab = chat
    tab._show(dict(SESSION), [{"type": "user", "text": "[QAVector: ...]", "trigger": "tasks_finished",
                               "at": 1.0, "seq": 1}], whole=True)
    row = tab.feed._rows[0][1]
    labels = [label.text() for label in row.body.findChildren(type(row.body.meta))]
    assert "QAVECTOR · TASKS FINISHED" in labels and "YOU" not in labels


def test_long_review_reaches_the_parent_without_losing_its_last_finding():
    message = found()
    review = "A review finding. " * 500 + "The final blocking issue."
    message["items"][0]["answer"] = review
    said = agentcards.wake_message(message)
    assert review in said
    assert "agents.tasks.reply" in said


def test_shortened_wake_requires_reading_the_correct_execution():
    message = found()
    message["items"][0].update(answer="", wake_shortened=True)
    said = agentcards.wake_message(message)
    assert 'qavector_get_execution {"execution_id": "e-1"}' in said
    assert "before judging its result" in said


def test_storage_truncation_is_not_presented_as_a_complete_result():
    message = found()
    message["items"][0].update(answer_truncated=True, answer_chars=25000)
    said = agentcards.wake_message(message)
    assert "25000 characters originally" in said
    assert "Do not treat this excerpt as the full result" in said
    assert "omitted findings" in said


def test_a_finished_task_is_offered_its_own_conversation_for_more_of_the_same_work():
    said = agentcards.wake_message({"items": [
        {"execution_id": "x-done", "state": "succeeded", "goal": "Review", "answer": "Two findings."},
        {"execution_id": "x-failed", "state": "failed", "goal": "Build", "error": "boom"},
        {"execution_id": "x-asked", "state": "succeeded", "kind": "question", "goal": "Which DB?"}]})
    assert agentcards.WAKE_CONTINUE % "x-done" in said
    assert agentcards.WAKE_CONTINUE % "x-failed" not in said
    assert agentcards.WAKE_CONTINUE % "x-asked" not in said


def _two():
    both = found()
    both["items"].append(dict(both["items"][0], execution_id="e-2", name="lint", goal="Lint it",
                              answer="clean"))
    return both


def test_a_result_the_agent_read_in_the_person_s_turn_leaves_the_queued_wake(chat):
    tab = chat
    session = tab.show_session("continue")
    tab._start(session, "Is the review in?", document={"message": "Is the review in?"})
    tab.round_finished(_two())
    [document] = tab._queued["s-1"]
    tab.round_seen({"session": "s-1", "execution_id": "e-1"})
    # What it read is no longer told; what it did not read still is.
    assert "Run the tests" not in document["message"] and "Lint it" in document["message"]
    tab.round_seen({"session": "s-1", "execution_id": "e-2"})
    assert tab._queued["s-1"] == []
    end(tab, tab.made[0])
    assert len(tab.made) == 1, "nothing left to wake it for"


def test_a_wake_card_loses_what_the_agent_read_and_goes_when_nothing_is_left(chat):
    tab = chat
    tab.show_session("ask")
    tab.round_finished(_two())
    tab.round_seen({"session": "s-1", "execution_id": "e-1"})
    (card,) = tab.feed.placed("wake")
    assert [line.text() for line in card.lines] == ["lint - Codex: done"]
    tab.round_seen({"session": "s-other", "execution_id": "e-2"})     # another conversation's
    assert len(tab.feed.placed("wake")) == 1
    tab.round_seen({"session": "s-1", "execution_id": "e-2"})
    assert tab.feed.placed("wake") == []
