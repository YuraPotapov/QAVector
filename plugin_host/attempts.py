"""Durable attempt ownership, event cursors and terminal outcomes."""

import hashlib
import json
import sqlite3

from qavector_sdk import ContractError, validate_contract


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class AttemptStore:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), isolation_level=None, timeout=10)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                kind TEXT NOT NULL, state TEXT NOT NULL, result TEXT);
            CREATE TABLE IF NOT EXISTS events (cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id TEXT NOT NULL, event_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                document TEXT NOT NULL, UNIQUE(attempt_id,event_id), UNIQUE(attempt_id,sequence));
        """)

    def close(self):
        self.db.close()

    def claim(self, initialization, params, *, runtime_binding=None):
        attempt = params["request"]["attempt_id"]
        kind = "agent" if initialization["identity"]["contribution"]["kind"] == "agent_backend" else "node"
        fingerprint = hashlib.sha256(canonical([initialization, params, runtime_binding]).encode()).hexdigest()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            prior = self.db.execute("SELECT fingerprint FROM attempts WHERE id=?", (attempt,)).fetchone()
            if prior:
                if prior[0] != fingerprint:
                    raise ContractError("WORKER_PROTOCOL_ERROR", "Attempt ID was reused with different execution data")
                claimed = False
            else:
                self.db.execute("INSERT INTO attempts VALUES (?,?,?,'running',NULL)", (attempt, fingerprint, kind))
                claimed = True
            self.db.execute("COMMIT")
            return claimed
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def snapshot(self, attempt):
        row = self.db.execute("SELECT state,result FROM attempts WHERE id=?", (attempt,)).fetchone()
        if row is None:
            raise ContractError("WORKER_PROTOCOL_ERROR", "Unknown attempt")
        return {"attempt_id": attempt, "status": row[0], "result": json.loads(row[1]) if row[1] else None}

    def _append(self, event, terminal=False):
        row = self.db.execute("SELECT kind,result FROM attempts WHERE id=?", (event["attempt_id"],)).fetchone()
        if row is None:
            raise ContractError("WORKER_PROTOCOL_ERROR", "Unknown event attempt")
        validate_contract(row[0] + "-event", event)
        encoded = canonical(event)
        prior = self.db.execute("SELECT document FROM events WHERE attempt_id=? AND event_id=?",
                                (event["attempt_id"], event["event_id"])).fetchone()
        if prior:
            if prior[0] != encoded:
                raise ContractError("WORKER_PROTOCOL_ERROR", "Conflicting duplicate event ID")
            return False
        if row[1] is not None or (not terminal and event["type"] in ("completed", "failed", "cancelled")):
            raise ContractError("WORKER_PROTOCOL_ERROR", "Event cannot change terminal execution")
        last, count = self.db.execute("SELECT MAX(sequence),COUNT(*) FROM events WHERE attempt_id=?",
                                      (event["attempt_id"],)).fetchone()
        if (last is not None and event["sequence"] <= last) or count >= 4097:
            raise ContractError("WORKER_PROTOCOL_ERROR", "Event sequence or count limit violated")
        self.db.execute("INSERT INTO events(attempt_id,event_id,sequence,document) VALUES (?,?,?,?)",
                        (event["attempt_id"], event["event_id"], event["sequence"], encoded))
        return True

    def append(self, event):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            added = self._append(event)
            self.db.execute("COMMIT")
            return added
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def finish(self, result, event):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT kind,result FROM attempts WHERE id=?", (result["attempt_id"],)).fetchone()
            if row is None:
                raise ContractError("WORKER_PROTOCOL_ERROR", "Unknown terminal attempt")
            validate_contract(row[0] + "-result", result)
            if event["attempt_id"] != result["attempt_id"] or event["payload"].get("result") != result:
                raise ContractError("WORKER_PROTOCOL_ERROR", "Terminal event does not match outcome")
            encoded = canonical(result)
            if row[1] is not None:
                if row[1] != encoded:
                    raise ContractError("WORKER_PROTOCOL_ERROR", "Conflicting terminal outcome")
            else:
                self._append(event, terminal=True)
                self.db.execute("UPDATE attempts SET state=?,result=? WHERE id=?",
                                (result["status"], encoded, result["attempt_id"]))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def events(self, attempt, after=0):
        return [{"cursor": row[0], "event": json.loads(row[1])} for row in self.db.execute(
            "SELECT cursor,document FROM events WHERE attempt_id=? AND cursor>? ORDER BY cursor", (attempt, after))]

    def last_sequence(self, attempt):
        return self.db.execute("SELECT COALESCE(MAX(sequence),-1) FROM events WHERE attempt_id=?", (attempt,)).fetchone()[0]

    def check_terminal(self, event):
        if event["sequence"] <= self.last_sequence(event["attempt_id"]):
            raise ContractError("WORKER_PROTOCOL_ERROR", "Terminal event is out of order")
        if self.db.execute("SELECT 1 FROM events WHERE attempt_id=? AND event_id=?",
                           (event["attempt_id"], event["event_id"])).fetchone():
            raise ContractError("WORKER_PROTOCOL_ERROR", "Terminal event reuses a prior event ID")
