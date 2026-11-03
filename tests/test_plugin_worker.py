"""Real worker processes with a TEST-ONLY RuntimeBackend, no OS isolation claim.

Production code only accepts approved Runtime requests. This fixture deliberately
runs repository-owned code locally to exercise IPC/process failures portably.
Native Bubblewrap transport proofs live in test_execution_runtime_linux.py.
"""

import asyncio
import copy
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from uuid import uuid4

import pytest

from execution_runtime import PolicyLayer, PolicyPatch, PolicyResolver
from execution_runtime.backends.base import ExecutionRequest, PreparedExecution, RuntimeBackend
from execution_runtime.channel import StdioChannel
from execution_runtime.errors import RuntimePolicyError
from execution_runtime.runtime import ExecutionRuntime
from plugin_host.attempts import AttemptStore
from plugin_host.gateway import HostGateway
from plugin_host.runner import execution_binding, run_plugin
from qavector_sdk import ContractError
from qavector_sdk.ipc import MAX_FRAME_BYTES, decode, encode
from qavector_sdk.worker import WorkerServer

ROOT = Path(__file__).parents[1]


def test_host_callback_deadline_belongs_to_pause_aware_runtime(monkeypatch):
    from qavector_sdk import worker
    server = WorkerServer(io.StringIO(), io.StringIO())
    server.attempt, server.status = 'attempt', 'running'
    server.context = {'execution_id': 'execution'}
    server.execute_params = {'request': {'limits': {'wall_time_seconds': 2}}}
    server.send = lambda message: server.callbacks[message['id']].set_result({'finished': True})
    async def observed(future, timeout):
        pytest.fail('A local wall-clock timer would count SIGSTOP time')
    monkeypatch.setattr(asyncio, 'wait_for', observed)
    async def call():
        token = worker._services.set(server)
        try:
            return await worker.host_call('services', 'process.run', {'tool': 'approved'})
        finally:
            worker._services.reset(token)
    assert asyncio.run(call()) == {'finished': True}
    assert not server.callbacks


def example(name):
    return json.loads((ROOT / "examples/plugin_contracts" / (name + ".json")).read_text())


class ProcessFixtureBackend(RuntimeBackend):
    provider, platform, stdio_protocol = "linux-isolated", "linux", True

    def __init__(self):
        self.starts, self.process, self.request = 0, None, None
        self.stderr = b""
        self.cleaned = False
        self.uncertain = False

    def capabilities(self):
        raise AssertionError("Synthetic transport test does not advertise isolation capabilities")

    def prepare(self, request):
        self.request = request
        return PreparedExecution(uuid4().hex, self.provider, request.policy.digest)

    def start(self, prepared):
        self.starts += 1
        self.process = subprocess.Popen(self.request.argv, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(ROOT),
            start_new_session=sys.platform != "win32")
        self.request.channel.attach(self.process.stdin, self.process.stdout)
        self.reader = threading.Thread(target=self.request.channel.read, daemon=True)
        self.reader.start()
        def drain():
            self.stderr = self.process.stderr.read()
            self.process.stderr.close()
        self.errors = threading.Thread(target=drain, daemon=True)
        self.errors.start()
        return prepared

    def stop(self, handle, deadline):
        if sys.platform != "win32":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        self.reader.join(5)
        self.errors.join(5)
        self.request.channel.close_input()
        self.request.channel.close()
        if self.uncertain:
            raise RuntimePolicyError("termination_unconfirmed", "Synthetic uncertain tree exit")

    def wait(self, handle):
        return self.inspect(handle)

    def inspect(self, handle):
        return {"state": "exited" if self.process.poll() is not None else "running",
                "reason": "exited", "exit_code": self.process.poll()}

    def cleanup(self, handle):
        self.cleaned = True


@pytest.fixture
def setup(tmp_path):
    if sys.platform == "win32":
        pytest.skip("The runtime stdio transport currently supports POSIX pipes only")
    backend = ProcessFixtureBackend()
    store = AttemptStore(tmp_path / "attempts.db")
    init = example("plugin-initialize")
    init["identity"]["contribution"]["entrypoint"] = "examples.plugin_contracts.count_provider:CountProvider"
    params = example("plugin-execute")
    # Only repository-owned code is loaded by this test fixture interpreter.
    bootstrap = ("import sys; sys.path[:0] = " + repr([str(ROOT), str(ROOT / "tests/fixtures")]) +
                 "; from qavector_sdk.worker import main; raise SystemExit(main())")
    policy = PolicyResolver().resolve([PolicyLayer("platform", PolicyPatch({}))], [],
                                      platform="linux", provider="linux-isolated")
    request = ExecutionRequest(policy, (sys.executable, "-I", "-B", "-c", bootstrap), str(tmp_path), timeout_seconds=10)
    yield backend, store, init, params, request
    if backend.process is not None and backend.process.poll() is None:
        backend.stop(None, time.monotonic())
    store.close()


