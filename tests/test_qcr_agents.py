import os
import threading
import time

import pytest

from agent_cli_fixture import fake_agent
from codex_cli_fixture import fake_codex
from integrations.agents import sessions, store
from integrations.agents.contracts import AgentProfile
from qcr import Authority, Grant, QCRError, Registry
from qcr.adapters.agents import AgentRuntime
from qcr.engine import Engine
from qcr.executions import ExecutionStore


@pytest.fixture(params=["claude", "codex"])
def world(request, tmp_path):
    provider = request.param
    binary = fake_agent(tmp_path) if provider == "claude" else fake_codex(tmp_path)
    agents = str(tmp_path / "agents.json")
    connection, errors = store.upsert({"name": provider, "kind": provider, "binary": binary}, agents)
    assert not errors
    folder = tmp_path / "work"
    folder.mkdir()
    authority = Authority()
    registry = Registry(authority)
    executions = ExecutionStore(str(tmp_path / "execution.sqlite"))
    engine = Engine(registry, executions)
    root = str(tmp_path / "sessions")
    runtime = AgentRuntime(registry, engine, "p", root, agents)
    profile = AgentProfile("developer", "Developer", connection["id"], "p", str(folder))
    runtime.configure([profile])
    caller = authority.issue("person", [Grant("p", "agents.read"), Grant("p", "agents.execute")])
    yield runtime, caller, engine, executions, authority, provider, root, profile
    runtime.close()
    engine.close()
    executions.close()


def test_task_uses_the_shared_engine_and_existing_chat_store(world):
    runtime, caller, engine, executions, authority, provider, root, profile = world
    execution = runtime.submit(caller, "developer", "hello", idempotency_key="one")
    ended = engine.wait(caller, execution["execution_id"], timeout=10)
    assert ended["state"] == "succeeded", ended
    output = ended["output"]
    assert output["provider"] == provider
    assert output["task_id"] != ended["execution_id"] != output["session_id"]
    assert all(os.path.isfile(path) for path in output["artifacts"])
    assert len(sessions.load(root, output["session_id"])["turns"]) == 1
    again = runtime.submit(caller, "developer", "hello", idempotency_key="one")
    assert again["execution_id"] == ended["execution_id"]
    task = runtime.task(caller, ended["execution_id"])
    assert task["id"] == output["task_id"]
    second = runtime.submit(caller, "developer", "continue", session_id=output["session_id"])
    second = engine.wait(caller, second["execution_id"], timeout=10)
    assert second["state"] == "succeeded", second
    assert second["output"]["provider_session_id"] == output["provider_session_id"]
    assert second["output"]["task_id"] != output["task_id"]
    events = executions.events(ended["execution_id"])
    assert any(event.get("type") == "usage.reported" for event in events), events


def test_scope_and_identity_checks_happen_before_dispatch(world):
    runtime, caller, engine, executions, authority, provider, root, profile = world
    outsider = authority.issue("outsider", [Grant("elsewhere", "agents.execute")])
    with pytest.raises(QCRError):
        runtime.submit(outsider, "developer", "no")
    assert executions.list("p") == []
    foreign = sessions.create(root, {"id": profile.connection_id, "kind": provider}, profile.folder, "read")
    with pytest.raises(QCRError, match="Session"):
        runtime.submit(caller, "developer", "no", session_id=foreign["id"])
    assert executions.list("p") == []


def test_cancel_stops_the_process_and_late_output_cannot_change_terminal_state(world):
    runtime, caller, engine, executions, authority, provider, root, profile = world
    execution = runtime.submit(caller, "developer", "HANG")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if any(event.get("type") == "agent.started" for event in executions.events(execution["execution_id"])):
            break
        threading.Event().wait(.02)
    else:
        pytest.fail("agent did not start")
    engine.cancel(caller, execution["execution_id"])
    runtime.close()
    assert engine.get(caller, execution["execution_id"])["state"] == "canceled"
    assert not runtime._jobs


def test_timeout_and_provider_failure_remain_terminal(world):
    runtime, caller, engine, executions, authority, provider, root, profile = world
    failed = runtime.submit(caller, "developer", "REFUSE")
    assert engine.wait(caller, failed["execution_id"], timeout=10)["state"] == "failed"
    timed = runtime.submit(caller, "developer", "HANG", timeout_seconds=.2)
    assert engine.wait(caller, timed["execution_id"], timeout=10)["state"] == "timed_out"
    runtime.close()
    assert not runtime._jobs


def test_host_registers_profiles_on_its_existing_engine_and_refuses_socket_configuration(tmp_path):
    from dataclasses import asdict
    from qcr.host import Host
    agents = str(tmp_path / "agents.json")
    connection, errors = store.upsert({"name": "Codex", "kind": "codex", "binary": fake_codex(tmp_path)}, agents)
    assert not errors
    profile = AgentProfile("helper", "Helper", connection["id"], "p", str(tmp_path))
    host = Host("p", str(tmp_path / "journal.sqlite"), lambda line: None)
    try:
        host._on_hello({"protocol": 1, "actors": {"person": [
            {"permission": "agents.read"}, {"permission": "agents.execute"}]}})
        config = {"type": "agent_profiles", "profiles": [asdict(profile)],
                  "sessions_root": str(tmp_path / "sessions"), "agents_path": agents}
        assert host._on_agent_profiles(config) == {}
        assert host.agent_runtime.engine is host.engine
        assert not host.handle_agent("person", config)["ok"]
        result = host._on_invoke({"actor": "person", "capability": "agents.helper.execute",
                                  "input": {"task_id": "task-1", "message": "hello"}})
        execution = host.engine.wait(host._callers["person"], result["execution"]["execution_id"], timeout=10)
        assert execution["state"] == "succeeded", execution
    finally:
        host.close()
