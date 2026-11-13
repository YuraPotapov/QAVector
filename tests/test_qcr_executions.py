"""Execution records (ADR-002 D3/D4/D6/D8): written before acting, honest states, no retries."""

import itertools
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading

import pytest

from qcr import executions as ex
from qcr.executions import ExecutionError, ExecutionStore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START = "services.web.start"


@pytest.fixture
def store(tmp_path):
    ticks = itertools.count(1000)
    ids = ("e%d" % n for n in itertools.count(1))
    one = ExecutionStore(str(tmp_path / ".qcr" / "executions.sqlite"),
                         clock=lambda: float(next(ticks)), new_id=lambda: next(ids))
    yield one
    one.close()


def create(store, **kwargs):
    args = dict(resource_id="web", exclusive=True)
    args.update(kwargs)
    return store.create("p", START, 1, "gui-user", {"wait_ready": True}, **args)


def states(store, execution_id):
    return [(e["type"], e.get("state")) for e in store.events(execution_id)]


def test_a_new_execution_is_queued_and_written_with_who_asked(store):
    execution, created = create(store, correlation_id="c-1")
    assert created and execution["state"] == "queued"
    assert {k: execution[k] for k in ("execution_id", "project_id", "capability_id", "version", "actor_id",
                                      "resource_id", "correlation_id", "input", "output", "error")} == {
        "execution_id": "e1", "project_id": "p", "capability_id": START, "version": 1, "actor_id": "gui-user",
        "resource_id": "web", "correlation_id": "c-1", "input": {"wait_ready": True}, "output": None, "error": None}
    assert states(store, "e1") == [("state", "queued")]


def test_the_happy_path_ends_once_with_its_output(store):
    create(store)
    store.transition("e1", "dispatched")
    store.progress("e1", "service.status", {"status": "starting"})
    store.transition("e1", "running")
    done = store.transition("e1", "succeeded", output={"service_status": "running", "readiness": "ready"})
    assert done["state"] == "succeeded" and done["output"]["readiness"] == "ready"
    assert done["finished_at"] is not None
    assert states(store, "e1") == [("state", "queued"), ("state", "dispatched"), ("service.status", None),
                                   ("state", "running"), ("state", "succeeded")]
    # The same outcome again is a no-op; another one is refused.
    store.transition("e1", "succeeded", output={"service_status": "running", "readiness": "ready"})
    with pytest.raises(ExecutionError) as caught:
        store.transition("e1", "failed", error={"code": "service_failed"})
    assert caught.value.code == "terminal"
    with pytest.raises(ExecutionError) as late:
        store.progress("e1", "service.status", {})
    assert late.value.code == "terminal"


@pytest.mark.parametrize("path", [["running"], ["succeeded"], ["dispatched", "queued"]])
def test_states_move_only_forward(store, path):
    create(store)
    with pytest.raises(ExecutionError) as caught:
        for state in path:
            store.transition("e1", state)
    assert caught.value.code == "invalid_transition"


def test_an_idempotency_key_returns_the_same_execution_or_refuses_other_work(store):
    first, _ = create(store, idempotency_key="k1")
    again, created = create(store, idempotency_key="k1")
    assert not created and again["execution_id"] == first["execution_id"]
    with pytest.raises(ExecutionError) as caught:
        store.create("p", START, 1, "gui-user", {"wait_ready": False}, resource_id="web", idempotency_key="k1")
    assert caught.value.code == "conflict"


def test_one_unfinished_execution_per_resource(store):
    create(store)
    with pytest.raises(ExecutionError) as caught:
        create(store)
    assert caught.value.code == "busy" and caught.value.execution_id == "e1"
    assert store.active("p", "web") == "e1"
    store.transition("e1", "canceled")
    assert store.active("p", "web") is None
    assert create(store)[0]["execution_id"] == "e2"


