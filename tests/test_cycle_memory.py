"""What the project remembers between runs.

Two properties carry this file. The first is that a claim is exclusive - it is
the only thing here that would be actively dangerous if it were merely usually
right, because two runs both believing they hold a task is exactly what it
exists to prevent. The second is that concurrent writers do not lose each
other's work, which the secrets store next door does not manage and which is
why this one takes a lock.

Everything writes to a tmp_path. One test asserts the default path is under the
user's data and not in a checkout, which is the same guard the secrets tests
keep for the same reason.
"""

import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cycle import memory                                          # noqa: E402


@pytest.fixture
def store(tmp_path):
    """A store of our own, so nothing here can reach the user's."""
    return str(tmp_path / "memory.json")


# ------------------------------------------------------------------ remembering
def test_nothing_is_known_before_anything_is_written(store):
    assert memory.recall("QA-1", store) == {}
    assert memory.known("QA-1", store) is False
    assert memory.keys(path=store) == []


def test_what_is_remembered_comes_back(store):
    memory.remember("QA-1", {"status": "done", "commit": "abc"}, store)
    assert memory.recall("QA-1", store) == {"status": "done", "commit": "abc"}


def test_remembering_merges_rather_than_replacing(store):
    """Two steps of one cycle remember different things about the same task,
    and neither should erase the other."""
    memory.remember("QA-1", {"plan": "guard the empty case"}, store)
    memory.remember("QA-1", {"status": "done"}, store)

    assert memory.recall("QA-1", store) == {"plan": "guard the empty case",
                                            "status": "done"}


def test_a_field_set_to_nothing_is_removed(store):
    memory.remember("QA-1", {"plan": "x", "status": "done"}, store)
    memory.remember("QA-1", {"plan": None}, store)
    assert sorted(memory.recall("QA-1", store)) == ["status"]


def test_forgetting_removes_the_whole_record(store):
    memory.remember("QA-1", {"status": "done"}, store)
    assert memory.forget("QA-1", store) is True
    assert memory.known("QA-1", store) is False
    assert memory.forget("QA-1", store) is False        # nothing left to forget


def test_two_keys_do_not_see_each_other(store):
    memory.remember("QA-1", {"status": "done"}, store)
    memory.remember("QA-2", {"status": "planned"}, store)
    assert memory.recall("QA-1", store)["status"] == "done"
    assert memory.recall("QA-2", store)["status"] == "planned"


def test_keys_can_be_narrowed_to_a_prefix(store):
    for key in ("web/QA-1", "web/QA-2", "api/QA-3"):
        memory.remember(key, {"seen": True}, store)
    assert memory.keys("web/", store) == ["web/QA-1", "web/QA-2"]


def test_a_record_carries_when_it_was_written(store):
    memory.remember("QA-1", {"status": "done"}, store)
    found = memory.describe("QA-1", store)
    assert found["created_at"] and found["updated_at"]
    assert found["fields"] == {"status": "done"}


# --------------------------------------------------------------------- counters
def test_a_counter_counts(store):
    assert memory.bump("QA-1", "attempts", path=store) == 1
    assert memory.bump("QA-1", "attempts", path=store) == 2
    assert memory.bump("QA-1", "attempts", by=3, path=store) == 5


def test_a_counter_survives_into_a_later_read(store):
    """The point of it: a budget that forgets an attempt bounds nothing."""
    memory.bump("QA-1", "attempts", path=store)
    memory.bump("QA-1", "attempts", path=store)
    assert memory.recall("QA-1", store)["attempts"] == 2


def test_a_counter_that_was_something_else_starts_over_rather_than_raising(store):
    memory.remember("QA-1", {"attempts": "not a number"}, store)
    assert memory.bump("QA-1", "attempts", path=store) == 1


# ----------------------------------------------------------------------- claims
def test_a_claim_is_exclusive(store):
    assert memory.claim("QA-1", "run-a", 60, store) == (True, "run-a")
    assert memory.claim("QA-1", "run-b", 60, store) == (False, "run-a")


def test_the_refusal_names_who_has_it(store):
    """So a run can say "this is being done by X" rather than only that it
    could not start."""
    memory.claim("QA-1", "run-a", 60, store)
    _ok, held = memory.claim("QA-1", "run-b", 60, store)
    assert held == "run-a"


def test_the_same_owner_taking_it_again_extends_it(store):
    """Which is what a resumed run needs."""
    memory.claim("QA-1", "run-a", 60, store)
    assert memory.claim("QA-1", "run-a", 60, store) == (True, "run-a")


def test_a_claim_expires_so_a_crash_does_not_hold_a_task_for_ever(store):
    memory.claim("QA-1", "run-a", 0.01, store)
    time.sleep(0.05)
    assert memory.claim("QA-1", "run-b", 60, store) == (True, "run-b")