def run(setup, provider=None, **kwargs):
    backend, store, init, params, request = setup
    if provider:
        init["identity"]["contribution"]["entrypoint"] = "plugin_providers:" + provider
    return run_plugin(ExecutionRuntime(backend), request, init, params, store, **kwargs)


def test_worker_runs_node_and_persists_events_and_result_before_return(setup):
    backend, store, init, params, request = setup
    result = run(setup)
    assert result["status"] == "completed", result
    assert result["output"] == {"count": 2}
    assert backend.cleaned and backend.process.poll() is not None
    assert store.snapshot("attempt-1")["result"] == result
    events = store.events("attempt-1")
    assert [row["event"]["type"] for row in events] == ["started", "completed"]
    assert store.events("attempt-1", events[0]["cursor"]) == events[1:]
    assert run(setup) == result
    assert backend.starts == 1  # Same attempt is never executed twice.


def test_running_receipt_blocks_blind_reexecution_after_restart(setup):
    backend, store, init, params, request = setup
    store.claim(init, params, runtime_binding=execution_binding(request))
    with pytest.raises(ContractError) as caught:
        run(setup)
    assert caught.value.code == "EXECUTION_STATE_UNKNOWN"
    assert backend.starts == 0


def test_attempt_cannot_reuse_result_under_changed_runtime_binding(setup):
    from dataclasses import replace
    backend, store, init, params, request = setup
    assert run(setup)["status"] == "completed"
    with pytest.raises(ContractError, match="different execution data"):
        run((backend, store, init, params, replace(request, timeout_seconds=9)))
    assert backend.starts == 1


@pytest.mark.parametrize("provider,code", [("Crash", "WORKER_CRASHED"),
    ("InvalidOutput", "OUTPUT_SCHEMA_INVALID"), ("UnverifiedArtifact", "POLICY_UNSATISFIED")])
def test_worker_crash_invalid_output_and_unverified_artifacts_fail_one_attempt(setup, provider, code):
    result = run(setup, provider)
    assert result["status"] == "failed", result
    assert result["error"]["code"] == code
    assert setup[0].cleaned


def test_plugin_print_is_stderr_not_protocol_corruption(setup):
    result = run(setup, "Noisy")
    assert result["status"] == "completed", result
    assert b"ordinary plugin diagnostic" in setup[0].stderr


def test_scoped_host_callback_is_bound_to_attempt_and_policy(setup):
    params = setup[3]
    params["context"]["tools_handle"] = "tools-1"
    calls = []
    def authorize(operation, arguments):
        calls.append(operation)
        return True
    gateway = HostGateway("execution-1", "attempt-1", {"tools-1": {"counter.read": lambda _: {"count": 7}}}, authorize)
    result = run(setup, "ScopedCall", gateway=gateway)
    assert result["output"] == {"count": 7}, result
    assert calls == ["counter.read"] and gateway.closed


def test_ungranted_host_callback_does_not_dispatch_service(setup):
    setup[3]["context"]["tools_handle"] = "ungranted"
    gateway = HostGateway("execution-1", "attempt-1", {}, lambda *_: pytest.fail("No grant"))
    result = run(setup, "ScopedCall", gateway=gateway)
    assert result["status"] == "failed", result
    assert result["error"]["code"] == "POLICY_UNSATISFIED"


@pytest.mark.parametrize("timed_out", [False, True])
def test_timeout_and_cancel_confirm_process_exit_before_terminal_status(setup, timed_out):
    from dataclasses import replace
    backend, store, init, params, request = setup
    stop = threading.Event()
    timer = None
    if timed_out:
        request = replace(request, timeout_seconds=1.5)
        setup = backend, store, init, params, request
    else:
        timer = threading.Timer(1.5, stop.set)
        timer.start()
    try:
        result = run(setup, "Slow", cancel=stop)
    finally:
        if timer:
            timer.cancel()
    assert result["status"] == ("timed_out" if timed_out else "cancelled"), result
    assert backend.cleaned and backend.process.poll() is not None


