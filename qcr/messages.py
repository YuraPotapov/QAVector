"""Messages between agents, durably (ADR-006 D4, Phase 5.3).

One SQLite file per project (``<project>/.qcr/messages.sqlite``) on the
:mod:`journal` primitives: a message is a journal record, each state change an
event, its end the journal's terminal result. Beside them ``message_meta``
holds what a listing needs.

**States**::

    queued -> awaiting_approval -> delivered -> acknowledged
       \\------------------------/      \\-> expired
    queued | awaiting_approval -> denied | failed

``delivered`` means it is where its recipient will see it (a connection's
inbox, or the conversation a reply goes back to) - not that anybody answered.
``acknowledged`` means it was read: answered by the recipient's turn, or
passed into a chat by the person. A delivered message nobody acknowledged in
:data:`EXPIRE_SECONDS` becomes ``expired`` the next time messages are listed.

**Exactly once per id.** A message's id is the id of the execution that sent
it; recording the same id with the same content again is a no-op, with other
content a ``conflict``.
"""

import hashlib
import json
import os
import time

from journal import Journal, JournalError, canonical

from .errors import QCRError

STATES = ("queued", "awaiting_approval", "delivered", "acknowledged", "denied", "failed", "expired")
TERMINAL = frozenset(("acknowledged", "denied", "failed", "expired"))
TRANSITIONS = {
    "queued": frozenset(("awaiting_approval", "delivered", "denied", "failed")),
    "awaiting_approval": frozenset(("delivered", "denied", "failed")),
    "delivered": frozenset(("acknowledged", "expired", "failed")),
}
EXPIRE_SECONDS = 7 * 24 * 3600
BODY_CHARS = 20000

META = """
CREATE TABLE IF NOT EXISTS message_meta (
    message_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
    kind TEXT NOT NULL, sender_actor TEXT NOT NULL, sender_session TEXT NOT NULL,
    sender_connection TEXT NOT NULL, recipient_connection TEXT NOT NULL, recipient_session TEXT,
    expects_reply INTEGER NOT NULL, reply_to TEXT, body TEXT NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS message_recipient ON message_meta(project_id, recipient_connection, created_at);
CREATE INDEX IF NOT EXISTS message_sender ON message_meta(project_id, sender_session, created_at);
CREATE INDEX IF NOT EXISTS message_conversation ON message_meta(project_id, conversation_id, created_at);
"""
_COLUMNS = ("message_id", "project_id", "conversation_id", "kind", "sender_actor", "sender_session",
            "sender_connection", "recipient_connection", "recipient_session", "expects_reply", "reply_to",
            "body", "created_at", "updated_at")


class MessageError(QCRError):
    pass


