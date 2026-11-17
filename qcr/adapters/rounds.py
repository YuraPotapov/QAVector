"""When a conversation that handed work on is woken with the results (ADR-009).

A conversation's agent hands tasks and questions to other agents in its turns.
What this module keeps, per delegating conversation:

* **items** - each task or question, its ``name`` if the agent gave one, the
  turn (round) it was handed on in, whether it ended, and whether the agent
  already **saw** its result (it got the ended execution through ``wait`` or
  ``get``);
* **conditions** - when the agent asked to be woken (``agents.tasks.wake_when``):
  all of some named items ended, any of them, and - with ``on_failure`` - at
  once when one fails. A round with no condition of its own wakes the agent
  when every item of it has ended.

A condition fires only once the turn that set it has ended: an agent still in
its turn sees for itself. It wakes the conversation only for results the
agent has not seen; one that waited for everything is not woken again for it.
Waking is telling: ``notify({"type": "round_finished", ...})`` - the owner of
the conversation (the GUI) decides whether a turn starts (ADR-009 D3, D5).

Thread-safe; nothing here asks any agent or service.
"""

import itertools
import re
import threading

#: What an agent may call an item.
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
#: An ended item that did not succeed makes its wake a failed one (ADR-009 D6).
GOOD = "succeeded"
ENDED = ("succeeded", "failed", "canceled", "timed_out", "unknown")


class RoundError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Rounds:
    def __init__(self, notify, describe):
        """``describe(execution_id)`` is what the woken agent is told of one
        ended item (ADR-009 D4)."""
        self._notify, self._describe = notify, describe
        self._lock = threading.RLock()
        self._sessions = {}             # parent session -> its state
        self._owner = {}                # execution id -> parent session
        self._ids = itertools.count(1)

    def _state(self, session):
        return self._sessions.setdefault(session, {
            "actor": "", "turn": 0, "in_turn": False, "items": {}, "names": {}, "conditions": []})

    # -- turns ------------------------------------------------------------------
    def turn_started(self, actor, session):
        with self._lock:
            state = self._state(session)
            state["actor"], state["turn"], state["in_turn"] = actor, state["turn"] + 1, True

    def turn_ended(self, actor):
        with self._lock:
            found = [session for session, state in self._sessions.items()
                     if state["actor"] == actor and state["in_turn"]]
            for session in found:
                self._sessions[session]["in_turn"] = False
        for session in found:
            self.evaluate(session)

    # -- items --------------------------------------------------------------------
    def add(self, session, execution_id, name="", kind="task"):
        """An item handed on in the conversation's current turn. A name in use by
        an unfinished item is refused."""
        with self._lock:
            state = self._state(session)
            if name:
                if not NAME.match(name):
                    raise RoundError("invalid_input", "A name is lowercase letters, digits, - and _, "
                                                      "at most 40.")
                held = state["names"].get(name)
                if held and held != execution_id and not state["items"][held]["ended"]:
                    raise RoundError("invalid_input", "%r is the name of a task that has not ended." % name)
                state["names"][name] = execution_id
            state["items"].setdefault(execution_id, {
                "name": name, "kind": kind, "round": state["turn"], "ended": False, "state": "",
                "seen": False, "reported": False})
            self._owner[execution_id] = session

    def resolve(self, session, names):
        """The execution ids ``names`` stand for in the conversation."""
        with self._lock:
            known = self._state(session)["names"]
            missing = [name for name in names if name not in known]
            if missing:
                raise RoundError("invalid_input", "No task or question of this conversation is called %s."
                                 % ", ".join(repr(one) for one in missing))
            return [known[name] for name in names]

    def state_of(self, execution_id):
        with self._lock:
            session = self._owner.get(execution_id)
            item = self._sessions[session]["items"].get(execution_id) if session else None
            return (item or {}).get("state") or ""

    def ended(self, execution_id, state):
        with self._lock:
            session = self._owner.get(execution_id)
            if session is None:
                return
            item = self._sessions[session]["items"][execution_id]
            if item["ended"]:
                return
            item["ended"], item["state"] = True, state
        self.evaluate(session)

    def seen(self, execution_id):
        with self._lock:
            session = self._owner.get(execution_id)
            if session is not None:
                self._sessions[session]["items"][execution_id]["seen"] = True

    # -- when to wake -------------------------------------------------------------
    def wake_when(self, session, kind, names, on_failure=False):
        """A condition of the conversation's current turn. Returns its id."""
        if kind not in ("all", "any") or not names:
            raise RoundError("invalid_input", "Say all or any, with the names of tasks or questions.")
        ids = self.resolve(session, names)
        with self._lock:
            state = self._state(session)
            condition = {"id": "w%d" % next(self._ids), "kind": kind, "ids": ids, "names": list(names),
                         "on_failure": bool(on_failure), "round": state["turn"], "fired": False}
            state["conditions"].append(condition)
        self.evaluate(session)
        return condition["id"]

    def evaluate(self, session):
        """Fire what holds - once the turn that set it has ended."""
        fired = []
        with self._lock:
            state = self._sessions.get(session)
            if state is None or state["in_turn"]:
                return
            items = state["items"]
            for condition in state["conditions"]:
                if condition["fired"]:
                    continue
                ends = [items[one]["ended"] for one in condition["ids"]]
                failed = condition["on_failure"] and any(
                    items[one]["ended"] and items[one]["state"] != GOOD for one in condition["ids"])
                if (condition["kind"] == "all" and all(ends)) or (condition["kind"] == "any" and any(ends)) or failed:
                    condition["fired"] = True
                    fired.append((dict(condition, why="failure" if failed and not all(ends) else condition["kind"]),
                                  list(condition["ids"])))
            declared = {condition["round"] for condition in state["conditions"]}
            rounds = {}
            for execution_id, item in items.items():
                if item["round"] not in declared:
                    rounds.setdefault(item["round"], []).append(execution_id)
            for number, ids in sorted(rounds.items()):
                if all(items[one]["ended"] for one in ids) and not all(items[one]["reported"] for one in ids):
                    fired.append(({"id": "round-%d" % number, "kind": "round", "why": "round", "names": []}, ids))
            told = []
            for condition, ids in fired:
                fresh = [one for one in ids if items[one]["ended"] and not items[one]["reported"]]
                for one in fresh:
                    items[one]["reported"] = True
                unseen = [one for one in fresh if not items[one]["seen"]]
                if not unseen:
                    continue            # it saw them all in its turn: nothing to wake it for
                running = [items[one]["name"] or one for one in ids if not items[one]["ended"]]
                told.append((condition, fresh, running))
            actor = state["actor"]
        for condition, fresh, running in told:
            described = [self._describe(one) for one in fresh]
            self._notify({"type": "round_finished", "session": session, "actor": actor,
                          "condition": {key: condition[key] for key in ("id", "kind", "why", "names")},
                          "items": described, "running": running,
                          "failed": any(one.get("state") != GOOD for one in described)})