def test_unconfirmed_runtime_termination_is_unknown_not_cancelled(setup):
    setup[0].uncertain = True
    result = run(setup)
    assert result["status"] == "unknown", result
    assert result["error"]["code"] == "EXECUTION_STATE_UNKNOWN"
    assert result["error"]["retryable"] is False


def test_stopping_local_worker_does_not_claim_remote_job_cancellation(setup):
    from dataclasses import replace
    backend, store, init, params, request = setup
    init["execution_scope"] = "remote_job"
    result = run((backend, store, init, params, replace(request, timeout_seconds=1.5)), "Slow")
    assert result["status"] == "unknown", result
    assert result["error"]["retryable"] is False
    assert backend.cleaned


def test_unsupported_runtime_transport_never_launches_worker(setup):
    setup[0].stdio_protocol = False
    result = run(setup)
    assert result["error"]["code"] == "POLICY_UNSATISFIED"
    assert setup[0].starts == 0


@pytest.mark.parametrize("raw", [b'{"jsonrpc":"2.0","id":"a","id":"b"}\n',
    b'{"value":NaN}\n', b'[]\n', b'{}', b'\xff\n', b'x' * (MAX_FRAME_BYTES + 1)])
def test_corrupt_frames_are_bounded_and_rejected(raw):
    with pytest.raises(ContractError) as caught:
        decode(raw)
    assert caught.value.code == "WORKER_PROTOCOL_ERROR"


def test_handshake_rejects_version_before_importing_plugin(monkeypatch):
    init = example("plugin-initialize")
    init["protocol_version"] = "2.0.0"
    monkeypatch.setattr(WorkerServer, "_backend", lambda *_: pytest.fail("No plugin import before compatible handshake"))
    server = WorkerServer(io.BytesIO(), io.BytesIO())
    with pytest.raises(ContractError):
        asyncio.run(server.dispatch("initialize", init))
    init["protocol_version"] = "1.0.0"
    assert asyncio.run(server.dispatch("initialize", init))["protocol_version"] == "1.0.0"


def test_repeated_execute_is_idempotent_and_changed_request_rejected():
    async def scenario():
        init = example("plugin-initialize")
        init["identity"]["contribution"]["entrypoint"] = "never_import_this:Backend"
        server = WorkerServer(io.BytesIO(), io.BytesIO())
        await server.dispatch("initialize", init)
        params = example("plugin-execute")
        first = await server.dispatch("execute", params)
        assert await server.dispatch("execute", params) == first
        different = copy.deepcopy(params)
        different["request"]["inputs"] = {"values": [7]}
        with pytest.raises(ContractError):
            await server.dispatch("execute", different)
        server.task.cancel()
        await asyncio.gather(server.task, return_exceptions=True)
    asyncio.run(scenario())


def test_host_gateway_rechecks_scope_and_policy_every_call():
    allowed = [True]
    calls = []
    gateway = HostGateway("execution-1", "attempt-1", {"tools-scope-1": {
        "artifacts.describe": lambda args: calls.append(args) or {}}}, lambda *_: allowed[0])
    request = example("plugin-host-call")
    assert gateway.call(request) == {}
    allowed[0] = False
    with pytest.raises(ContractError):
        gateway.call(request)
    allowed[0] = True
    with pytest.raises(ContractError):
        gateway.call(dict(request, attempt_id="another"))
    gateway.close()
    with pytest.raises(ContractError):
        gateway.call(request)
    assert len(calls) == 1


def test_event_deduplication_conflicts_and_terminal_monotonicity(setup):
    store, init, params = setup[1:4]
    store.claim(init, params)
    event = example("node-event")
    assert store.append(event)
    assert store.append(event) is False
    with pytest.raises(ContractError):
        store.append(dict(event, timestamp="2026-10-30T13:00:00Z"))
    result = example("node-result")
    terminal = dict(event, event_id="terminal-1", sequence=1, type="completed", payload={"result": result})
    store.finish(result, terminal)
    store.finish(result, terminal)
    with pytest.raises(ContractError):
        store.append(dict(event, event_id="after-terminal", sequence=2))