class MessageStore:
    def __init__(self, path, *, clock=time.time):
        self._clock = clock
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
        except OSError:
            raise MessageError("journal_unavailable", "Message journal is unavailable.") from None
        self._journal = self._call(lambda: Journal(path, records="messages", events="message_events",
                                                   record_column="message_id", limit=100, schema=META))

    def close(self):
        self._journal.close()

    @staticmethod
    def _call(work):
        try:
            return work()
        except JournalError as exc:
            if exc.code == "unavailable":
                raise MessageError("journal_unavailable", "Message journal is unavailable.") from None
            code = {"conflict": "conflict", "unknown_record": "not_found", "terminal": "terminal"}.get(
                exc.code, "invalid_transition")
            raise MessageError(code, "Message journal refused the change.") from None

    # -- writing -----------------------------------------------------------------
    def create(self, message_id, project_id, *, kind, sender_actor, sender_session, sender_connection,
               recipient_connection, body, recipient_session=None, expects_reply=False, reply_to=None,
               conversation_id=None):
        """Record a message as ``queued``. Returns ``(message, created)``."""
        if kind not in ("message", "reply") or not isinstance(body, str) or len(body) > BODY_CHARS:
            raise MessageError("invalid_input", "A message is a body of at most %d characters." % BODY_CHARS)
        fields = {"project_id": project_id, "conversation_id": conversation_id or message_id, "kind": kind,
                  "sender_actor": sender_actor, "sender_session": sender_session,
                  "sender_connection": sender_connection, "recipient_connection": recipient_connection,
                  "recipient_session": recipient_session, "expects_reply": bool(expects_reply),
                  "reply_to": reply_to, "body": body}
        fingerprint = hashlib.sha256(canonical(fields).encode("utf-8")).hexdigest()

        def work():
            with self._journal.transaction() as db:
                if not self._journal.claim(message_id, fingerprint, "message", "queued"):
                    return False
                now = self._clock()
                db.execute("INSERT INTO message_meta VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (message_id, project_id, fields["conversation_id"], kind, sender_actor,
                            sender_session, sender_connection, recipient_connection, recipient_session,
                            int(bool(expects_reply)), reply_to, body, now, now))
                self._journal.append(message_id, "0", 0, {"type": "state", "state": "queued", "at": now})
                return True

        created = self._call(work)
        return self.get(message_id), created

    def transition(self, message_id, state, payload=None):
        if state not in STATES:
            raise MessageError("invalid_input", "Unknown message state.")

        def work():
            with self._journal.transaction() as db:
                record = self._journal.record(message_id)
                if record is None:
                    raise MessageError("not_found", "Message is not available.")
                current = record["state"]
                if current in TERMINAL:
                    if current == state:
                        return
                    raise MessageError("terminal", "Message already has its outcome.")
                if state == current:
                    return
                if state not in TRANSITIONS[current]:
                    raise MessageError("invalid_transition", "Message cannot move to that state.")
                now = self._clock()
                sequence = self._journal.last_sequence(message_id) + 1
                event = {"type": "state", "state": state, "from": current, "at": now}
                if payload is not None:
                    event["payload"] = payload
                if state in TERMINAL:
                    self._journal.finish(message_id, state, {"state": state}, "terminal", sequence, event)
                else:
                    self._journal.append(message_id, str(sequence), sequence, event)
                    self._journal.set_state(message_id, state)
                db.execute("UPDATE message_meta SET updated_at=? WHERE message_id=?", (now, message_id))

        self._call(work)
        return self.get(message_id)

    # -- reading -----------------------------------------------------------------
    def get(self, message_id):
        rows = self._call(lambda: self._journal.read(
            "SELECT %s FROM message_meta WHERE message_id=?" % ",".join(_COLUMNS), (message_id,)))
        if not rows:
            raise MessageError("not_found", "Message is not available.")
        return self._row(rows[0])

    def _row(self, row):
        message = dict(zip(_COLUMNS, row))
        message["expects_reply"] = bool(message["expects_reply"])
        message["state"] = self._call(lambda: self._journal.record(message["message_id"]))["state"]
        return message

    def events(self, message_id):
        self.get(message_id)
        return [item["document"] for item in self._call(lambda: self._journal.events(message_id))]

    def list(self, project_id, *, recipient_connection=None, sender_session=None, recipient_session=None,
             conversation_id=None, state=None, limit=100):
        """Newest first; delivered messages past their time become expired first."""
        self.expire(project_id)
        where, args = ["m.project_id=?"], [project_id]
        for column, value in (("m.recipient_connection", recipient_connection),
                              ("m.sender_session", sender_session),
                              ("m.recipient_session", recipient_session),
                              ("m.conversation_id", conversation_id), ("r.state", state)):
            if value is not None:
                where.append("%s=?" % column)
                args.append(value)
        sql = ("SELECT %s FROM message_meta m JOIN messages r ON r.id=m.message_id WHERE %s "
               "ORDER BY m.created_at DESC LIMIT ?") % (",".join("m." + c for c in _COLUMNS), " AND ".join(where))
        rows = self._call(lambda: self._journal.read(sql, tuple(args) + (max(1, min(int(limit), 1000)),)))
        return [self._row(row) for row in rows]

    def sent_since(self, project_id, sender_session, since):
        rows = self._call(lambda: self._journal.read(
            "SELECT COUNT(*) FROM message_meta WHERE project_id=? AND sender_session=? AND created_at>=?",
            (project_id, sender_session, since)))
        return rows[0][0]

    def expire(self, project_id):
        cutoff = self._clock() - EXPIRE_SECONDS
        rows = self._call(lambda: self._journal.read(
            "SELECT m.message_id FROM message_meta m JOIN messages r ON r.id=m.message_id "
            "WHERE m.project_id=? AND r.state='delivered' AND m.updated_at<?", (project_id, cutoff)))
        for (message_id,) in rows:
            try:
                self.transition(message_id, "expired")
            except MessageError:
                pass


def summary(message, body_chars=2000):
    """A message as a listing shows it: everything, the body cut to ``body_chars``."""
    shown = dict(message)
    if len(shown["body"]) > body_chars:
        shown["body"], shown["truncated"] = shown["body"][:body_chars], True
    return json.loads(json.dumps(shown))
