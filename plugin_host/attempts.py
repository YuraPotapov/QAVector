"""Durable attempt ownership, event cursors and terminal outcomes.

The durable primitives are :mod:`journal`'s (QCR ADR-002 2.6): claimed once with
a fingerprint, events that only grow with unique ids and sequences, one terminal
outcome written with its event. What is a plugin's own stays here - the worker
contracts each event and result is validated against, terminal event types that
only ``finish`` may write, and the protocol errors callers act on.

The file layout is the one this store always wrote (``attempts`` / ``events``
with ``attempt_id``), and documents are stored in the same canonical spelling,
so an existing attempts file opens unchanged. A file that cannot be opened or
written raises :class:`journal.JournalError` (``unavailable``) where it used to
raise ``sqlite3`` errors: neither was ever a protocol answer, and callers treat
both as a failure of the run.
"""

import hashlib

from journal import Journal, JournalError, canonical as _canonical
from qavector_sdk import ContractError, validate_contract

#: Event types only :meth:`AttemptStore.finish` may write.
TERMINAL_TYPES = ("completed", "failed", "cancelled")
#: Events kept per attempt, as before.
LIMIT = 4097


def canonical(value):
    return _canonical(value)


def _protocol(message):
    return ContractError("WORKER_PROTOCOL_ERROR", message)


class AttemptStore:
    def __init__(self, path):
        self._journal = Journal(str(path), records="attempts", events="events",
                                record_column="attempt_id", limit=LIMIT)
        #: The connection, for callers that inspected it before the journal existed.
        self.db = self._journal.db

    def close(self):
        self._journal.close()

    def claim(self, initialization, params, *, runtime_binding=None):
        attempt = params["request"]["attempt_id"]
        kind = "agent" if initialization["identity"]["contribution"]["kind"] == "agent_backend" else "node"
        fingerprint = hashlib.sha256(canonical([initialization, params, runtime_binding]).encode()).hexdigest()
        try:
            return self._journal.claim(attempt, fingerprint, kind, "running")
        except JournalError as exc:
            if exc.code == "conflict":
                raise _protocol("Attempt ID was reused with different execution data") from None
            raise

    def snapshot(self, attempt):
        record = self._journal.record(attempt)
        if record is None:
            raise _protocol("Unknown attempt")
        return {"attempt_id": attempt, "status": record["state"], "result": record["result"]}

    def _append(self, event, terminal=False):
        record = self._journal.record(event["attempt_id"])
        if record is None:
            raise _protocol("Unknown event attempt")
        validate_contract(record["kind"] + "-event", event)
        prior = self._prior(event)
        if prior is not None:
            if prior != canonical(event):
                raise _protocol("Conflicting duplicate event ID")
            return False
        if record["result"] is not None or (not terminal and event["type"] in TERMINAL_TYPES):
            raise _protocol("Event cannot change terminal execution")
        try:
            return self._journal.append(event["attempt_id"], event["event_id"], event["sequence"], event,
                                        terminal=terminal)
        except JournalError as exc:
            if exc.code == "sequence":
                raise _protocol("Event sequence or count limit violated") from None
            if exc.code == "conflict":
                raise _protocol("Conflicting duplicate event ID") from None
            if exc.code == "terminal":
                raise _protocol("Event cannot change terminal execution") from None
            raise

    def _prior(self, event):
        rows = self._journal.read("SELECT document FROM events WHERE attempt_id=? AND event_id=?",
                                  (event["attempt_id"], event["event_id"]))
        return rows[0][0] if rows else None

    def append(self, event):
        with self._journal.transaction():
            return self._append(event)

    def finish(self, result, event):
        with self._journal.transaction():
            record = self._journal.record(result["attempt_id"])
            if record is None:
                raise _protocol("Unknown terminal attempt")
            validate_contract(record["kind"] + "-result", result)
            if event["attempt_id"] != result["attempt_id"] or event["payload"].get("result") != result:
                raise _protocol("Terminal event does not match outcome")
            if record["result"] is not None:
                if canonical(record["result"]) != canonical(result):
                    raise _protocol("Conflicting terminal outcome")
                return
            self._append(event, terminal=True)
            self._journal.set_state(result["attempt_id"], result["status"])
            self._journal.db.execute("UPDATE attempts SET result=? WHERE id=?",
                                     (canonical(result), result["attempt_id"]))

    def events(self, attempt, after=0):
        return [{"cursor": item["cursor"], "event": item["document"]}
                for item in self._journal.events(attempt, after)]

    def last_sequence(self, attempt):
        return self._journal.last_sequence(attempt)

    def check_terminal(self, event):
        if event["sequence"] <= self.last_sequence(event["attempt_id"]):
            raise _protocol("Terminal event is out of order")
        if self._prior(event) is not None:
            raise _protocol("Terminal event reuses a prior event ID")
