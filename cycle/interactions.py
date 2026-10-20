"""What a person says to a run while it is going, and whether it got there.

A cycle runs for minutes or hours, and the person watching it often knows
something the task did not say - *this method is also called from cron* - at the
moment they see the work heading the wrong way. This module is where such a
message is kept, where it is headed, and the honest account of what became of
it.

**Three parties, three jobs.** A front-end shows who can be told what and sends
a request. The executor - the one thread that owns the run - judges the request
against the run as it is *now* and only then has this module write it down. A
plugin reads messages at points of its own choosing through :class:`Inbox`, and
says when one actually reached whatever it drives. Nothing here knows what an
agent is: a plugin says which kinds of message it takes
(``PluginMetadata.accepts``), and what it does with them is its own business.

**Kept is not delivered.** A message is *pending* once it is durably recorded,
and *delivered* only when a plugin says it went into something - for an agent,
into the input of one particular call. Between the two it may become
*undelivered* - the step or the run ended before another point came - and it
says why. Delivered means handed over, never followed: whether the work took it
into account is what the work and its checks show.

**A point is a snapshot, and a snapshot never changes.** At each point a plugin
takes the messages that apply to it then, oldest first, and that list is saved
before the plugin uses it. A message accepted afterwards belongs to the next
point; acceptance and snapshots share one lock and one sequence, so which side
of a snapshot a message falls on is never a matter of luck. Taking the same
point again - the step running again after Resume - returns the very same list,
which is what lets a resumed step rebuild the same input and so find the answer
it already paid for instead of asking again. That is why an empty snapshot is
saved too.

**Two scopes.** A message to a *step* is for one execution of it: if that
execution ends first, the message says it did not arrive, and it is never
passed on to another step or to the same step started again. A message to the
*run* is for the rest of it: every step that takes messages reads it at its next
point, each such step is a delivery of its own, and the first reader does not
use it up. A stop closes whatever has not arrived; what did arrive stays part
of the run, so a Resume of it still hears it.

**What an earlier step was told travels with its work.** A message to a step
is for that step - but the steps after it that *judge* its work have to know
what it was told, or they judge work that followed a person's correction
against the task as it stood before, and send it back for following it. So a
point also reads every message **delivered** to a step upstream of it in the
graph, in that step's current execution, marked as having been said to that
step. Only what arrived travels: a message that never reached its step is not
handed on to anyone, and a message to a step running beside this one is not
this one's business.

**The execution of a step** is the step plus a number that grows only when a
plan revision sends the step round again (see ``cycle/revisions.py``). Resume
and a plugin's own ``retry`` continue the same execution - they are the same
work finishing, not new work - so a message waiting for one survives them.

Nothing crosses into another run. A partial run starts with an empty history,
and no message is ever turned into memory or into anything a later run reads.
"""

import copy
from contextlib import contextmanager
import json
import logging
import os
import re
import threading
import time

from cycle import checkpoints, run as records
from domain.cycle import PENDING as STEP_PENDING, SKIPPED, TERMINAL

log = logging.getLogger("cycle.interactions")

#: The version of both a message and the file. A reader meeting a larger number
#: refuses rather than guessing at a format it does not know.
SCHEMA = 1

#: Where a run keeps what it was told, beside its other execution records.
FILE = "interactions.json"

#: Who a message is for: one execution of one step, or the rest of the run.
STEP = "step"
RUN = "run"
SCOPES = (STEP, RUN)

#: What a message can be. All three are part of the format so a request can
#: name them; only :data:`ROUTED` is taken, and the others are refused with a
#: reason until something handles them - a front-end must not offer a kind the
#: core would only drop.
TEXT = "text"
ANSWER = "answer"
ACTION = "action"
KINDS = (TEXT, ANSWER, ACTION)
ROUTED = (TEXT,)

#: What became of a message.
PENDING = "pending"            # saved, not yet handed to anything
DELIVERED = "delivered"        # a plugin says it went into something
REJECTED = "rejected"          # judged and refused when it arrived; see reason
UNDELIVERED = "undelivered"    # its addressee ended first; see reason
UNCERTAIN = "uncertain"        # handed to a call whose outcome was not recorded

#: A delivery in progress: in the input of a call that has not come back yet.
HANDING = "handing"

#: What a step's offer says while it has not started.
WHEN_PENDING = "When it starts"
#: And the run's, which is every step still to come.
WHEN_RUN = "At the next point of every step still to come that takes messages"

MAX_TEXT = 8000
MAX_MESSAGES = 200
MAX_WHO = 200
MAX_PAYLOAD = 32768
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

#: What an offer is in, by the step's own state.
OFFER_PENDING = "pending"
OFFER_RUNNING = "running"
OFFER_WAITING = "waiting"


