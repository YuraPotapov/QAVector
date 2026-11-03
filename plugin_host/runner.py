"""Provider-neutral worker execution through the shared Execution Runtime."""

import threading
from contextlib import nullcontext
import time
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from execution_runtime.channel import StdioChannel
from execution_runtime.errors import RuntimePolicyError
from qavector_sdk import ContractError, validate_contract, validate_output
from qavector_sdk.ipc import decode, encode, error_response, failure


class _Interrupted(Exception):
    def __init__(self, timed_out):
        self.timed_out = timed_out


class WorkerClient:
    def __init__(self, channel, initialization, params, store, deadline, cancel,
                 gateway=None, artifact_validator=None, pause=None):
        self.channel, self.initialization, self.params = channel, initialization, params
        self.store, self.deadline, self.cancel = store, deadline, cancel
        self.gateway, self.artifact_validator = gateway, artifact_validator
        self.pause = pause
        self.now = pause.clock.now if pause else time.monotonic
        self.attempt = params["request"]["attempt_id"]
        self.kind = "agent" if initialization["identity"]["contribution"]["kind"] == "agent_backend" else "node"
        self.terminal = None

    def _check(self):
        if self.pause:
            self.pause.wait()
        if self.cancel.is_set():
            raise _Interrupted(False)
        if self.now() >= self.deadline:
            raise _Interrupted(True)

    def send(self, message):
        with self.pause.io() if self.pause else nullcontext():
            self._check()
            self.channel.send(encode(message), timeout=min(1.0, max(0.001, self.deadline - self.now())))

    def validate_result(self, result):
        validate_contract(self.kind + "-result", result)
        identity = self.initialization["identity"]
        reference = "backend_ref" if self.kind == "agent" else "contribution_ref"
        if (result["attempt_id"] != self.attempt or result[reference] != identity["contribution"]["id"]
                or result["plugin_version"] != identity["version"]):
            raise ContractError("WORKER_PROTOCOL_ERROR", "Worker result identity mismatch")
        for artifact in result["artifact_refs"]:
            if self.artifact_validator is None or self.artifact_validator(artifact) is not True:
                raise ContractError("POLICY_UNSATISFIED", "Artifact reference was not verified in the execution scope")
        if result["status"] == "completed":
            validate_output(result["output"], self.initialization["schemas"]["output"])
            expected = self.params["request"]["output_schema"]
            if expected is not None:
                validate_output(result["output"], expected)
        if result["status"] == "cancelled":
            raise ContractError("WORKER_PROTOCOL_ERROR", "Worker cannot confirm process-tree cancellation")

    def _event(self, event):
        validate_contract(self.kind + "-event", event)
        if event["attempt_id"] != self.attempt:
            raise ContractError("WORKER_PROTOCOL_ERROR", "Event belongs to another attempt")
        if self.terminal is not None:
            if event == self.terminal:
                return
            raise ContractError("WORKER_PROTOCOL_ERROR", "Event arrived after terminal result")
        if event["type"] == "input.required":
            raise ContractError("CAPABILITY_UNSUPPORTED", "Interactive input was not negotiated")
        if event["type"] == "artifact.created":
            artifact = event["payload"]["artifact_ref"]
            if self.artifact_validator is None or self.artifact_validator(artifact) is not True:
                raise ContractError("POLICY_UNSATISFIED", "Artifact event is outside the execution scope")
        if event["type"] in ("completed", "failed", "cancelled"):
            self.validate_result(event["payload"]["result"])
            self.store.check_terminal(event)
            self.terminal = event  # Publish atomically only after Runtime cleanup.
        else:
            self.store.append(event)

    def rpc(self, method, params):
        self._check()
        identifier = uuid4().hex
        self.send({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
        while True:
            self._check()
            raw = self.channel.receive(timeout=0.02)
            if raw is None:
                continue
            message = decode(raw)
            if message.get("method") == "event":
                self._event(message["params"])
                continue
            if message.get("method") == "host.call" and "id" in message:
                try:
                    if self.gateway is None or self.terminal is not None:
                        raise ContractError("POLICY_UNSATISFIED", "Host API access is not available")
                    result = self.gateway.call(message["params"])
                    reply = {"jsonrpc": "2.0", "id": message["id"], "result": result}
                except Exception as exc:
                    code = exc.code if isinstance(exc, ContractError) else "POLICY_UNSATISFIED"
                    reply = error_response(message["id"], code, "Host API request rejected")
                self.send(reply)
                continue
            if "method" in message or message.get("id") != identifier:
                raise ContractError("WORKER_PROTOCOL_ERROR", "Unexpected worker RPC response")
            if "error" in message:
                error = message["error"]["data"]
                raise ContractError(error["code"], "Worker rejected operation")
            return message["result"]


def _outcome(initialization, params, status, code, message):
    identity = initialization["identity"]
    result = {"schema_version": "1.0.0", "attempt_id": params["request"]["attempt_id"],
              "plugin_version": identity["version"], "status": status, "summary": message,
              "output": None, "artifact_refs": [], "error": failure(code, message)}
    if identity["contribution"]["kind"] == "agent_backend":
        result.update(backend_ref=identity["contribution"]["id"], session_ref=None, usage=None)
    else:
        result["contribution_ref"] = identity["contribution"]["id"]
    return result


def execution_binding(request):
    """Return exact inputs for fingerprinting; raw inputs are not persisted."""
    return {"policy_digest": request.policy.digest, "argv": list(request.argv),
            "working_directory": request.working_directory,
            "timeout_seconds": request.timeout_seconds,
            "environment": request.environment.to_document()}


def run_plugin(runtime, execution_request, initialization, params, store, *, cancel=None,
               gateway=None, artifact_validator=None, pause=None):
    """Run a resolved, trusted contribution in an approved runtime image.

    No interpreter discovery, host import or unrestricted subprocess fallback.
    The caller obtains initialization identity from the registry run lock and
    supplies an approved argv/image and effective execution policy.
    """
    validate_contract("plugin-initialize", initialization)
    validate_contract("plugin-execute", params)
    timeout = execution_request.timeout_seconds
    requested_timeout = params["request"]["limits"].get("wall_time_seconds")
    if timeout is None or requested_timeout is not None and timeout > requested_timeout:
        raise ContractError("POLICY_UNSATISFIED", "Runtime must enforce the requested wall-clock deadline")
    channel = StdioChannel()
    request = replace(execution_request, channel=channel)
    attempt = params["request"]["attempt_id"]
    if not store.claim(initialization, params, runtime_binding=execution_binding(request)):
        prior = store.snapshot(attempt)["result"]
        if prior is None:
            raise ContractError("EXECUTION_STATE_UNKNOWN", "Attempt already exists; reconcile its runtime before retrying")
        return prior
    cancel = cancel if cancel is not None else threading.Event()
    now = pause.clock.now if pause else time.monotonic
    client = WorkerClient(channel, initialization, params, store, now() + timeout,
                          cancel, gateway, artifact_validator, pause)
    handle, started, result, clean, execution_sent = None, False, None, False, False
    try:
        client._check()
        handle = runtime.prepare(request)
        handle = runtime.start(handle)
        started = True
        ready = client.rpc("initialize", initialization)
        if ready.get("protocol_version") != "1.0.0" or ready.get("identity") != initialization["identity"]:
            raise ContractError("WORKER_PROTOCOL_ERROR", "Worker handshake identity/version mismatch")
        execution_sent = True
        accepted = client.rpc("execute", params)
        if accepted != {"execution_id": params["context"]["execution_id"], "attempt_id": attempt}:
            raise ContractError("WORKER_PROTOCOL_ERROR", "Worker accepted a different execution")
        while True:
            snapshot = client.rpc("get_status", {"attempt_id": attempt})
            if (set(snapshot) != {"attempt_id", "status", "result"} or snapshot["attempt_id"] != attempt
                    or snapshot["status"] not in ("running", "cancelling", "completed", "failed", "timed_out", "unknown")):
                raise ContractError("WORKER_PROTOCOL_ERROR", "Invalid execution status snapshot")
            if snapshot["result"] is not None:
                result = snapshot["result"]
                client.validate_result(result)
                if snapshot["status"] != result["status"] or client.terminal is None or client.terminal["payload"]["result"] != result:
                    raise ContractError("WORKER_PROTOCOL_ERROR", "Snapshot and terminal event disagree")
                break
            cancel.wait(0.02)
        client.rpc("shutdown", {})
    except _Interrupted as exc:
        status = "timed_out" if exc.timed_out else "cancelled"
        result = _outcome(initialization, params, status, "EXECUTION_TIMEOUT" if exc.timed_out else "EXECUTION_CANCELLED",
                          "Worker deadline exceeded" if exc.timed_out else "Execution cancelled by host")
        if execution_sent and initialization["execution_scope"] == "remote_job":
            result = _outcome(initialization, params, "unknown", "EXECUTION_STATE_UNKNOWN", "Remote execution needs cancellation reconciliation")
    except (ContractError, RuntimePolicyError) as exc:
        code = exc.code if isinstance(exc, ContractError) else (
            "WORKER_CRASHED" if exc.code == "protocol_closed" else "WORKER_PROTOCOL_ERROR")
        if not started and isinstance(exc, RuntimePolicyError):
            code = "POLICY_UNSATISFIED"
        result = _outcome(initialization, params, "failed", code, "Plugin worker execution failed")
        if code == 'EXECUTION_CANCELLED' and cancel.is_set():
            result = _outcome(initialization, params, 'cancelled', code, 'Execution cancelled by host')
        if execution_sent and initialization["execution_scope"] == "remote_job":
            result = _outcome(initialization, params, "unknown", "EXECUTION_STATE_UNKNOWN", "Remote execution state could not be confirmed")
    finally:
        if gateway is not None:
            gateway.close()
        try:
            if handle is not None:
                if started:
                    # Even a valid terminal result does not attest child exit.
                    runtime.stop(handle, grace_seconds=0.2)
                    state = runtime.wait(handle)
                    if state.get("state") not in ("exited", "cleaned") or state.get("reason") == "termination_unconfirmed":
                        raise RuntimePolicyError("termination_unconfirmed", "Runtime could not confirm process-tree exit.")
                runtime.cleanup(handle)
            clean = True
        except (RuntimePolicyError, OSError):
            clean = False
    if not clean:
        result = _outcome(initialization, params, "unknown", "EXECUTION_STATE_UNKNOWN", "Runtime termination needs reconciliation")
    if gateway is not None and getattr(gateway, 'uncertain', False):
        result = _outcome(initialization, params, 'unknown', 'EXECUTION_STATE_UNKNOWN',
                          'A scoped service outcome or its cleanup needs reconciliation')
    if result is None:
        result = _outcome(initialization, params, "unknown", "EXECUTION_STATE_UNKNOWN", "Execution outcome needs reconciliation")
    terminal = client.terminal
    if terminal is None or terminal["payload"]["result"] != result:
        terminal = {"schema_version": "1.0.0", "event_id": uuid4().hex, "attempt_id": attempt,
                    "sequence": store.last_sequence(attempt) + 1,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "type": "completed" if result["status"] == "completed" else "cancelled" if result["status"] == "cancelled" else "failed",
                    "payload": {"result": result}}
    store.finish(result, terminal)
    return result
