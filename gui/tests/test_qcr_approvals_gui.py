"""Cycle approvals through QCR (ADR-003 3.3): the window's side.

What an agent is shown of the questions on screen, and an answer given in its
name going down the very channel the dialog or the Approve button uses - with
its name on it, once.
"""

import functools
import os
import time
import types

import pytest

from cms_gui import qcrhost
from cms_gui.askbridge import AskBridge
from cms_gui.main_window import MainWindow
from test_qcr_scenario_run import Recorder, owner_events

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORE_PYTHON = os.path.join(ROOT, ".venv", "bin", "python")


def ask_event(request_id, **extra):
    return dict({"kind": "cycle.ask", "id": request_id, "step": "gate", "question": "Ship it?",
                 "detail": "", "options": ["Approve", "Reject"], "timeout_ms": 600000}, **extra)


@pytest.fixture
def bridge(qapp):
    sent = []
    one = AskBridge(lambda **command: sent.append(command))
    one.sent = sent
    yield one
    one.cancel_all()


def test_the_questions_on_screen_are_shown_by_kind(bridge):
    bridge.handle(ask_event(1))
    bridge.handle(ask_event(2, revision={"enabled": True, "plan": "x"}))
    bridge.handle(ask_event(3, options=["Send", "Cancel"], text={"required": True, "placeholder": "why"}))
    bridge.handle(ask_event(4, options=["Choose", "Cancel"], choice={
        "mode": "single", "columns": ["Name"], "value_column": 0,
        "rows": [{"cells": ["a"], "detail": "", "checked": False}, {"cells": ["b"], "detail": "", "checked": True}]}))
    items = {item["id"]: item for item in bridge.pending_items()}
    assert [items["ask:%d" % n]["kind"] for n in (1, 2, 3, 4)] == ["approval", "revision", "input", "choice"]
    assert items["ask:2"]["revision"] == {"enabled": True}
    assert items["ask:3"]["text"] == {"required": True, "placeholder": "why"}
    assert items["ask:4"]["choice"] == {"mode": "single", "columns": ["Name"], "rows": [["a"], ["b"]]}


def test_an_answer_in_an_agent_s_name_is_sent_once_and_the_dialog_goes(bridge):
    bridge.handle(ask_event(1))
    assert bridge.answer("1", "Approve", "agent:claude/s1") is True
    assert bridge.sent == [{"command": "ask.result", "id": 1, "answer": "Approve", "who": "agent:claude/s1"}]
    assert bridge.pending_items() == [] and bridge.waiting() == []
    assert bridge.answer("1", "Approve", "agent:claude/s1") is False, "nothing left to answer"


def test_a_revision_is_asked_for_with_feedback(bridge):
    bridge.handle(ask_event(2, revision={"enabled": True}))
    bridge.answer(2, "", "agent:x", revise=True, feedback="split step two")
    assert bridge.sent == [{"command": "ask.result", "id": 2, "answer": "", "who": "agent:x",
                            "revise": True, "feedback": "split step two"}]


# -- the main window --------------------------------------------------------------------
def window(cycle=None, running=True, bridge_=None):
    host, process = Recorder(), Recorder()
    host.actor_of = lambda execution_id: "agent:claude/s1"
    host.set_pending = lambda pending: host.calls.append(("set_pending", (pending,), {}))
    process.send_command = lambda **command: (process.calls.append(("send_command", (), command)), True)[1]
    fake = types.SimpleNamespace(qcr_host=host, process=process, ask_bridge=bridge_ or types.SimpleNamespace(
        pending_items=lambda: [], answer=lambda *a, **k: False), _qcr_answers={},
        run_state=types.SimpleNamespace(cycle=cycle, cycle_running=running))
    for name in ("_qcr_pending", "_qcr_push_pending", "_qcr_cycle_answer", "_qcr_approval_event"):
        setattr(fake, name, functools.partial(getattr(MainWindow, name), fake))
    return fake, host, process


CYCLE = {"cycle": "nightly", "run_id": "r-1", "node_approvals": {
    "abc": {"id": "abc", "status": "pending", "step": "deploy", "label": "Deploy", "fingerprint": "f1",
            "purpose": "Deploys", "resolved_inputs": {"target": "staging"}},
    "old": {"id": "old", "status": "approved", "step": "build", "fingerprint": "f0"}}}


def test_what_is_open_names_the_cycle_its_run_and_every_open_item(qapp, bridge):
    bridge.handle(ask_event(7))
    fake, _host, _process = window(CYCLE, bridge_=bridge)
    pending = fake._qcr_pending()
    assert pending["cycle"] == "nightly" and pending["run_id"] == "r-1"
    assert [(i["id"], i["kind"]) for i in pending["items"]] == [("node:abc", "node_approval"), ("ask:7", "approval")]
    assert pending["items"][0]["inputs"] == {"target": "staging"} and pending["items"][0]["question"] == "Run Deploy?"
    assert window(None)[0]._qcr_pending() is None