def test_transport_fails_closed_on_backpressure():
    channel = StdioChannel(max_pending=1)
    channel._stdout = io.BytesIO(b'one\ntwo\n')
    assert channel.read() == b""
    with pytest.raises(RuntimePolicyError) as caught:
        channel.receive()
    assert caught.value.code == "protocol_backpressure"


def test_same_worker_and_runtime_execute_agent_backend_contract(setup):
    backend, store, init, params, request = setup
    init["identity"].update(plugin_id="example.agent.review", contribution={
        "kind": "agent_backend", "id": "example.agent.review",
        "entrypoint": "plugin_providers:FakeAgent", "config_schema": "schemas/config.json"})
    init["schemas"] = {"config": {"type": "object", "additionalProperties": False},
                       "input": True, "output": example("agent-request")["output_schema"]}
    params.update(request=example("agent-request"), profile=example("agent-profile"),
                  requirements=example("agent-requirements"))
    result = run(setup)
    assert result["status"] == "completed", result
    assert result["output"] == {"verdict": "changes_required"}
    assert result["usage"] is None
    assert backend.cleaned


@pytest.mark.parametrize("mode,status,code", [
    ("success", "completed", None),
    ("logged_out", "failed", "AGENT_PREREQUISITE_MISSING"),
    ("missing_binary", "failed", "AGENT_PREREQUISITE_MISSING"),
    ("bad_output", "failed", "OUTPUT_SCHEMA_INVALID"),
    ("no_result", "unknown", "EXECUTION_STATE_UNKNOWN"),
    ("resume", "failed", "CAPABILITY_UNSUPPORTED"),
    ("oversized_output", "failed", "WORKER_PROTOCOL_ERROR"),
])
def test_claude_backend_runs_inside_sdk_worker(setup, tmp_path, mode, status, code):
    from claude_backend_fixture import configure, fake_cli
    backend, store, init, params, request = setup
    kwargs = {"authenticated": mode != "logged_out"}
    if mode == "bad_output":
        kwargs["answer"] = {"type": "result", "result": '{"verdict":"unexpected"}'}
    if mode == "no_result":
        kwargs["body"] = "print('connection closed')\n"
    if mode == "oversized_output":
        kwargs["body"] = "print('x' * (2 * 1024 * 1024 + 1))\n"
    binary = fake_cli(tmp_path / "claude", sys.executable, **kwargs)
    configure(init, params, binary if mode != "missing_binary" else str(tmp_path / "absent"))
    if mode == "resume":
        params["request"]["session_ref"] = "unsupported-session"
    result = run(setup)
    assert result["status"] == status, result
    assert backend.cleaned and backend.process.poll() is not None
    if code:
        assert result["error"]["code"] == code, result
    else:
        assert result["output"] == {"verdict": "approved"}
        assert result["usage"]["metrics"] == [{"name": "turns", "value": 1, "unit": "turns"}]
        assert [row["event"]["type"] for row in store.events("attempt-1")] == [
            "started", "usage.updated", "completed"]
    assert run(setup) == result and backend.starts == 1


def test_claude_worker_resolves_workspace_through_scoped_gateway(setup, tmp_path):
    from claude_backend_fixture import configure, fake_cli
    backend, store, init, params, request = setup
    binary = fake_cli(tmp_path / "claude", sys.executable, body=(
        "import os\nprint(json.dumps({'type': 'result', 'result': os.getcwd()}))\n"))
    configure(init, params, binary)
    params["request"].update(workspace_ref="workspace-opaque-id", output_schema=None)
    calls = []

    def resolve(arguments):
        calls.append(arguments)
        return {"path": str(tmp_path)}

    gateway = HostGateway(params["context"]["execution_id"], params["request"]["attempt_id"],
        {params["context"]["runtime_handle"]: {"workspace.resolve": resolve}},
        lambda *args: True)
    result = run(setup, gateway=gateway)
    assert result["status"] == "completed" and result["output"] == str(tmp_path), result
    assert calls == [{"workspace_ref": "workspace-opaque-id"}]


def test_pipe_event_loop_supports_threads_without_socketpair(monkeypatch):
    if sys.platform == "win32":
        pytest.skip("POSIX worker event loop")
    import socket
    from qavector_sdk.event_loop import run as run_loop
    monkeypatch.setattr(socket, "socketpair", lambda *_a, **_k: pytest.fail("Socketpair is forbidden"))
    async def task():
        return await asyncio.to_thread(lambda: 7)
    assert run_loop(task()) == 7
