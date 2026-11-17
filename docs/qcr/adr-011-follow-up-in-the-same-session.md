# ADR-011: a delegated task asks, and the answer goes to the same session

Date: 2026-11-17. Status: **Accepted** (user, 2026-11-17: D1 a last line,
cheaper than a tool that keeps the child's turn open; D3 the person may answer
too). The direction: answer in the child's existing session. A new session for every
answer would pile up sessions, lower the quality of the work and cost more,
because the agent would gather the same information again. Builds on
[ADR-006](adr-006-agent-messaging-and-delegation.md) (D5 delegation, D6
limits), [ADR-009](adr-009-waking-the-delegator.md) (rounds and waking) and
[ADR-010](adr-010-agent-roles.md) (roles and the verdict line).

## The problem

A delegated task is one turn in a new session (ADR-006 D5). If the child needs
something cleared up, all it can do is end its turn with the question in its
answer:

* **The question looks like a result.** The task ends `succeeded`, and nothing
  tells the parent, the wake message or the card that it is a question.
* **No way back into the child's session.** The parent has `delegate`, which
  starts a new session and a new context package, and `send_message`, which
  starts a new read-only session too.
* **Every answer starts over.** The child's session (`session_id`,
  `provider_session_id`) is kept but never continued. The new child reads
  again what the first one had read.

## What exists (audit)

* **The runner already resumes.** A session whose provider session has started
  is resumed on its next turn (`runner.send`: `resume` when
  `provider_started` and `provider_session` are set), so a second turn in the
  child's session keeps everything the agent had read.
* **A task's output has the child's session** (`session_id`) and its turn
  state. Its record conversation is `source.label = "Delegated"`, with
  `parent` set.
* **Rounds (ADR-009)** wake the parent when the items of a round end, whatever
  the outcome.
* **A role's verdict** is read from the answer's last JSON line
  (`verdict_of`). A question can be read the same way.

## Decisions

**D1. The child says it needs an answer.** It ends its turn with a last line:
`{"needs": "answer", "question": "..."}`. The runner's preface tells every
delegated child about this line, the same way the verdict is asked for. The
output then gets `needs_input: true` and `question` (at most 2,000
characters). The task ends `succeeded`, and the execution states do not
change, so the engine stays as it is. Everything that reads a task's outcome
(the round, the wake message, the cards, Usage) shows it as **asks**, not
**done**.

**D2. The answer continues the same session.**
`agents.tasks.reply {task, answer}` runs one more turn in the child's existing
session:

* same folder, mode, model, effort and role;
* the provider session is resumed;
* the turn's text is the answer, with a short QAVector line saying who
  answers.

It is a new execution (`follow_up_of: <task>`, the same `name`), so it can be
waited on, canceled and woken on like any task. In the chat (user,
2026-11-17) the task keeps one card, which shows how it stands ("asks");
each question is a card of its own, and so is each answer, in time order:

* **QUESTION FROM <agent>**, marked with an arrow coming in: amber while it
  waits, with the place to answer it; settled once answered;
* **ANSWER TO <agent>**, marked with an arrow going out, with the accent
  stripe and who answered ("from you", "from Opus").

Usage counts it in the same conversation, because it is one.

**D3. Who may reply.** The agent that delegated the task (the same actor
check as `verify`), and the person, from the task's card (**Answer**). Nobody
else. Replying to a task that does not ask (`needs_input` false) is refused
(`not_waiting`).

**D4. Waking.** A task that asks counts as ended for its round (ADR-009), so
the parent is woken with the question. The wake message lists it under
"asks" with the question, and says to answer with `agents.tasks.reply` and not
to delegate again. With **Ask me**, the TASKS FINISHED card shows the question
too.

**D5. Bounds.**

* **No limit on questions** (user, 2026-11-17: "if the task needs them to
  talk, let them talk till tomorrow - show the count, and the person decides
  whether to stop"). Each question is counted (`asked`); the task's card says
  how many so far.
* **The person stops it.** **Stop** on the task's card (`task_stop` over the
  owner pipe): the task takes no more answers (`stopped`), and a turn of it
  that still runs is canceled. The conversation keeps what it found.
* A reply turn has the task's own timeout. The ADR-006 D6 limits are
  unchanged: a reply turn takes a place among its connection's running tasks.
* Questions and replies are not counted as failed rounds (ADR-009 D6), because
  asking is not failing. A wake that brings only questions is not counted
  against the 10-wakes backstop either: a dialogue is not a loop, and the
  person sees it and can stop it.

**D6. Messages are unchanged.** A message that expects a reply (ADR-006 D4)
is still a single read-only turn. To go back and forth with another agent,
the agent delegates a task, and the task can ask.

## Increments

* **F.1 core.** **Done 2026-11-17** (uncommitted):
  * the question line and its preface;
  * `needs_input` and `question` in the output;
  * `agents.tasks.reply` (actor check, `not_waiting`, `stopped`), and the
    person's `task_stop`;
  * the follow-up execution in the same session;
  * the wake message's "asks" part and the `next` hint.

  Tests with fake agents (`tests/test_qcr_follow_up.py`): a child asks, the
  parent replies, and the second turn resumes the same provider session; the
  person answers after the parent's turn ended (the host lends the
  conversation's actor a caller for it); `not_waiting`, `not_found`, no
  limit on questions, the person's stop; a task after one that asks waits for its answer. The MCP
  tool text tells agents to reply, never to delegate again.
* **F.2 GUI.** **Done 2026-11-17** (uncommitted):
  * the task card says it asks and how many questions so far, with **Stop**;
  * when the person and the agent answer at once, the answer that went is
    shown, and the person's is said to have come too late;
  * a wake with only questions does not count against going on by itself;
  * the task card says it asks; question and answer cards of their own
    (`TaskQuestionCard`, `TaskAnswerCard`, arrows drawn in `icons.py`);
  * **Answer** on the question card (`task_reply` over the owner pipe);
  * the woken turn's message gives the question and how to answer it.

  Tests: `gui/tests/test_follow_up.py`. Light and dark screenshots.
* **The user's live check:** Opus delegates a task to Codex with something
  missing, Codex asks, Opus answers, and Codex finishes in the same session.

## Decided

1. ~~How the child asks~~ - a last line (D1): cheaper, since a tool would keep
   the child's turn open while the parent answers.
2. ~~Who answers~~ - the agent that delegated, and the person from the task's
   card (D3).
3. ~~How many questions~~ - no limit; counted and shown, the person stops it
   (D5).
