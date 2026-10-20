"""Asking a person something in the middle of a run, and waiting for the answer.

A cycle that is about to change somebody's repository, or move work along a
board other people watch, sometimes ought to stop and ask. Nothing in the engine
could: the one path from a run back to a person is
``engine/services.py``, and both ends of it are wired to services in three
places - the engine parses the reference as ``Project/Service``, the bridge
resolves it against ``services.json`` before it looks at the operation, and the
window routes only ``service.request`` to the bridge.

So this is that path used for a different question. The transport is the same
and was always general: a JSON line out on ``--events``, a JSON line back on
``--control``. What is new is a vocabulary for "here is a question and the
answers it takes", and the window handing it to a dialog instead of to a
service supervisor.

**An unanswered question is never a yes.** No GUI, nobody there, a timeout, a
window closed - every one of them comes back as *not answered*, and the step
that asked fails. A gate that approves when it cannot ask is not a gate, and it
would be a gate precisely where somebody thought they had one.

**One question at a time is not assumed.** Steps run in parallel, so a request
carries an id and blocks on its own event, the way a service request does.
"""

import logging
import threading

from engine import events

log = logging.getLogger("cycle.ask")

#: How long a question waits when the step does not say. Long, because the
#: answer is a person walking back to their desk, and a default that is nearly
#: long enough is worse than one that is plainly generous.
DEFAULT_TIMEOUT_MS = 600000

#: What a question takes for an answer when the step names nothing. Two, and
#: named as actions rather than yes/no, because the button somebody clicks is
#: what they will remember agreeing to.
DEFAULT_OPTIONS = ("Approve", "Reject")

#: The longest text a person may hand back with an answer. The same bound as
#: revision feedback: room for a task description, not for a pasted log.
MAX_TEXT = 8000

_lock = threading.Lock()
_enabled = False
_next_id = 0
#: request id -> [threading.Event, answer dict or None]
_pending = {}


def configure(enabled):
    """Turn asking on or off. Returns what it is now.

    True only when both halves of the pipe are live - somewhere to ask and a
    channel to be answered on. Either alone is a question nobody could reply to.
    """
    global _enabled
    with _lock:
        _enabled = bool(enabled)
        return _enabled


def enabled():
    with _lock:
        return _enabled


def reset():
    """Forget everything. For tests, and for a second run in one process."""
    global _enabled, _next_id
    with _lock:
        _enabled, _next_id, pending = False, 0, list(_pending.values())
        _pending.clear()
    for slot in pending:
        slot[0].set()


def abandon_all():
    """Release everyone waiting, because no more answers are coming.

    Called when the run is over. A step blocked on an answer would otherwise
    hold the launcher open for the rest of its timeout.
    """
    with _lock:
        pending = list(_pending.items())
        _pending.clear()
    for request_id, slot in pending:
        if slot[1] is None:
            slot[1] = {"answered": False, "answer": "", "who": "",
                       "message": "the run ended before anybody answered"}
        slot[0].set()
        log.debug("question %d abandoned", request_id)