def parse_request(raw):
    """``(request, problem)`` for one command off the control channel.

    Only the shape is checked here - which is all the transport may check. Who
    the message may go to depends on the run as it is when the executor gets
    to it, so that is judged there (:meth:`RunInteractions.accept`).
    """
    if not isinstance(raw, dict):
        return None, "A message must be a JSON object."
    message_id = raw.get("id")
    if not isinstance(message_id, str) or not _ID.match(message_id):
        return None, ("A message needs an id of letters, digits, '.', '_', ':' "
                      "or '-', at most 128 characters long.")
    run_id = raw.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None, "A message must name the run it is for."
    scope = raw.get("scope", STEP)
    if scope not in SCOPES:
        return None, "Unknown scope %r: a message goes to a step or to the run." % (scope,)
    step = raw.get("step") or ""
    if not isinstance(step, str):
        return None, "A step is named by its id."
    if scope == STEP and not step:
        return None, "A message to a step must name the step."
    execution = raw.get("execution")
    if execution is not None and (isinstance(execution, bool)
                                  or not isinstance(execution, int) or execution < 1):
        return None, "An execution is a whole number from 1."
    kind = raw.get("kind", TEXT)
    if kind not in KINDS:
        return None, "Unknown kind of message %r." % (kind,)
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        return None, "A message needs a payload object."
    try:
        if len(json.dumps(payload, ensure_ascii=False)) > MAX_PAYLOAD:
            return None, "A message's payload is too large."
    except (TypeError, ValueError):
        return None, "A message's payload must be plain JSON."
    who = raw.get("who", "")
    if not isinstance(who, str) or len(who) > MAX_WHO:
        return None, "Who sent a message is a short piece of text."
    offer = raw.get("offer")
    if offer is not None and (isinstance(offer, bool) or not isinstance(offer, int)
                              or offer < 0):
        return None, "An offer version is a whole number."
    return {"id": message_id, "run_id": run_id, "scope": scope,
            "step": step if scope == STEP else "", "execution": execution
            if scope == STEP else None, "kind": kind,
            "payload": copy.deepcopy(payload), "who": who, "offer": offer,
            "source": str(raw.get("source") or "control")[:40]}, ""


def execution_of(run, step_id):
    """Which execution of ``step_id`` this run is on, or would start next.

    One more than the number of plan revisions that sent the step round again.
    Derived rather than stored, from a list the run record already keeps and
    Resume already carries, so it cannot drift from what actually happened.
    """
    rounds = sum(1 for one in (getattr(run, "revisions", None) or [])
                 if step_id in (one.get("steps") or []))
    retries = sum(1 for one in getattr(run, "control_history", [])
                  if one.get("action") == "retry_phase" and step_id in one.get("steps", []))
    return 1 + rounds + retries


def kinds_of(plugin):
    """The kinds of message a plugin takes, from its metadata."""
    metadata = getattr(plugin, "metadata", None)
    return tuple(getattr(one, "kind", "") for one in getattr(metadata, "accepts", ()) or ())


def hint_of(plugin, kind=TEXT):
    """What a plugin says about when it reads ``kind``, for the offer."""
    metadata = getattr(plugin, "metadata", None)
    for one in getattr(metadata, "accepts", ()) or ():
        if getattr(one, "kind", "") == kind:
            return getattr(one, "when", "") or ""
    return ""


def path_of(execution_directory):
    return os.path.join(execution_directory, FILE)


def _content(request):
    """What makes two requests with one id the same request."""
    return checkpoints.digest({key: request.get(key) for key in
                               ("scope", "step", "execution", "kind", "payload")})


def _receipt_state(workspace, operation):
    """A saved outcome, None for no call, or ``unknown`` for an unreadable receipt."""
    if not operation:
        return None
    path = os.path.join(workspace, operation, "receipt.json")
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
        state = record.get("state") if isinstance(record, dict) else None
        if state == "complete" and record.get("input_delivered") is False:
            return None
        return state if state in ("complete", "started") else "unknown"
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        # An unreadable receipt is not evidence that its call never ran.
        return "unknown"


class Snapshot(object):
    """The messages one point of one step reads. Never changes once taken.

    ``items`` are copies, oldest first: ``{id, scope, text, who, accepted_at,
    new}``. ``new`` is whether this consumer has not been handed it before -
    the list itself is cumulative, because a plugin whose every call starts
    afresh (an agent run from a prompt) must be told again what it was told
    last time, or the second attempt forgets what the first was asked.

    An empty snapshot is falsy. One with no ``key`` was never recorded - taken
    outside a run, or after the run stopped taking messages - and binding or
    confirming it does nothing.
    """

    def __init__(self, key="", step="", execution=0, point="", items=(), digest=""):
        self.key = key
        self.step = step
        self.execution = execution
        self.point = point
        self.items = [dict(one) for one in items]
        self.digest = digest

    def __bool__(self):
        return bool(self.items)

    def __len__(self):
        return len(self.items)

    def __repr__(self):
        return "Snapshot(%r, %d item(s))" % (self.key, len(self.items))