def test_only_the_holder_may_give_it_back(store):
    """A late process must not free a claim somebody else now has."""
    memory.claim("QA-1", "run-a", 60, store)
    assert memory.release("QA-1", "run-b", store) is False
    assert memory.holder("QA-1", store) == "run-a"
    assert memory.release("QA-1", "run-a", store) is True
    assert memory.holder("QA-1", store) == ""


def test_an_expired_claim_reads_as_held_by_nobody(store):
    memory.claim("QA-1", "run-a", 0.01, store)
    time.sleep(0.05)
    assert memory.holder("QA-1", store) == ""


def test_releasing_a_claim_that_was_taken_over_does_nothing(store):
    """The newcomer's work is not the late run's to cancel."""
    memory.claim("QA-1", "run-a", 0.01, store)
    time.sleep(0.05)
    memory.claim("QA-1", "run-b", 60, store)

    assert memory.release("QA-1", "run-a", store) is False
    assert memory.holder("QA-1", store) == "run-b"


def test_a_claim_does_not_disturb_what_is_remembered(store):
    memory.remember("QA-1", {"status": "done"}, store)
    memory.claim("QA-1", "run-a", 60, store)
    assert memory.recall("QA-1", store) == {"status": "done"}


# ------------------------------------------------------------------ the locking
def test_concurrent_writers_do_not_lose_each_other(store):
    """The reason this store takes a lock and the secrets store does not: a
    read-modify-write from two directions drops one of them, and a budget that
    silently forgets an attempt bounds nothing."""
    def work():
        for _ in range(20):
            memory.bump("shared", "n", path=store)

    threads = [threading.Thread(target=work) for _ in range(6)]
    for one in threads:
        one.start()
    for one in threads:
        one.join()

    assert memory.recall("shared", store)["n"] == 120


def test_two_claims_at_once_still_produce_one_winner(store):
    """The dangerous case. Without the lock both could read "free" and both
    write themselves in."""
    won = []

    def take(name):
        ok, _holder = memory.claim("QA-1", name, 60, store)
        if ok:
            won.append(name)

    threads = [threading.Thread(target=take, args=("run-%d" % n,))
               for n in range(8)]
    for one in threads:
        one.start()
    for one in threads:
        one.join()

    assert len(won) == 1, won
    assert memory.holder("QA-1", store) == won[0]


def test_a_lock_left_behind_by_something_that_died_is_aged_out(store, monkeypatch):
    """Otherwise one crash makes the store unusable until somebody finds the
    file. The cost is named in LOCK_STALE: a process paused longer than that
    could have its lock broken under it."""
    memory.remember("QA-1", {"a": 1}, store)
    lock = memory._lock_path(store)
    with open(lock, "w") as handle:
        handle.write("999999")
    old = time.time() - memory.LOCK_STALE - 1
    os.utime(lock, (old, old))

    memory.remember("QA-1", {"b": 2}, store)            # must not hang
    assert memory.recall("QA-1", store) == {"a": 1, "b": 2}


def test_a_live_lock_is_waited_for_and_then_refused(store, monkeypatch):
    monkeypatch.setattr(memory, "LOCK_TIMEOUT", 0.15)
    lock = memory._lock_path(store)
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    with open(lock, "w") as handle:
        handle.write("1")

    with pytest.raises(memory.MemoryError_) as caught:
        memory.remember("QA-1", {"a": 1}, store)
    assert "locked" in str(caught.value)
    os.unlink(lock)


# ------------------------------------------------------------------- the file
def test_the_store_is_plain_json_somebody_can_open(store):
    """Unlike the secrets file. "Why did it skip that task" is a question
    somebody will ask, and the answer being readable is worth more than a
    confidentiality this does not need."""
    memory.remember("QA-1", {"status": "done"}, store)
    document = json.load(open(store, encoding="utf-8"))

    assert document["version"] == 1
    assert document["records"]["QA-1"]["fields"]["status"] == "done"


def test_a_store_that_is_not_json_says_so_rather_than_reading_as_empty(store):
    with open(store, "w", encoding="utf-8") as handle:
        handle.write("{ not json")
    with pytest.raises(memory.MemoryError_):
        memory.load(store)


def test_a_broken_store_does_not_make_recall_raise_into_a_step(store):
    """A step asking "have I done this" should hear "no", not an exception."""
    with open(store, "w", encoding="utf-8") as handle:
        handle.write("{ not json")
    assert memory.recall("QA-1", store) == {}
    assert memory.known("QA-1", store) is False
    assert memory.holder("QA-1", store) == ""


@pytest.mark.real_memory_default
def test_the_default_store_is_under_the_user_s_data_and_not_in_a_checkout():
    import runtime_paths

    where = memory.default_path()
    assert os.path.dirname(where) == runtime_paths.user_data_root()
    assert os.path.basename(where) == memory.FILE_NAME


def test_a_configured_path_wins_over_the_default(tmp_path):
    mine = str(tmp_path / "elsewhere.json")
    assert memory.resolve_path(mine) == mine
    assert memory.resolve_path("") == memory.default_path()
    assert memory.resolve_path("   ") == memory.default_path()
