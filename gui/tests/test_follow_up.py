"""A delegated task asks, and its answer goes on in the same conversation (ADR-011), the window's side.

* The task's own card says it asks; the question is a card of its own, with
  the place to answer it while it waits.
* The answer - the person's from the card, or the delegating agent's - is a
  card of its own after the question, saying who answered.
* An answered question settles; the next question is a new card. The task's
  card counts its questions and has Stop: no limit, the person decides.
* When the person and the agent answer at once, the one that went is shown.
* A wake that only brings questions is not counted against going on by itself.
* The woken agent is told the question and how to answer it.
"""

import pytest

from cms_gui.pages import agentcards

CLAUDE = {"id": "claude", "name": "Claude", "kind": "claude", "qavector_access": {},
          "others_default": "auto", "allowed_agents": [], "wake_default": "ask"}
SESSION = {"id": "s-1", "connection": "claude", "folder": "/tmp", "mode": "edit", "others": "auto",
           "source": {"label": "Chat"}, "turns": [], "wake": "ask"}
ACTOR = "agent:claude/s-1"


@pytest.fixture
def chat(qapp):
    from cms_gui.pages import agentchat
    from cms_gui.settings import Settings

    class Host:
        def __init__(self):
            self.replies = []

        def grant_agent(self, *args, **kwargs):
            return None

        def messages(self, done, **filters):
            done([])

        def reply_task(self, task, answer, by="You"):
            self.replies.append((task, answer, by))
    tab = agentchat.ChatTab(Settings())
    tab.core = lambda: object()
    tab.set_connections([CLAUDE, {"id": "codex", "name": "Codex", "kind": "codex"}])
    previous = agentchat.ChatTab.qcr_access
    agentchat.ChatTab.qcr_access = host = Host()
    tab._put(dict(SESSION))
    tab._show(dict(SESSION), [], whole=True)
    tab.fake_qcr = host
    yield tab
    agentchat.ChatTab.qcr_access = previous
    tab.close()


def task(state="running", **more):
    return dict({"execution_id": "e-1", "capability_id": "agents.codex.delegate", "actor_id": ACTOR,
                 "state": state, "input": {"goal": "Fix the login tests"}, "created_at": 100.0}, **more)


def asked(execution_id, number, question, at, **more):
    return dict({"execution_id": execution_id, "actor_id": ACTOR, "state": "succeeded", "updated_at": at,
                 "output": {"needs_input": True, "question": question, "asked": number, "root": "e-1",
                            "session_id": "child", "answer": "..."}}, **more)


def reply(execution_id, to, said, state="running", at=0.0, **output):
    return {"execution_id": execution_id, "capability_id": "agents.tasks.reply", "actor_id": ACTOR,
            "state": state, "input": {"task": to, "answer": said}, "created_at": at, "output": output}


def test_a_question_and_each_answer_are_cards_of_their_own(chat):
    tab = chat
    tab.agent_execution(task())
    tab.agent_execution(asked("e-1", 1, "Which database?", 110.0, capability_id="agents.codex.delegate",
                              input={"goal": "Fix the login tests"}, created_at=100.0))
    [request] = tab.feed.placed("request")
    assert request.request["state"] == "asks" and request.meta.text().startswith("asks")
    [question] = tab.feed.placed("question")
    assert isinstance(question, agentcards.TaskQuestionCard)
    assert question.text.text() == "Which database?" and "QUESTION FROM CODEX" in question.kind.text()
    assert question.meta.text() == "waits for an answer - question 1" and not question.answer_button.isEnabled()

    question.answer_box.setPlainText("Use the sqlite fixture.")
    question.answer_button.click()
    assert tab.fake_qcr.replies == [("e-1", "Use the sqlite fixture.", "You")]
    [answer] = tab.feed.placed("answer")
    assert answer.text.text() == "Use the sqlite fixture." and answer.meta.text() == "from you"
    assert question.answer_box.isHidden() and "answered" in question.meta.text()

    # The core's execution of it: the same answer, not a second card.
    tab.agent_execution(reply("r-1", "e-1", "Use the sqlite fixture.", at=120.0))
    assert len(tab.feed.placed("answer")) == 1 and request.request["state"] == "running"

    # It asks again; the delegating agent answers this one.
    tab.agent_execution(dict(reply("r-1", "e-1", "Use the sqlite fixture.", at=120.0),
                             **asked("r-1", 2, "May I add freezegun?", 130.0, by="You")))
    assert [one.text.text() for one in tab.feed.placed("question")] == ["Which database?", "May I add freezegun?"]
    second = tab.feed.placed("question")[1]
    assert second.meta.text() == "waits for an answer - question 2" and not second.answer_box.isHidden()
    tab.agent_execution(reply("r-2", "r-1", "Patch time.time instead.", at=140.0))
    answers = tab.feed.placed("answer")
    assert [one.meta.text() for one in answers] == ["from you", "from Claude"]
    assert second.answer_box.isHidden()

    tab.agent_execution(reply("r-2", "r-1", "Patch time.time instead.", state="succeeded", at=140.0,
                              answer="All 12 tests pass.", root="e-1", by="Claude"))
    assert request.request["state"] == "succeeded"