class RunInteractions(object):
    """Every message one run was sent, its snapshots, and what it offers now.

    One per run. The executor judges requests on its own thread and plugins
    take snapshots on theirs, so everything here is behind one lock - which is
    also what orders an acceptance against a snapshot. Every change is written
    to disk before it is announced: an event is a diagnostic that may be lost,
    and must never be the only account of a message somebody was told arrived.
    """

    def __init__(self, directory, run_id, observer=None, workspace=""):
        self.path = path_of(directory)
        self.run_id = run_id
        self.workspace = workspace or os.path.dirname(os.path.abspath(directory))
        self._observer = observer
        self._lock = threading.RLock()
        self._messages = {}                   # id -> record, in seq order
        self._snapshots = {}                  # key -> record
        self._seq = 0
        #: step id -> the offer it makes now. Not saved: it is a fact about
        #: this process, and a resumed run makes its offers afresh.
        self._offers = {}
        self._active = {}                     # step id -> execution in flight
        self._withdrawn = set()               # (step, execution) with no point left
        self._version = 0
        self._closed = False
        self._run = self._cycle = self._registry = None

    # -- reading back ---------------------------------------------------------
    def load(self):
        """Read what an earlier attempt at this run left, and settle it.

        A delivery that was in the middle of happening when the process died is
        settled by the receipt of the call it was bound to: a complete receipt
        means the call ran with it, so it arrived; a started one means nobody
        can say, which is what the message then says; no receipt means the
        call never began, and the message is simply still waiting.
        """
        if not os.path.exists(self.path):
            return
        document = checkpoints._read(self.path)
        if not isinstance(document, dict):
            raise checkpoints.CheckpointError("Unreadable message history: %s" % self.path)
        schema = document.get("schema")
        if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1:
            raise checkpoints.CheckpointError("Unreadable message history: invalid schema.")
        if schema > SCHEMA:
            raise checkpoints.CheckpointError(
                "This run's messages were written by a newer version of QAVector.")
        if document.get("run_id") != self.run_id:
            raise checkpoints.CheckpointError("Message history belongs to a different run.")
        self._validate_history(document)
        with self._transaction():
            messages = document.get("messages") or []
            messages.sort(key=lambda one: one.get("seq") or 0)
            self._messages = {one["id"]: one for one in messages}
            self._snapshots = {one["key"]: one for one in document.get("snapshots") or []
                               if isinstance(one, dict) and one.get("key")}
            self._seq = max([int(document.get("seq") or 0)]
                            + [int(one.get("seq") or 0) for one in messages])
            changed = False
            for message in self._messages.values():
                for delivery in list(message.get("deliveries") or []):
                    if delivery.get("state") != HANDING:
                        continue
                    state = _receipt_state(self.workspace, delivery.get("operation"))
                    if state == "complete":
                        delivery["state"] = DELIVERED
                        delivery["note"] = "confirmed from the saved receipt of its call"
                    elif state in ("started", "unknown"):
                        delivery["state"] = UNCERTAIN
                    else:
                        message["deliveries"].remove(delivery)
                    changed = True
                changed = self._settle(message) or changed
            if changed:
                self._save()

    # -- the run's side -------------------------------------------------------
    def open(self, run, cycle, registry):
        """Start taking messages for ``run``. Called once, as it begins.

        A resumed run first closes what can no longer arrive - a message to a
        step that finished before the process went down - and then says again
        everything it was told, because whatever is watching starts from an
        empty page.
        """
        with self._transaction():
            self._run, self._cycle, self._registry = run, cycle, registry
            self._closed = False
            changed = []
            for message in self._messages.values():
                if message["state"] != PENDING or message["scope"] != STEP:
                    continue
                step_run = (run.steps or {}).get(message["step"])
                current = execution_of(run, message["step"])
                if step_run is None or step_run.status in TERMINAL:
                    message["state"] = UNDELIVERED
                    message["reason"] = ("%s had already finished, so it never read it."
                                         % message["step"])
                    changed.append(message)
                elif message["execution"] != current:
                    message["state"] = UNDELIVERED
                    message["reason"] = ("The %s it was written to was sent round "
                                         "again before it read it." % message["step"])
                    changed.append(message)
            for step in cycle.steps:
                step_run = (run.steps or {}).get(step.id)
                plugin = registry.get(step.plugin)
                if (step_run is not None and step_run.status == STEP_PENDING
                        and TEXT in kinds_of(plugin)):
                    self._offers[step.id] = self._offer(step.id, execution_of(run, step.id),
                                                        OFFER_PENDING, WHEN_PENDING)
            if changed:
                self._save()
            for message in self._ordered():
                self._emit(message)
            self._publish()

    def accept(self, request):
        """Judge one request and write it down. ``(record, refusal)``.

        Called on the executor's own thread, so the run it reads is the run as
        it is. A request judged and refused is written down too - the person
        wrote something, and the record should say what became of it - and a
        request sent again with the same id and content gets the same answer
        rather than a second message. Only what cannot be recorded at all (a
        conflicting id, a run that is finishing, a disk that refused) comes
        back as a refusal with no record.
        """
        with self._lock:
            existing = self._messages.get(request["id"])
            if existing is not None:
                if existing.get("content") == _content(request):
                    self._emit(existing)
                    refusal = existing.get("reason", "") if existing["state"] == REJECTED else ""
                    return copy.deepcopy(existing), refusal
                return None, self._refuse(request["id"], (
                    "A message with id %s was already sent with different content; "
                    "the first one stands." % request["id"]))
            if self._closed:
                return None, self._refuse(request["id"],
                                          "The run is finishing; it no longer takes messages.")
            if len(self._messages) >= MAX_MESSAGES:
                return None, self._refuse(request["id"], (
                    "This run already holds %d messages, the most it keeps." % MAX_MESSAGES))
            problem, execution = self._judge(request)
            self._seq += 1
            record = {"schema": SCHEMA, "id": request["id"], "run_id": self.run_id,
                      "seq": self._seq, "scope": request["scope"],
                      "step": request["step"], "execution": execution,
                      "kind": request["kind"], "payload": copy.deepcopy(request["payload"]),
                      "who": request.get("who", ""),
                      "source": request.get("source", ""),
                      "offer": request.get("offer"), "content": _content(request),
                      "accepted_at": time.time(),
                      "state": REJECTED if problem else PENDING, "reason": problem,
                      "deliveries": []}
            self._messages[record["id"]] = record
            try:
                self._save()
            except checkpoints.CheckpointError as exc:
                del self._messages[record["id"]]
                self._seq -= 1
                return None, self._refuse(request["id"], (
                    "The message could not be saved, so it was not taken: %s" % exc))
            self._emit(record)
            return copy.deepcopy(record), problem

    def _judge(self, request):
        """``(why not, execution)`` - why not is empty when it may go ahead."""
        run, cycle, registry = self._run, self._cycle, self._registry
        stale = (request.get("offer") is not None and request["offer"] < self._version)

        def said(problem):
            return problem + (" The list you chose from was out of date." if stale else "")

        if request["run_id"] != self.run_id:
            return "The message names a different run.", request.get("execution")
        if request["kind"] not in ROUTED:
            return (said("A message of kind %s is not taken yet; only text is."
                         % request["kind"]), request.get("execution"))
        text = request["payload"].get("text")
        if not isinstance(text, str) or not text.strip():
            return "A message needs some text.", request.get("execution")
        if len(text) > MAX_TEXT:
            return ("A message may be at most %d characters long." % MAX_TEXT,
                    request.get("execution"))
        if request["scope"] == RUN:
            if not self._offers:
                return said("No step still to come in this run takes messages."), None
            return "", None
        step_id = request["step"]
        step = cycle.step(step_id) if cycle is not None else None
        if step is None:
            return said("%s is not a step of this run." % step_id), request.get("execution")
        current = execution_of(run, step_id)
        if request.get("execution") not in (None, current):
            return said("The %s you wrote to is over: it has been sent round again "
                        "since. Choose it again to write to the one going now."
                        % step_id), request["execution"]
        if TEXT not in kinds_of(registry.get(step.plugin) if registry else None):
            return said("%s (%s) does not take messages." % (step_id, step.plugin)), current
        step_run = (run.steps or {}).get(step_id)
        status = step_run.status if step_run is not None else ""
        if status in TERMINAL:
            return said("%s has already finished (%s)." % (step_id, status)), current
        offer = self._offers.get(step_id)
        if offer is None or TEXT not in offer["kinds"]:
            return said("%s has no point left at which it could read a message."
                        % step_id), current
        return "", current

    def begin(self, step_id, execution, plugin):
        """A step is starting, or starting again after a wait."""
        with self._lock:
            self._active[step_id] = execution
            if (self._closed or TEXT not in kinds_of(plugin)
                    or (step_id, execution) in self._withdrawn):
                return
            offer = self._offers.get(step_id)
            if (offer is not None and offer["execution"] == execution
                    and offer["state"] != OFFER_PENDING):
                # Back from a wait: the same execution, and whatever the plugin
                # last said about when it reads next still holds.
                offer["state"] = OFFER_RUNNING
            else:
                self._offers[step_id] = self._offer(step_id, execution, OFFER_RUNNING,
                                                    hint_of(plugin))
            self._publish()

    def park(self, step_id):
        """A step asked to be come back to. It keeps its offer."""
        with self._lock:
            offer = self._offers.get(step_id)
            if offer is not None and offer["state"] == OFFER_RUNNING:
                offer["state"] = OFFER_WAITING
                self._publish()

    def finish(self, step_id, status, started=True, reason=""):
        """A step reached its verdict: nothing more can reach it.

        What was waiting for it says it did not arrive, and why - ``reason``
        when the caller knows better than the status does, as when the step
        only ended because somebody stopped the run. What was in the middle of
        being handed over when the step ended cannot be said to have arrived
        or not, and says that instead.
        """
        with self._transaction():
            execution = self._active.pop(step_id, None)
            offer = self._offers.pop(step_id, None)
            if execution is None:
                execution = (offer["execution"] if offer is not None
                             else execution_of(self._run, step_id) if self._run else 1)
            if reason:
                pass
            elif status == SKIPPED:
                reason = "%s was skipped, so it never read it." % step_id
            elif not started:
                reason = "%s did not start (%s), so it never read it." % (step_id, status)
            else:
                reason = ("%s ended (%s) before it reached another point where it "
                          "reads messages." % (step_id, status))
            changed = []
            for message in self._messages.values():
                touched = False
                for delivery in message.get("deliveries") or []:
                    if (delivery.get("step") == step_id
                            and delivery.get("execution") == execution
                            and delivery.get("state") == HANDING):
                        delivery["state"] = UNCERTAIN
                        touched = True
                if (message["scope"] == STEP and message["step"] == step_id
                        and message["execution"] == execution
                        and message["state"] == PENDING and not touched):
                    message["state"] = UNDELIVERED
                    message["reason"] = reason
                    touched = True
                if (message["scope"] == RUN
                        and message["state"] not in (REJECTED, UNDELIVERED)
                        and self._takes_text(step_id)
                        and not any(one.get("step") == step_id
                                    and one.get("execution") == execution
                                    for one in message.get("deliveries") or [])):
                    message["deliveries"].append({
                        "step": step_id, "execution": execution, "point": "",
                        "state": UNDELIVERED, "reason": reason,
                        "at": time.time(), "operation": ""})
                    touched = True
                if touched:
                    self._settle(message)
                    changed.append(message)
            if changed:
                self._save()
                for message in changed:
                    self._emit(message)
            if offer is not None:
                self._publish()

    def reoffer(self, step_ids):
        """Steps a plan revision sent round again: each is a new execution."""
        with self._lock:
            if self._closed or self._run is None:
                return
            for step_id in step_ids:
                self._active.pop(step_id, None)
                step = self._cycle.step(step_id)
                plugin = self._registry.get(step.plugin) if step is not None else None
                if step is not None and TEXT in kinds_of(plugin):
                    self._offers[step_id] = self._offer(
                        step_id, execution_of(self._run, step_id), OFFER_PENDING,
                        WHEN_PENDING)
            self._publish()

    def close(self, reason):
        """The run takes no more messages. What has not arrived never will.

        A message in the middle of being handed over is left for that call to
        settle: its step may still come back and say it went in.
        """
        with self._transaction():
            if self._closed:
                return
            self._closed = True
            self._offers.clear()
            changed = []
            for message in self._messages.values():
                if message["state"] != PENDING:
                    continue
                if any(one.get("state") == HANDING for one in message.get("deliveries") or []):
                    continue
                message["state"] = UNDELIVERED
                message["reason"] = (reason if message["scope"] == STEP else
                                     "No step read it before the run ended.")
                changed.append(message)
            if changed:
                self._save()
                for message in changed:
                    self._emit(message)
            self._publish()

    def seal(self):
        """The run is over. A delivery still in progress now never will be known."""
        with self._transaction():
            self._closed = True
            changed = []
            for message in self._messages.values():
                touched = False
                for delivery in message.get("deliveries") or []:
                    if delivery.get("state") == HANDING:
                        delivery["state"] = UNCERTAIN
                        touched = True
                if touched:
                    self._settle(message)
                    changed.append(message)
            if changed:
                self._save()
                for message in changed:
                    self._emit(message)

    # -- the plugin's side ----------------------------------------------------
    def take(self, step_id, point, last=False, reached=False):
        """The snapshot for one point of the step going now. See :class:`Inbox`."""
        point = str(point or "")
        with self._transaction():
            execution = self._active.get(step_id)
            if execution is None or not point or not self._takes_text(step_id):
                return Snapshot(step=step_id, point=point)
            key = "%s#%d/%s" % (step_id, execution, point)
            found = self._snapshots.get(key)
            if found is None:
                if self._closed:
                    # Not recorded: the run is on its way down, and a point
                    # taken now may never be used. Resume takes it afresh.
                    return Snapshot(step=step_id, execution=execution, point=point)
                # A legacy receipt fixes the inputs of the original work.
                # A plan revision explicitly starts new work with new inputs.
                reached = reached and execution == 1
                earlier = self._upstream(step_id)
                items = ([] if reached else
                         [one["id"] for one in self._ordered()
                          if self._applies(one, step_id, execution, earlier)])
                found = {"key": key, "step": step_id, "execution": execution,
                         "point": point, "upto": self._seq, "items": items,
                         "digest": checkpoints.digest(
                             [[one, self._messages[one]["payload"]] for one in items]),
                         "taken_at": time.time(), "operation": "",
                         "delivered_at": None, "reached_before": bool(reached)}
                found["new"] = [one for one in items
                                if not self._previously_delivered(
                                    self._messages[one], found)]
                self._snapshots[key] = found
                try:
                    self._save()
                except checkpoints.CheckpointError:
                    del self._snapshots[key]
                    raise
            if last:
                self._withdraw(step_id, execution)
            return self._view(found)

    def offer(self, step_id, when):
        """A running step says, in its own words, when it will read next."""
        with self._lock:
            offer = self._offers.get(step_id)
            when = str(when or "")
            if offer is not None and offer["state"] != OFFER_PENDING and offer["when"] != when:
                offer["when"] = when
                self._publish()

    def bind(self, snapshot, operation):
        """The snapshot is going into the call recorded at ``operation``.

        Saved before the call begins, so a process that dies during it leaves
        enough behind for :meth:`load` to settle the delivery from the call's
        own receipt.
        """
        self._mark(snapshot, HANDING, operation, strict=True)

    def delivered(self, snapshot, operation=""):
        """The snapshot's messages went into the call - into its input."""
        self._mark(snapshot, DELIVERED, operation, strict=True)

    def release(self, snapshot, reason=""):
        """The call never began. Its messages go back to waiting."""
        with self._transaction():
            record = self._snapshots.get(getattr(snapshot, "key", ""))
            if record is None or not record["items"]:
                return
            changed = []
            for message_id in record["items"]:
                message = self._messages.get(message_id)
                if message is None:
                    continue
                before = len(message.get("deliveries") or [])
                message["deliveries"] = [
                    one for one in message.get("deliveries") or []
                    if not (self._same(one, record) and one.get("state") == HANDING)]
                if len(message["deliveries"]) != before:
                    self._settle(message)
                    handed = any(one.get("state") in (HANDING, DELIVERED, UNCERTAIN)
                                 for one in message["deliveries"])
                    if message["state"] == PENDING and not handed:
                        # Back to waiting - unless there is nothing left for it
                        # to wait for, which is the one thing close() and
                        # finish() could not decide while the call was pending.
                        if self._closed:
                            message["state"] = UNDELIVERED
                            message["reason"] = (
                                "The run ended before it was read."
                                if message["scope"] == STEP else
                                "No step read it before the run ended.")
                        elif (message["scope"] == STEP and self._active.get(message["step"])
                              != message["execution"]):
                            message["state"] = UNDELIVERED
                            message["reason"] = reason or "Its call never began."
                    changed.append(message)
            if changed:
                self._save()
                for message in changed:
                    self._emit(message)

    # -- reading --------------------------------------------------------------
    def messages(self):
        with self._lock:
            return [copy.deepcopy(one) for one in self._ordered()]

    def offers(self):
        with self._lock:
            return self._table()

    def snapshots(self):
        with self._lock:
            return copy.deepcopy(list(self._snapshots.values()))

    # -- the machinery --------------------------------------------------------
    def _mark(self, snapshot, state, operation, strict):
        with self._transaction():
            record = self._snapshots.get(getattr(snapshot, "key", ""))
            if record is None or not record["items"]:
                return
            if operation:
                record["operation"] = operation
            if state == DELIVERED:
                record["delivered_at"] = record.get("delivered_at") or time.time()
            changed = []
            for message_id in record["items"]:
                message = self._messages.get(message_id)
                if message is None:
                    continue
                delivery = next((one for one in message.setdefault("deliveries", [])
                                 if self._same(one, record)), None)
                if delivery is None:
                    delivery = {"step": record["step"], "execution": record["execution"],
                                "point": record["point"], "state": "", "at": None,
                                "operation": ""}
                    message["deliveries"].append(delivery)
                if delivery["state"] == DELIVERED:
                    continue              # said already: confirming twice is once
                delivery["state"] = state
                delivery["at"] = time.time()
                delivery["operation"] = record["operation"]
                self._settle(message)
                changed.append(message)
            if changed:
                if strict:
                    self._save()
                else:
                    self._save()
                for message in changed:
                    self._emit(message)

    @staticmethod
    def _same(delivery, snapshot):
        return (delivery.get("step") == snapshot["step"]
                and delivery.get("execution") == snapshot["execution"]
                and delivery.get("point") == snapshot["point"])

    def _settle(self, message):
        """Bring a message's own state into line with its deliveries."""
        if message["state"] in (REJECTED,):
            return False
        states = [one.get("state") for one in message.get("deliveries") or []]
        before = (message["state"], message.get("reason", ""))
        if DELIVERED in states:
            message["state"], message["reason"] = DELIVERED, ""
        elif UNCERTAIN in states:
            message["state"] = UNCERTAIN
            message["reason"] = ("It went into a call whose outcome was not "
                                 "recorded, so whether that call read it is unknown.")
        elif message["state"] in (DELIVERED, UNCERTAIN):
            message["state"], message["reason"] = PENDING, ""
        return before != (message["state"], message.get("reason", ""))

    def _applies(self, message, step_id, execution, earlier=()):
        if (message.get("kind") != TEXT
                or message["state"] not in (PENDING, DELIVERED, UNCERTAIN)):
            return False
        if message["scope"] != STEP:
            return True
        if message["step"] == step_id:
            return message["execution"] == execution
        # Said to a step this one comes after: context for judging its work,
        # but only once it arrived there, and only for the execution of that
        # step whose work this one is going to see.
        return (message["step"] in earlier
                and message["execution"] == execution_of(self._run, message["step"])
                and any(one.get("state") == DELIVERED and one.get("step") == message["step"]
                        for one in message.get("deliveries") or []))

    def _upstream(self, step_id):
        """Every step whose work reaches ``step_id``, however far back."""
        if self._cycle is None:
            return set()
        from cycle import model
        return model.upstream(self._cycle, {step_id}) - {step_id}

    def _view(self, record):
        items = []
        for message_id in record["items"]:
            message = self._messages.get(message_id)
            if message is None:
                continue
            new = (message_id in record["new"] if "new" in record else
                   not self._previously_delivered(message, record))
            items.append({"id": message_id, "scope": message["scope"],
                          # Who it was said to, and whether that is somebody
                          # else - an earlier step, whose work this one sees.
                          "to": message.get("step", ""),
                          "earlier": (message["scope"] == STEP
                                      and message.get("step") != record["step"]),
                          "text": str((message.get("payload") or {}).get("text") or ""),
                          "who": message.get("who", ""),
                          "accepted_at": message.get("accepted_at"),
                          "new": new})
        return Snapshot(record["key"], record["step"], record["execution"],
                        record["point"], items, record.get("digest", ""))

    @staticmethod
    def _previously_delivered(message, snapshot):
        return any(one.get("state") == DELIVERED
                   and one.get("step") == snapshot["step"]
                   and one.get("execution") == snapshot["execution"]
                   and one.get("point") != snapshot["point"]
                   and (one.get("at") or 0) <= snapshot["taken_at"]
                   for one in message.get("deliveries") or [])

    def _takes_text(self, step_id):
        step = self._cycle.step(step_id) if self._cycle is not None else None
        plugin = self._registry.get(step.plugin) if step is not None else None
        return TEXT in kinds_of(plugin)

    @contextmanager
    def _transaction(self):
        """Keep the in-process view at the last durable state if a write fails."""
        with self._lock:
            before = copy.deepcopy((self._messages, self._snapshots, self._seq))
            try:
                yield
            except checkpoints.CheckpointError:
                self._messages, self._snapshots, self._seq = before
                raise

    @staticmethod
    def _validate_history(document):
        """Do not silently drop damaged messages or rebuild a different snapshot."""
        try:
            messages, snapshots = document["messages"], document["snapshots"]
            if not isinstance(messages, list) or not isinstance(snapshots, list):
                raise ValueError("messages and snapshots must be lists")
            seq = document["seq"]
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
                raise ValueError("invalid sequence")
            ids = set()
            for one in messages:
                if (not isinstance(one, dict) or not isinstance(one.get("id"), str)
                        or not one["id"] or one["id"] in ids
                        or not isinstance(one.get("seq"), int)
                        or one.get("scope") not in SCOPES
                        or one.get("state") not in (PENDING, DELIVERED, REJECTED,
                                                    UNDELIVERED, UNCERTAIN)
                        or not isinstance(one.get("payload"), dict)
                        or not isinstance(one.get("deliveries"), list)
                        or not all(isinstance(item, dict) for item in one["deliveries"])):
                    raise ValueError("invalid or repeated message")
                if one["scope"] == STEP and (not one.get("step")
                                             or "execution" not in one):
                    raise ValueError("missing step address")
                ids.add(one["id"])
            keys = set()
            for one in snapshots:
                if (not isinstance(one, dict) or not isinstance(one.get("key"), str)
                        or one["key"] in keys or not one.get("step")
                        or not isinstance(one.get("execution"), int)
                        or not isinstance(one.get("point"), str)
                        or not isinstance(one.get("taken_at"), (int, float))
                        or not isinstance(one.get("items"), list)
                        or any(not isinstance(item, str) or item not in ids
                               for item in one["items"])):
                    raise ValueError("invalid snapshot or missing message")
                keys.add(one["key"])
        except (KeyError, TypeError, ValueError) as exc:
            raise checkpoints.CheckpointError("Unreadable message history: %s." % exc) from exc

    def _withdraw(self, step_id, execution):
        self._withdrawn.add((step_id, execution))
        if self._offers.pop(step_id, None) is not None:
            self._publish()

    def _offer(self, step_id, execution, state, when):
        return {"step": step_id, "execution": execution, "kinds": [TEXT],
                "state": state, "when": when or ""}

    def _table(self):
        steps = {name: dict(one, kinds=list(one["kinds"]))
                 for name, one in self._offers.items()}
        return {"version": self._version,
                "run": ({"kinds": [TEXT], "when": WHEN_RUN} if steps else None),
                "steps": steps}

    def _publish(self):
        self._version += 1
        if self._observer is None:
            return
        try:
            self._observer.interaction_offers(self._table())
        except Exception:                     # observers are diagnostics
            pass

    def _emit(self, message):
        if self._observer is None:
            return
        try:
            self._observer.interaction(copy.deepcopy(message))
        except Exception:                     # observers are diagnostics
            pass

    def _refuse(self, message_id, reason):
        if self._observer is not None:
            try:
                self._observer.interaction_refused(message_id, reason)
            except Exception:                 # observers are diagnostics
                pass
        return reason

    def _ordered(self):
        return sorted(self._messages.values(), key=lambda one: one.get("seq") or 0)

    def _document(self):
        return {"schema": SCHEMA, "run_id": self.run_id, "seq": self._seq,
                "messages": self._ordered(),
                "snapshots": sorted(self._snapshots.values(),
                                    key=lambda one: one.get("taken_at") or 0)}

    def _save(self):
        try:
            records._atomic(self.path, self._document())
        except (OSError, ValueError, TypeError) as exc:
            raise checkpoints.CheckpointError(
                "Cannot save execution state: messages: %s" % exc) from exc

    def _save_quietly(self):
        """For the way down, where there is nobody left to tell about a failure."""
        try:
            self._save()
        except checkpoints.CheckpointError as exc:
            log.warning("%s", exc)