def test_two_writers_on_one_file_let_exactly_one_exclusive_execution_in(tmp_path):
    path = str(tmp_path / "x.sqlite")
    stores = [ExecutionStore(path, new_id=lambda n=n: "w%d" % n) for n in range(2)]
    won, lost, gate = [], [], threading.Barrier(2)

    def go(store):
        gate.wait()
        try:
            won.append(create(store)[0]["execution_id"])
        except ExecutionError as exc:
            lost.append(exc.code)
    threads = [threading.Thread(target=go, args=(one,)) for one in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(won) == 1 and lost == ["busy"]
    for one in stores:
        one.close()


def test_reconcile_turns_unfinished_into_unknown_with_what_is_true_now(store):
    create(store)
    store.transition("e1", "dispatched")
    create(store, resource_id="db")
    store.transition("e2", "dispatched")
    store.transition("e2", "succeeded", output={})
    observed = []

    def observe(execution):
        observed.append(execution["execution_id"])
        return {"service_status": "running"}
    assert store.reconcile(observe) == ["e1"]
    after = store.get("e1")
    assert after["state"] == "unknown" and after["error"]["code"] == "interrupted"
    assert store.events("e1")[-1]["payload"] == {"reason": "host_restarted",
                                                  "observed": {"service_status": "running"}}
    assert observed == ["e1"] and store.get("e2")["state"] == "succeeded"


def test_an_observation_that_fails_is_left_out_not_fatal(store):
    create(store)

    def broken(execution):
        raise RuntimeError("owner gone")
    store.reconcile(broken)
    assert store.events("e1")[-1]["payload"] == {"reason": "host_restarted"}


def test_progress_beyond_the_limit_is_counted_and_the_outcome_still_fits(store, monkeypatch):
    monkeypatch.setattr(ex, "EVENT_LIMIT", 5)
    create(store)
    kept = [store.progress("e1", "tick") for _ in range(5)]
    assert kept == [True, True, False, False, False]
    assert store.get("e1")["dropped_events"] == 3
    assert store.transition("e1", "canceled")["state"] == "canceled"


def test_bulk_and_non_json_documents_are_refused(store):
    with pytest.raises(ExecutionError) as large:
        store.create("p", START, 1, "a", {"log": "x" * (ex.DOCUMENT_BYTES + 1)})
    with pytest.raises(ExecutionError) as bad:
        store.create("p", START, 1, "a", {"x": object()})
    assert (large.value.code, bad.value.code) == ("too_large", "invalid_input")
    assert store.list("p") == []


def test_list_filters_newest_first_and_pages(store):
    create(store)
    store.transition("e1", "canceled")
    create(store, resource_id="db")
    store.create("p", "services.web.stop", 1, "cycle:r1", {}, resource_id="web")
    store.create("q", START, 1, "gui-user", {}, resource_id="web")
    assert [e["execution_id"] for e in store.list("p")] == ["e3", "e2", "e1"]
    assert [e["execution_id"] for e in store.list("p", state="canceled")] == ["e1"]
    assert [e["execution_id"] for e in store.list("p", actor_id="cycle:r1")] == ["e3"]
    assert [e["execution_id"] for e in store.list("p", resource_id="web")] == ["e3", "e1"]
    newest = store.list("p", limit=1)[0]
    assert [e["execution_id"] for e in store.list("p", before=newest["created_at"])] == ["e2", "e1"]


def test_prune_forgets_only_the_oldest_finished(store):
    for n in range(4):
        create(store, resource_id="r%d" % n)
    for execution_id in ("e1", "e2", "e3"):
        store.transition(execution_id, "canceled")
    assert store.prune(keep=1) == 2
    assert sorted(e["execution_id"] for e in store.list("p")) == ["e3", "e4"]
    with pytest.raises(ExecutionError) as caught:
        store.events("e1")
    assert caught.value.code == "not_found"


def test_a_journal_that_cannot_be_written_refuses_before_anything_happens(store, monkeypatch):
    def broken(*args):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(store._journal, "db", type("Db", (), {"execute": staticmethod(broken),
                                                               "close": lambda self: None})())
    with pytest.raises(ExecutionError) as caught:
        create(store)
    assert caught.value.code == "journal_unavailable"


def test_an_unusable_location_is_journal_unavailable(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    with pytest.raises(ExecutionError) as caught:
        ExecutionStore(str(blocker / "executions.sqlite"))
    assert caught.value.code == "journal_unavailable"


def test_a_host_dying_after_create_leaves_a_record_that_reconciles_to_unknown(tmp_path):
    path = str(tmp_path / "e.sqlite")
    script = textwrap.dedent("""
        import os, sys
        sys.path.insert(0, %r)
        from qcr.executions import ExecutionStore
        s = ExecutionStore(%r, new_id=lambda: "e1")
        s.create("p", "services.web.start", 1, "gui-user", {}, resource_id="web", exclusive=True)
        s.transition("e1", "dispatched")
        os._exit(0)
    """ % (ROOT, path))
    subprocess.run([sys.executable, "-c", script], check=True, timeout=60)
    reopened = ExecutionStore(path)
    assert reopened.get("e1")["state"] == "dispatched"
    assert reopened.reconcile() == ["e1"] and reopened.get("e1")["state"] == "unknown"
    assert reopened.active("p", "web") is None
    reopened.close()