def test_a_node_answer_carries_who_and_ends_when_the_core_decides(qapp):
    fake, host, process = window(CYCLE)
    fake._qcr_cycle_answer("e1", "nightly~approvals", {"item": "node:abc", "answer": "approve"})
    assert ("send_command", (), {"command": "cycle.node.approval.answer", "run_id": "r-1", "request_id": "abc",
                                 "fingerprint": "f1", "approved": True, "who": "agent:claude/s1"}) in process.calls
    assert [state for state, _ in owner_events(host)] == ["accepted"], "not decided until the core says so"
    fake._qcr_approval_event({"kind": "cycle.node.approval", "receipt": {"id": "abc", "status": "approved"}})
    assert owner_events(host)[-1] == ("succeeded", {"output": {"item": "node:abc", "decided": "approved"}})


def test_a_refused_node_answer_fails_with_the_core_s_reason(qapp):
    fake, host, _process = window(CYCLE)
    fake._qcr_cycle_answer("e1", "nightly~approvals", {"item": "node:abc", "answer": "decline"})
    fake._qcr_approval_event({"kind": "cycle.control.refused", "action": "node_approval",
                              "reason": "The node inputs changed. Review a new approval request."})
    state, fields = owner_events(host)[-1]
    assert state == "failed" and fields["error"]["code"] == "refused" and "inputs changed" in fields["error"]["message"]


def test_an_item_that_closed_meanwhile_is_not_pending(qapp):
    fake, host, process = window(dict(CYCLE, node_approvals={"abc": dict(CYCLE["node_approvals"]["abc"], status="approved")}))
    fake._qcr_cycle_answer("e1", "nightly~approvals", {"item": "node:abc", "answer": "approve"})
    assert owner_events(host)[-1][1]["error"]["code"] == "not_pending"
    assert not [c for c in process.calls if c[0] == "send_command"]


def test_a_question_is_answered_through_the_bridge_in_the_agent_s_name(qapp, bridge):
    bridge.handle(ask_event(3, options=["Send", "Cancel"], text={"required": True}))
    fake, host, _process = window(CYCLE, bridge_=bridge)
    fake._qcr_cycle_answer("e1", "nightly~approvals", {"item": "ask:3", "answer": "Send", "text": "because"})
    assert bridge.sent == [{"command": "ask.result", "id": 3, "answer": "Send", "who": "agent:claude/s1",
                            "text": "because"}]
    assert owner_events(host)[-1] == ("succeeded", {"output": {"item": "ask:3", "decided": "answered"}})


@pytest.mark.skipif(not os.path.exists(CORE_PYTHON), reason="needs the core's .venv")
def test_an_agent_with_the_approve_grant_answers_a_gate_through_the_real_host(qapp, tmp_path, bridge):
    from cms_gui import servicesfile as sf
    from cms_gui.services import ServiceSupervisor
    supervisor = ServiceSupervisor()
    supervisor.sync([sf.ProjectRow(name="Group", runners=[])])
    host = qcrhost.QcrHost(supervisor)
    fake, _h, _p = window(CYCLE, bridge_=bridge)
    host.set_handler("cycle.answer", lambda eid, rid, given: MainWindow._qcr_cycle_answer(
        types.SimpleNamespace(qcr_host=host, ask_bridge=bridge, run_state=fake.run_state, process=fake.process,
                              _qcr_answers={}), eid, rid, given))
    host.set_inventory({"schema_version": 1, "project_id": "p", "scenarios": [], "environments": [],
                        "cycles": [{"id": "nightly", "name": "Nightly", "steps": 1, "problems": []}]})
    bridge.handle(ask_event(5))
    host.start([CORE_PYTHON, os.path.join(ROOT, "session_launcher.py"), "--qcr-host", "--qcr-socket=off", "--qcr-project=p",
                "--qcr-store=" + qcrhost.store_path(str(tmp_path))], "p")
    replies, ended = [], []
    host.execution_changed.connect(lambda e: ended.append(e) if e["state"] in qcrhost.TERMINAL else None)
    try:
        end = time.monotonic() + 20
        while host.state != qcrhost.READY and time.monotonic() < end:
            qapp.processEvents()
            time.sleep(0.01)
        host.set_pending({"cycle": "nightly", "run_id": "r-1", "items": bridge.pending_items()})
        host._send({"type": "actor", "id": 900, "actor": "agent:claude/s1",
                    "grants": [{"permission": "cycles.read"}, {"permission": "cycles.approve"}]})
        request_id = next(host._ids)
        host._replies[request_id] = replies.append
        host._send({"type": "invoke", "id": request_id, "actor": "agent:claude/s1",
                    "capability": "cycles.nightly.answer", "input": {"item": "ask:5", "answer": "Approve"}})
        end = time.monotonic() + 20
        while not ended and time.monotonic() < end:
            qapp.processEvents()
            time.sleep(0.01)
        assert replies[0]["ok"], replies
        assert ended[0]["state"] == "succeeded" and ended[0]["actor_id"] == "agent:claude/s1"
        assert bridge.sent == [{"command": "ask.result", "id": 5, "answer": "Approve", "who": "agent:claude/s1"}]
    finally:
        host.stop()
        supervisor.shutdown(1000)