class Inbox(object):
    """One step's way to its messages - and nothing more.

    What a plugin is handed (``context.inbox(step.id)``). It cannot see other
    steps' messages, confirm another step's snapshot or change the run: it
    takes what applies to it at a point, and says what became of it.

    The pattern, around one call::

        snapshot = inbox.take("attempt-2")      # saved before it is used
        ... build the call's input from snapshot.items ...
        inbox.bind(snapshot, "steps/x/operations/<key>")   # before the call
        ... make the call ...
        inbox.delivered(snapshot)                # it went in
        # or inbox.release(snapshot) when the call never began

    **A point's name is used once per execution.** Taking it again returns the
    snapshot already taken - which is how a step run again after Resume gets
    the same input it had, and so the same recorded answer. A plugin that asks
    at every turn of a loop names each turn.

    ``last`` says this is the step's final point: its offer is withdrawn, so a
    message sent from now on is refused with that reason instead of being
    accepted for a point that will never come. ``reached`` says the plugin
    already knows this point happened before messages were recorded here (a
    run begun by an older version): it then reads nothing new.

    Outside a run - a plugin under test, a command run by hand - every call
    does nothing and ``take`` returns an empty snapshot.
    """

    def __init__(self, interactions, step_id):
        self._interactions = interactions
        self._step = step_id

    def take(self, point, last=False, reached=False):
        if self._interactions is None:
            return Snapshot(step=self._step, point=str(point or ""))
        return self._interactions.take(self._step, point, last=last, reached=reached)

    def offer(self, when):
        if self._interactions is not None:
            self._interactions.offer(self._step, when)

    def bind(self, snapshot, operation):
        if self._mine(snapshot):
            self._interactions.bind(snapshot, operation)

    def delivered(self, snapshot, operation=""):
        if self._mine(snapshot):
            self._interactions.delivered(snapshot, operation)

    def release(self, snapshot, reason=""):
        if self._mine(snapshot):
            self._interactions.release(snapshot, reason)

    def _mine(self, snapshot):
        return (self._interactions is not None and snapshot is not None
                and getattr(snapshot, "key", "") and snapshot.step == self._step)
