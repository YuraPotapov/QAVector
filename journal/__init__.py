"""Durable records, append-only event cursors and one terminal outcome each.

The primitives ``plugin_host.attempts.AttemptStore`` proved, made neutral so
more than one owner can keep a journal of its own without a second copy of the
rules: a QCR execution, a plugin attempt.

* **A record is claimed once.** ``claim`` inserts it under ``BEGIN IMMEDIATE``
  with a fingerprint of what it is for. Claiming the same id again with the same
  fingerprint is a no-op that says so; with another fingerprint it is refused -
  an id reused for different work is a bug, never a retry.
* **Events only grow.** Each has a unique id and a strictly increasing sequence
  within its record, and a global ``cursor`` a reader resumes from. Re-sending
  an identical event is harmless; a different one under the same id is refused.
* **The outcome is written once.** ``finish`` writes the terminal event and the
  result in one transaction. After it nothing more is appended, and finishing
  again with another result is refused.
* **Durable before it returns.** ``synchronous=FULL``; a caller that writes
  before acting (journal first, side effect second) can trust that a crash
  leaves either nothing or the whole write.

Nothing here knows what a record means: kinds, states and documents are the
owner's. Documents are JSON and stored canonically, so "identical" is exact.
Every failure is a :class:`JournalError` with a ``code``; a file that cannot be
opened or written is ``unavailable`` - the owner must then refuse to act.
"""

import json
import re
import sqlite3
import threading
from contextlib import contextmanager

NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class JournalError(Exception):
    """``code``: unavailable, invalid, unknown_record, conflict, terminal, sequence."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def canonical(value):
    """The one spelling of a JSON document, so equal documents compare equal."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise JournalError("invalid", "Document is not finite JSON data.") from None


