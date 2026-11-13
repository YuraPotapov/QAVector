"""The neutral journal: claimed once, events only grow, one outcome, durable or nothing."""

import os
import sqlite3
import subprocess
import sys
import textwrap

import pytest

from journal import Journal, JournalError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def journal(tmp_path):
    one = Journal(tmp_path / "j.sqlite", limit=5)
    yield one
    one.close()


def test_a_record_is_claimed_once_and_never_for_other_work(journal):
    assert journal.claim("r1", "fp", "job", "running") is True
    assert journal.claim("r1", "fp", "job", "running") is False
    with pytest.raises(JournalError) as caught:
        journal.claim("r1", "other", "job", "running")
    assert caught.value.code == "conflict"
    assert journal.record("r1") == {"id": "r1", "fingerprint": "fp", "kind": "job",
                                    "state": "running", "result": None}
    assert journal.record("nobody") is None


def test_events_only_grow_and_a_resent_event_is_harmless(journal):
    journal.claim("r1", "fp", "job", "running")
    assert journal.append("r1", "a", 0, {"n": 1}) is True
    assert journal.append("r1", "a", 0, {"n": 1}) is False
    with pytest.raises(JournalError) as conflicting:
        journal.append("r1", "a", 1, {"n": 2})
    with pytest.raises(JournalError) as backwards:
        journal.append("r1", "b", 0, {"n": 3})
    assert (conflicting.value.code, backwards.value.code) == ("conflict", "sequence")
    journal.append("r1", "c", 7, {"n": 4})
    assert [(e["event_id"], e["sequence"]) for e in journal.events("r1")] == [("a", 0), ("c", 7)]
    first = journal.events("r1")[0]["cursor"]
    assert [e["event_id"] for e in journal.events("r1", after=first)] == ["c"]
    assert journal.last_sequence("r1") == 7 and journal.count("r1") == 2


def test_the_event_limit_holds(journal):
    journal.claim("r1", "fp", "job", "running")
    for n in range(5):
        journal.append("r1", str(n), n, {})
    with pytest.raises(JournalError) as caught:
        journal.append("r1", "5", 5, {})
    assert caught.value.code == "sequence"


def test_the_outcome_is_written_once_and_ends_the_record(journal):
    journal.claim("r1", "fp", "job", "running")
    assert journal.finish("r1", "done", {"ok": True}, "end", 1, {"type": "end"}) is True
    assert journal.finish("r1", "done", {"ok": True}, "end", 1, {"type": "end"}) is False
    with pytest.raises(JournalError) as other:
        journal.finish("r1", "failed", {"ok": False}, "end2", 2, {})
    with pytest.raises(JournalError) as after:
        journal.append("r1", "late", 3, {})
    with pytest.raises(JournalError) as state:
        journal.set_state("r1", "running")
    assert (other.value.code, after.value.code, state.value.code) == ("conflict", "terminal", "terminal")
    assert journal.record("r1")["result"] == {"ok": True} and journal.record("r1")["state"] == "done"


def test_a_failed_transaction_keeps_nothing(journal):
    with pytest.raises(RuntimeError):
        with journal.transaction():
            journal.claim("r1", "fp", "job", "running")
            journal.append("r1", "a", 0, {})
            raise RuntimeError("the owner changed its mind")
    assert journal.record("r1") is None


def test_documents_must_be_json(journal):
    journal.claim("r1", "fp", "job", "running")
    with pytest.raises(JournalError) as caught:
        journal.append("r1", "a", 0, {"x": float("nan")})
    assert caught.value.code == "invalid"


def test_an_unopenable_file_is_unavailable(tmp_path):
    with pytest.raises(JournalError) as caught:
        Journal(tmp_path / "no" / "such" / "dir" / "j.sqlite")
    assert caught.value.code == "unavailable"


def test_names_are_checked():
    with pytest.raises(ValueError):
        Journal(":memory:", records="x; DROP TABLE y")


#: The layout plugin_host.attempts.AttemptStore wrote before it moved onto the
#: journal (QCR 2.6), verbatim - files written by earlier builds look like this.
LEGACY_ATTEMPTS = """
    CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
        kind TEXT NOT NULL, state TEXT NOT NULL, result TEXT);
    CREATE TABLE IF NOT EXISTS events (cursor INTEGER PRIMARY KEY AUTOINCREMENT,
        attempt_id TEXT NOT NULL, event_id TEXT NOT NULL, sequence INTEGER NOT NULL,
        document TEXT NOT NULL, UNIQUE(attempt_id,event_id), UNIQUE(attempt_id,sequence));
"""


def legacy_file(path, rows=(), events=()):
    db = sqlite3.connect(str(path))
    db.executescript(LEGACY_ATTEMPTS)
    db.executemany("INSERT INTO attempts VALUES (?,?,?,?,?)", rows)
    db.executemany("INSERT INTO events(attempt_id,event_id,sequence,document) VALUES (?,?,?,?)", events)
    db.commit()
    db.close()


def test_an_attempts_file_of_an_earlier_build_opens_as_a_journal(tmp_path):
    path = tmp_path / "attempts.sqlite"
    legacy_file(path, rows=[("a0", "fp0", "node", "running", None)],
                events=[("a0", "e0", 0, '{"n":1}')])
    one = Journal(path, records="attempts", events="events", record_column="attempt_id")
    assert one.record("a0")["state"] == "running"
    assert one.append("a0", "e0", 0, {"n": 1}) is False, "the same canonical spelling"
    one.claim("a1", "fp", "node", "running")
    one.close()
    db = sqlite3.connect(str(path))
    assert db.execute("SELECT id,kind,state FROM attempts ORDER BY id").fetchall() == [
        ("a0", "node", "running"), ("a1", "node", "running")]
    db.close()


def _crash(tmp_path, body):
    script = textwrap.dedent("""
        import os, sys
        sys.path.insert(0, %r)
        from journal import Journal
        j = Journal(%r)
    """ % (ROOT, str(tmp_path / "j.sqlite"))) + textwrap.dedent(body) + "\nos._exit(0)\n"
    subprocess.run([sys.executable, "-c", script], check=True, timeout=60)
    return Journal(tmp_path / "j.sqlite")


def test_a_committed_write_survives_the_process_dying(tmp_path):
    reopened = _crash(tmp_path, """
        j.claim("r1", "fp", "job", "running")
        j.append("r1", "a", 0, {"n": 1})
    """)
    assert reopened.record("r1")["state"] == "running" and len(reopened.events("r1")) == 1
    reopened.close()


def test_a_process_dying_mid_transaction_leaves_nothing_of_it(tmp_path):
    reopened = _crash(tmp_path, """
        j.claim("r0", "fp", "job", "running")
        with j.transaction():
            j.claim("r1", "fp", "job", "running")
            j.append("r1", "a", 0, {"n": 1})
            os._exit(0)
    """)
    assert reopened.record("r0") is not None
    assert reopened.record("r1") is None and reopened.events("r1") == []
    reopened.close()
