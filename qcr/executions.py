"""Execution records: what was asked, by whom, how it went - durably (ADR-002 D3/D4).

One SQLite file per project (the host passes ``<project>/.qcr/executions.sqlite``)
on top of :mod:`journal`: an execution is a journal record, its state changes
and progress are journal events, its outcome is the journal's terminal result.
Beside them, ``execution_meta`` holds what a list or a filter needs to read
without opening every event.

**Written before acting.** :meth:`ExecutionStore.create` commits the record
before the caller dispatches anything; when it cannot (``journal_unavailable``)
the caller must not act at all. A crash between the two leaves a ``queued``
record that :meth:`reconcile` turns into ``unknown`` - never into a retry.

**States** (D4)::

    queued -> dispatched -> running -> succeeded | failed | timed_out | canceled
    any non-terminal state -> unknown   (the host restarted without an outcome)

``dispatched`` means the owner took the job (ACK), which is not success. A
terminal state is final: repeating the same outcome is a no-op, another one is
refused.

**Idempotency** (D6). With an ``idempotency_key``, the same key and the same
request (project, capability, version, actor, resource, input, parent) returns
the execution already made; the same key for anything else is ``conflict``.
``exclusive=True`` makes creation atomic with "no other unfinished execution on
this resource" - else ``busy``, naming the one in the way.

Every failure is a value-free :class:`ExecutionError` with a code.
"""

import hashlib
import json
import os
import time
import uuid

from journal import Journal, JournalError, canonical

from .errors import QCRError

STATES = ("queued", "dispatched", "running", "succeeded", "failed", "timed_out", "canceled", "unknown")
TERMINAL = frozenset(("succeeded", "failed", "timed_out", "canceled", "unknown"))
TRANSITIONS = {
    "queued": frozenset(("dispatched", "failed", "canceled", "unknown")),
    "dispatched": frozenset(("running", "succeeded", "failed", "timed_out", "canceled", "unknown")),
    "running": frozenset(("succeeded", "failed", "timed_out", "canceled", "unknown")),
}

#: Events kept per execution. Progress beyond ``EVENT_LIMIT - 2`` is counted as
#: dropped rather than stored, so the terminal event always has room.
EVENT_LIMIT = 1000
#: Largest input, output or event payload kept, canonical JSON bytes. Logs and
#: other bulk travel as references (artifacts), not inside the journal.
DOCUMENT_BYTES = 64 * 1024

META = """
CREATE TABLE IF NOT EXISTS execution_meta (
    execution_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, capability_id TEXT NOT NULL,
    version INTEGER NOT NULL, actor_id TEXT NOT NULL, resource_id TEXT, idempotency_key TEXT,
    parent_id TEXT, correlation_id TEXT, input TEXT NOT NULL, created_at REAL NOT NULL,
    updated_at REAL NOT NULL, finished_at REAL, dropped_events INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS execution_idempotency
    ON execution_meta(project_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS execution_resource ON execution_meta(project_id, resource_id, finished_at);
CREATE INDEX IF NOT EXISTS execution_created ON execution_meta(project_id, created_at);
"""

_COLUMNS = ("execution_id", "project_id", "capability_id", "version", "actor_id", "resource_id",
            "idempotency_key", "parent_id", "correlation_id", "input", "created_at", "updated_at",
            "finished_at", "dropped_events")


class ExecutionError(QCRError):
    """``code``: not_found, conflict, busy, terminal, invalid_transition, invalid_input,
    too_large, journal_unavailable. ``execution_id`` names the execution in the way
    for ``busy``."""

    def __init__(self, code, message, execution_id=None):
        super().__init__(code, message)
        self.execution_id = execution_id