def ask(question, detail="", options=(), timeout_ms=None, step="",
        cancel=None, revision=None, auto="", text=None, choice=None, progress=None):
    """Put a question to whoever is watching. Blocks until answered or not.

    Returns ``{answered, answer, who, message}``. ``answered`` is the only
    field worth branching on: it is False for every way of not getting an
    answer, and a caller that read ``answer`` alone would treat an empty string
    from a timeout as a choice somebody made.

    ``auto`` is the one exception, and it is said: an answer the question takes
    by itself when its time runs out with nobody answering. Then the result is
    answered, with that answer and ``automatic`` true. A window closed by hand
    is still no answer, and a stopped run is still stopped.

    ``text`` asks for written text along with the answer - ``{placeholder,
    default, required}``. The answer then carries ``text``, what was written.

    ``choice`` asks to pick rows from a table - ``{mode, columns,
    value_column, rows}``. The answer then carries ``chosen``, the indexes of
    the rows picked.

    What comes with an answer is handed back as it arrived. The step that
    asked judges it, so a bad one fails that step at once with its reason:
    refusing it here would leave the question waiting out its deadline for an
    answer the window, already closed, is never going to send again.
    """
    if not enabled():
        return {"answered": False, "answer": "", "who": "",
                "message": "asking needs the application: no --control channel "
                           "on this run. A gate that cannot ask does not "
                           "approve."}

    choices = [str(one) for one in (options or DEFAULT_OPTIONS) if str(one).strip()]
    timeout_ms = int(timeout_ms or DEFAULT_TIMEOUT_MS)
    with _lock:
        global _next_id
        _next_id += 1
        request_id = _next_id
        slot = _pending[request_id] = [threading.Event(), None, dict(revision or {}),
                                       dict(text) if text is not None else None,
                                       dict(choice) if choice is not None else None]
    extra = {}
    if text is not None:
        extra["text"] = dict(text)
    if choice is not None:
        extra["choice"] = dict(choice)

    events.emit("cycle.ask", id=request_id, step=step or "",
                question=str(question or ""), detail=str(detail or ""),
                options=choices, timeout_ms=timeout_ms, revision=dict(revision or {}),
                auto=str(auto or ""), **extra)

    if progress is not None:
        progress(step, title=str(question or ""), state=(
            "waiting_for_input" if text is not None or choice is not None else "waiting_for_approval"))
    try:
        answered = _wait(slot[0], timeout_ms / 1000.0, cancel)
    finally:
        if progress is not None:
            progress(step, state="running")
    with _lock:
        _pending.pop(request_id, None)
        answer = slot[1]
    if answered and answer:
        return answer
    if cancel is not None and cancel.is_set():
        # Nobody is waiting for the answer any more, so the window asking for
        # it should not stay up claiming somebody is.
        events.emit("cycle.ask.withdrawn", id=request_id, step=step or "")
        # Deliberately not the token's reason. Stop and the step's deadline both
        # arrive through this token, and the executor already puts whichever it
        # was in front of the step's message - so repeating it here would print
        # "timed out after 30s: timed out after 30s". What this has to add is
        # the part the reason does not say: that nobody answered.
        return {"answered": False, "answer": "", "who": "",
                "message": "nobody answered"}
    if str(auto or "").strip():
        # The window is still up and counting nothing: take it away, the
        # question has been answered.
        events.emit("cycle.ask.withdrawn", id=request_id, step=step or "")
        return {"answered": True, "answer": str(auto).strip(), "who": "",
                "automatic": True,
                "message": "nobody answered within %ds - %r chosen automatically"
                           % (timeout_ms / 1000.0, str(auto).strip())}
    return {"answered": False, "answer": "", "who": "",
            "message": "nobody answered within %ds" % (timeout_ms / 1000.0)}


def _wait(event, seconds, cancel):
    """Wait for the answer, but notice a stopped run rather than sitting it out.

    Polled rather than waited on in one go, because an Event cannot wait on two
    things and a question with a ten minute timeout would otherwise keep the
    launcher open for ten minutes after somebody pressed Stop.
    """
    if cancel is None:
        return event.wait(seconds)
    deadline = seconds
    while deadline > 0:
        if event.wait(min(0.1, deadline)):
            return True
        if cancel.is_set():
            return False
        deadline -= 0.1
    return event.is_set()


def deliver(request_id, answer="", who="", revise=False, feedback="", text="",
            chosen=None):
    """Hand one answer back to whoever is waiting. True when it was wanted.

    Called on the launcher's control thread. An id nobody waits for is ignored
    rather than fatal - an answer that arrives after its step timed out is
    late, not wrong, and taking the control thread down over it would cost
    every other step its Stop.
    """
    try:
        request_id = int(request_id)
    except (TypeError, ValueError):
        return False
    picked = str(answer or "")
    with _lock:
        slot = _pending.get(request_id)
        if slot is None:
            log.debug("answer %s arrived with nobody waiting", request_id)
            return False
        if revise:
            feedback = str(feedback or "").strip()
            if not slot[2].get("enabled") or not feedback or len(feedback) > 8000:
                return False
            slot[1] = {"answered": True, "answer": "", "who": str(who or ""),
                       "revision_requested": True, "feedback": feedback,
                       "message": "plan revision requested"}
        elif picked:
            slot[1] = {"answered": True, "answer": picked, "who": str(who or ""),
                       "message": "answered %r" % picked}
            # Only for a question that asked for them: text or rows sent with
            # a plain question are not something anybody will read.
            if slot[3] is not None:
                slot[1]["text"] = str(text or "").strip()
            if slot[4] is not None:
                slot[1]["chosen"] = (list(chosen) if isinstance(chosen, (list, tuple))
                                     else chosen)
        else:
            # A window closed without a decision. Delivered rather than left
            # silent, so the run is not blocked for the rest of its deadline by
            # somebody who has already decided not to look - but it is not an
            # answer, because none of the answers on offer is spelled this way.
            slot[1] = {"answered": False, "answer": "", "who": str(who or ""),
                       "message": "the question was dismissed without an answer"}
    slot[0].set()
    return True