class Journal:
    """One SQLite file of records and their events.

    ``records`` / ``events`` / ``record_column`` name the tables and the events'
    reference column, so an owner with an existing file keeps its layout.
    ``schema`` is extra SQL the owner needs beside them (its own metadata table),
    run once on open; it can then be written in the same :meth:`transaction`.
    ``limit`` caps events per record. One connection, guarded by a lock: safe
    for several threads of one process; several processes serialize on SQLite's
    own write lock.
    """

    def __init__(self, path, *, records="records", events="events", record_column="record_id",
                 limit=4097, schema=""):
        for name in (records, events, record_column):
            if not NAME.match(name):
                raise ValueError("Invalid journal table or column name.")
        self._records, self._events, self._ref = records, events, record_column
        self.limit = int(limit)
        self._lock = threading.RLock()
        self._depth = 0
        try:
            self.db = sqlite3.connect(str(path), isolation_level=None, timeout=10,
                                      check_same_thread=False)
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS {r} (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    kind TEXT NOT NULL, state TEXT NOT NULL, result TEXT);
                CREATE TABLE IF NOT EXISTS {e} (cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                    {c} TEXT NOT NULL, event_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    document TEXT NOT NULL, UNIQUE({c},event_id), UNIQUE({c},sequence));
            """.format(r=records, e=events, c=record_column) + (schema or ""))
        except (sqlite3.Error, OSError):
            raise JournalError("unavailable", "Journal cannot be opened.") from None

    def close(self):
        with self._lock:
            self.db.close()

    @contextmanager
    def transaction(self):
        """One write transaction; nested uses join the outer one.

        A ``sqlite3`` failure inside becomes ``JournalError("unavailable")``
        after the rollback: nothing of the transaction is kept.
        """
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield self.db
                finally:
                    self._depth -= 1
                return
            try:
                self.db.execute("BEGIN IMMEDIATE")
            except sqlite3.Error:
                raise JournalError("unavailable", "Journal cannot be written.") from None
            self._depth = 1
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException as exc:
                try:
                    self.db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                if isinstance(exc, sqlite3.Error):
                    raise JournalError("unavailable", "Journal cannot be written.") from None
                raise
            finally:
                self._depth = 0

    def read(self, sql, args=()):
        """Rows of one read-only query, under the same lock as writes."""
        with self._lock:
            try:
                return self.db.execute(sql, args).fetchall()
            except sqlite3.Error:
                raise JournalError("unavailable", "Journal cannot be read.") from None

    # -- records ----------------------------------------------------------------
    def claim(self, record_id, fingerprint, kind, state):
        """Insert a record. True when new, False when it already was, with this fingerprint."""
        with self.transaction() as db:
            prior = db.execute("SELECT fingerprint FROM %s WHERE id=?" % self._records,
                               (record_id,)).fetchone()
            if prior:
                if prior[0] != fingerprint:
                    raise JournalError("conflict", "Record id was reused for different work.")
                return False
            db.execute("INSERT INTO %s VALUES (?,?,?,?,NULL)" % self._records,
                       (record_id, fingerprint, kind, state))
            return True

    def record(self, record_id):
        rows = self.read("SELECT id,fingerprint,kind,state,result FROM %s WHERE id=?" % self._records,
                          (record_id,))
        if not rows:
            return None
        row = rows[0]
        return {"id": row[0], "fingerprint": row[1], "kind": row[2], "state": row[3],
                "result": json.loads(row[4]) if row[4] is not None else None}

    def set_state(self, record_id, state):
        """Change a record's state while it has no outcome."""
        with self.transaction() as db:
            row = self._row(db, record_id)
            if row[1] is not None:
                raise JournalError("terminal", "Record already has its outcome.")
            db.execute("UPDATE %s SET state=? WHERE id=?" % self._records, (state, record_id))

    def _row(self, db, record_id):
        row = db.execute("SELECT kind,result FROM %s WHERE id=?" % self._records, (record_id,)).fetchone()
        if row is None:
            raise JournalError("unknown_record", "Unknown record.")
        return row

    # -- events -----------------------------------------------------------------
    def append(self, record_id, event_id, sequence, document, *, terminal=False):
        """Add one event. True when added, False when this exact event was already there."""
        encoded = canonical(document)
        with self.transaction() as db:
            row = self._row(db, record_id)
            prior = db.execute("SELECT document FROM %s WHERE %s=? AND event_id=?" % (self._events, self._ref),
                               (record_id, event_id)).fetchone()
            if prior:
                if prior[0] != encoded:
                    raise JournalError("conflict", "Event id was reused with another document.")
                return False
            if row[1] is not None and not terminal:
                raise JournalError("terminal", "Record already has its outcome.")
            last, count = db.execute("SELECT MAX(sequence),COUNT(*) FROM %s WHERE %s=?" % (self._events, self._ref),
                                     (record_id,)).fetchone()
            if type(sequence) is not int or (last is not None and sequence <= last) or count >= self.limit:
                raise JournalError("sequence", "Event sequence or count limit violated.")
            db.execute("INSERT INTO %s(%s,event_id,sequence,document) VALUES (?,?,?,?)" % (self._events, self._ref),
                       (record_id, event_id, sequence, encoded))
            return True

    def finish(self, record_id, state, result, event_id, sequence, document):
        """Write the terminal event and the outcome together. False when this outcome was already written."""
        encoded = canonical(result)
        with self.transaction() as db:
            row = self._row(db, record_id)
            if row[1] is not None:
                if row[1] != encoded:
                    raise JournalError("conflict", "Record already has another outcome.")
                return False
            self.append(record_id, event_id, sequence, document, terminal=True)
            db.execute("UPDATE %s SET state=?,result=? WHERE id=?" % self._records, (state, encoded, record_id))
            return True

    def events(self, record_id, after=0, limit=None):
        sql = "SELECT cursor,event_id,sequence,document FROM %s WHERE %s=? AND cursor>? ORDER BY cursor" % (
            self._events, self._ref)
        args = (record_id, int(after))
        if limit is not None:
            sql += " LIMIT ?"
            args += (int(limit),)
        return [{"cursor": row[0], "event_id": row[1], "sequence": row[2], "document": json.loads(row[3])}
                for row in self.read(sql, args)]

    def last_sequence(self, record_id):
        return self.read("SELECT COALESCE(MAX(sequence),-1) FROM %s WHERE %s=?" % (self._events, self._ref),
                          (record_id,))[0][0]

    def count(self, record_id):
        return self.read("SELECT COUNT(*) FROM %s WHERE %s=?" % (self._events, self._ref), (record_id,))[0][0]