def _text(value, what, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value:
        raise ExecutionError("invalid_input", "Invalid %s." % what)
    return value


def _document(value, what):
    try:
        encoded = canonical(value)
    except JournalError:
        raise ExecutionError("invalid_input", "%s is not JSON data." % what) from None
    if len(encoded.encode("utf-8")) > DOCUMENT_BYTES:
        raise ExecutionError("too_large", "%s is larger than the journal keeps." % what)
    return encoded


def _error(error):
    if error is None:
        return None
    if not isinstance(error, dict) or set(error) - {"code", "message"} or "code" not in error:
        raise ExecutionError("invalid_input", "An error is {code, message}.")
    return {"code": _text(error["code"], "error code"),
            "message": _text(error.get("message") or error["code"], "error message")}


class ExecutionStore:
    def __init__(self, path, *, clock=time.time, new_id=lambda: uuid.uuid4().hex):
        self._clock, self._new_id = clock, new_id
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        except OSError:
            raise ExecutionError("journal_unavailable", "Execution journal is unavailable.") from None
        self._journal = self._call(lambda: Journal(path, records="executions", events="execution_events",
                                                   record_column="execution_id", limit=EVENT_LIMIT, schema=META))

    def close(self):
        self._journal.close()

    @staticmethod
    def _call(work):
        try:
            return work()
        except JournalError as exc:
            if exc.code == "unavailable":
                raise ExecutionError("journal_unavailable", "Execution journal is unavailable.") from None
            code = {"conflict": "conflict", "unknown_record": "not_found", "terminal": "terminal"}.get(
                exc.code, "invalid_transition")
            raise ExecutionError(code, "Execution journal refused the change.") from None

    # -- writing ----------------------------------------------------------------
    def create(self, project_id, capability_id, version, actor_id, request, *, resource_id=None,
               idempotency_key=None, parent_id=None, correlation_id=None, exclusive=False):
        """Record a new execution as ``queued``. Returns ``(execution, created)``."""
        project_id, capability_id, actor_id = (_text(project_id, "project id"),
                                               _text(capability_id, "capability id"), _text(actor_id, "actor id"))
        resource_id, idempotency_key, parent_id, correlation_id = (
            _text(resource_id, "resource id", True), _text(idempotency_key, "idempotency key", True),
            _text(parent_id, "parent id", True), _text(correlation_id, "correlation id", True))
        if type(version) is not int or version < 1:
            raise ExecutionError("invalid_input", "Invalid capability version.")
        if exclusive and resource_id is None:
            raise ExecutionError("invalid_input", "An exclusive execution names its resource.")
        encoded = _document(request, "Input")
        fingerprint = hashlib.sha256(canonical(
            [project_id, capability_id, version, actor_id, resource_id, parent_id, json.loads(encoded)]
        ).encode("utf-8")).hexdigest()

        def work():
            with self._journal.transaction() as db:
                if idempotency_key is not None:
                    row = db.execute("SELECT execution_id FROM execution_meta WHERE project_id=? AND idempotency_key=?",
                                     (project_id, idempotency_key)).fetchone()
                    if row:
                        if self._journal.record(row[0])["fingerprint"] != fingerprint:
                            raise ExecutionError("conflict", "Idempotency key was used for another request.")
                        return row[0], False
                if exclusive:
                    row = db.execute("SELECT execution_id FROM execution_meta WHERE project_id=? AND resource_id=? "
                                     "AND finished_at IS NULL ORDER BY created_at LIMIT 1",
                                     (project_id, resource_id)).fetchone()
                    if row:
                        raise ExecutionError("busy", "Another execution is still working on this resource.", row[0])
                execution_id = _text(self._new_id(), "execution id")
                now = self._clock()
                if not self._journal.claim(execution_id, fingerprint, "execution", "queued"):
                    raise ExecutionError("conflict", "Execution id is already taken.")
                db.execute("INSERT INTO execution_meta VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,0)",
                           (execution_id, project_id, capability_id, version, actor_id, resource_id,
                            idempotency_key, parent_id, correlation_id, encoded, now, now))
                self._journal.append(execution_id, "0", 0, {"type": "state", "state": "queued", "at": now})
                return execution_id, True

        execution_id, created = self._call(work)
        return self.get(execution_id), created

    def transition(self, execution_id, state, *, payload=None, output=None, error=None):
        """Move an execution to ``state``; a terminal state also records output/error. Returns it."""
        if state not in STATES:
            raise ExecutionError("invalid_input", "Unknown execution state.")
        payload_doc = None if payload is None else json.loads(_document(payload, "Payload"))
        output_doc = None if output is None else json.loads(_document(output, "Output"))
        error_doc = _error(error)

        def work():
            with self._journal.transaction() as db:
                record = self._journal.record(execution_id)
                if record is None:
                    raise ExecutionError("not_found", "Execution is not available.")
                current = record["state"]
                if current in TERMINAL:
                    if state == current and record["result"] == self._result(state, output_doc, error_doc):
                        return
                    raise ExecutionError("terminal", "Execution already has its outcome.")
                if state not in TRANSITIONS[current]:
                    raise ExecutionError("invalid_transition", "Execution cannot move to that state.")
                now = self._clock()
                sequence = self._journal.last_sequence(execution_id) + 1
                event = {"type": "state", "state": state, "from": current, "at": now}
                if payload_doc is not None:
                    event["payload"] = payload_doc
                if state in TERMINAL:
                    self._journal.finish(execution_id, state, self._result(state, output_doc, error_doc),
                                         "terminal", sequence, event)
                    db.execute("UPDATE execution_meta SET updated_at=?, finished_at=? WHERE execution_id=?",
                               (now, now, execution_id))
                else:
                    self._journal.append(execution_id, str(sequence), sequence, event)
                    self._journal.set_state(execution_id, state)
                    db.execute("UPDATE execution_meta SET updated_at=? WHERE execution_id=?", (now, execution_id))

        self._call(work)
        return self.get(execution_id)

    @staticmethod
    def _result(state, output, error):
        return {"state": state, "output": output, "error": error}

    def progress(self, execution_id, kind, payload=None):
        """Add a progress event to an unfinished execution. False when it was dropped
        for the event limit (and counted), so the outcome always has room."""
        kind = _text(kind, "event type")
        payload_doc = None if payload is None else json.loads(_document(payload, "Payload"))

        def work():
            with self._journal.transaction() as db:
                record = self._journal.record(execution_id)
                if record is None:
                    raise ExecutionError("not_found", "Execution is not available.")
                if record["state"] in TERMINAL:
                    raise ExecutionError("terminal", "Execution already has its outcome.")
                if self._journal.count(execution_id) >= EVENT_LIMIT - 2:
                    db.execute("UPDATE execution_meta SET dropped_events=dropped_events+1 WHERE execution_id=?",
                               (execution_id,))
                    return False
                sequence = self._journal.last_sequence(execution_id) + 1
                event = {"type": kind, "at": self._clock()}
                if payload_doc is not None:
                    event["payload"] = payload_doc
                return self._journal.append(execution_id, str(sequence), sequence, event)

        return self._call(work)

    def reconcile(self, observe=None):
        """After a restart: every unfinished execution becomes ``unknown``. Nothing is retried.

        ``observe(execution)`` may add what is true now (a service's status) to
        the record; its failure only leaves that observation out.
        """
        ids = [row[0] for row in self._call(lambda: self._journal.read(
            "SELECT execution_id FROM execution_meta WHERE finished_at IS NULL ORDER BY created_at"))]
        for execution_id in ids:
            observed = None
            if observe is not None:
                try:
                    observed = observe(self.get(execution_id))
                    _document(observed, "Observation")
                except Exception:
                    observed = None
            payload = {"reason": "host_restarted"}
            if observed is not None:
                payload["observed"] = observed
            self.transition(execution_id, "unknown", payload=payload,
                            error={"code": "interrupted",
                                   "message": "The host stopped before the execution finished."})
        return ids

    def prune(self, keep=5000):
        """Forget the oldest finished executions beyond ``keep`` per file. Unfinished ones stay."""
        def work():
            with self._journal.transaction() as db:
                ids = [row[0] for row in db.execute(
                    "SELECT execution_id FROM execution_meta WHERE finished_at IS NOT NULL "
                    "ORDER BY finished_at DESC LIMIT -1 OFFSET ?", (int(keep),))]
                for execution_id in ids:
                    db.execute("DELETE FROM execution_events WHERE execution_id=?", (execution_id,))
                    db.execute("DELETE FROM executions WHERE id=?", (execution_id,))
                    db.execute("DELETE FROM execution_meta WHERE execution_id=?", (execution_id,))
                return len(ids)
        return self._call(work)

    # -- reading ----------------------------------------------------------------
    def get(self, execution_id):
        rows = self._call(lambda: self._journal.read(
            "SELECT %s FROM execution_meta WHERE execution_id=?" % ",".join(_COLUMNS), (execution_id,)))
        if not rows:
            raise ExecutionError("not_found", "Execution is not available.")
        return self._row(rows[0])

    def _row(self, row):
        meta = dict(zip(_COLUMNS, row))
        record = self._call(lambda: self._journal.record(meta["execution_id"]))
        result = record["result"] or {}
        meta["input"] = json.loads(meta["input"])
        meta.update(state=record["state"], output=result.get("output"), error=result.get("error"))
        return meta

    def events(self, execution_id, after=0, limit=200):
        self.get(execution_id)
        return [{"cursor": item["cursor"], "sequence": item["sequence"], **item["document"]}
                for item in self._call(lambda: self._journal.events(execution_id, after, limit))]

    def list(self, project_id, *, state=None, capability_id=None, actor_id=None, resource_id=None,
             limit=100, before=None):
        """Newest first. ``before`` is a ``created_at`` to page back from."""
        where, args = ["m.project_id=?"], [_text(project_id, "project id")]
        for column, value in (("m.capability_id", capability_id), ("m.actor_id", actor_id),
                              ("m.resource_id", resource_id), ("e.state", state)):
            if value is not None:
                where.append("%s=?" % column)
                args.append(value)
        if before is not None:
            where.append("m.created_at<?")
            args.append(float(before))
        sql = ("SELECT %s FROM execution_meta m JOIN executions e ON e.id=m.execution_id WHERE %s "
               "ORDER BY m.created_at DESC LIMIT ?") % (",".join("m." + c for c in _COLUMNS), " AND ".join(where))
        rows = self._call(lambda: self._journal.read(sql, tuple(args) + (max(1, min(int(limit), 1000)),)))
        return [self._row(row) for row in rows]

    def active(self, project_id, resource_id):
        """The unfinished execution on a resource, or None."""
        rows = self._call(lambda: self._journal.read(
            "SELECT execution_id FROM execution_meta WHERE project_id=? AND resource_id=? AND finished_at IS NULL "
            "ORDER BY created_at LIMIT 1", (project_id, resource_id)))
        return rows[0][0] if rows else None