def test_the_woken_agent_is_told_the_question_and_how_to_answer_it():
    said = agentcards.wake_message({"condition": {"why": "round"}, "running": [], "items": [
        {"execution_id": "e-1", "name": "tests", "kind": "task", "target_name": "Codex",
         "goal": "Fix the login tests", "state": "asks", "question": "Which database?", "answer": "..."}]})
    assert "asks you a question" in said and "It asks: Which database?" in said
    assert 'agents.tasks.reply {"task": "e-1"' in said and "Do not delegate it again" in said
    assert "Answer: ..." not in said


def test_the_task_counts_its_questions_and_the_person_stops_it(chat):
    tab = chat
    tab.agent_execution(task())
    tab.agent_execution(asked("e-1", 1, "Which database?", 110.0, capability_id="agents.codex.delegate",
                              input={"goal": "Fix the login tests"}, created_at=100.0))
    [request] = tab.feed.placed("request")
    assert request.meta.text() == "asks - its question is below - 1 question so far"
    assert not request.stop_button.isHidden()
    stops = []
    tab.fake_qcr.stop_task = lambda task, by="you": stops.append(task)
    request.stop_button.click()
    assert stops == ["e-1"] and request.request["state"] == "canceled" and request.stop_button.isHidden()
    [question] = tab.feed.placed("question")
    assert question.meta.text() == "not answered - the task was stopped - question 1"
    assert question.answer_box.isHidden()
    # The woken agent's answer, refused now: nothing changes.
    tab.agent_execution(reply("r-1", "e-1", "Too late", state="failed", at=130.0) | {
        "error": {"code": "stopped", "message": "You stopped this task."}})
    assert tab.feed.placed("answer") == [] and request.request["state"] == "canceled"


def test_when_both_answer_at_once_the_one_that_went_is_shown(chat):
    tab = chat
    tab.agent_execution(task())
    tab.agent_execution(asked("e-1", 1, "Which database?", 110.0, capability_id="agents.codex.delegate",
                              input={"goal": "Fix the login tests"}, created_at=100.0))
    [question] = tab.feed.placed("question")
    question.answer_box.setPlainText("Use postgres.")
    question.answer_button.click()
    [answer] = tab.feed.placed("answer")
    assert answer.meta.text() == "from you"
    tab.agent_execution(reply("r-1", "e-1", "Use the sqlite fixture.", at=111.0))      # the agent's went first
    tab.agent_execution(reply("r-2", "e-1", "Use postgres.", state="failed", at=112.0) | {
        "error": {"code": "not_waiting", "message": "That question was answered already."}})
    [answer] = tab.feed.placed("answer")
    assert answer.meta.text() == "from Claude" and answer.text.text() == "Use the sqlite fixture."
    assert "not sent" in answer.note.text() and "Use postgres." in answer.note.text()
    assert tab.feed.placed("request")[0].request["state"] == "running"


def test_a_wake_with_only_questions_is_not_counted(chat):
    tab = chat
    started = []
    tab._go_on = lambda session, document: started.append(document)
    session = dict(SESSION, wake="continue")
    tab._put(session)
    item = {"execution_id": "e-1", "name": "", "kind": "task", "target_name": "Codex", "goal": "Play",
            "state": "asks", "question": "Your move?"}
    for _ in range(15):
        tab.round_finished({"session": "s-1", "condition": {"id": "w", "why": "round"}, "items": [item],
                            "running": [], "failed": False})
    assert len(started) == 15 and tab._woke["s-1"] == {"failed": 0, "all": 0}


def test_an_answer_from_the_board_is_the_person_s_in_the_chat_too(chat):
    tab = chat
    tab.agent_execution(task())
    tab.agent_execution(asked("e-1", 1, "Which database?", 110.0, capability_id="agents.codex.delegate",
                              input={"goal": "Fix the login tests"}, created_at=100.0))
    tab.reply_task("e-1", "Use the sqlite fixture.")             # what Agents -> Tasks calls
    assert tab.fake_qcr.replies == [("e-1", "Use the sqlite fixture.", "You")]
    tab.agent_execution(reply("r-1", "e-1", "Use the sqlite fixture.", at=120.0))
    [answer] = tab.feed.placed("answer")
    assert answer.meta.text() == "from you"
    [question] = tab.feed.placed("question")
    assert question.answer_box.isHidden()
