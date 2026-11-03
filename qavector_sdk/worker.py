"""One contribution/attempt per process; JSON-RPC stdio server.

Run only inside an approved Execution Runtime environment. This module does
not select an interpreter, spawn a subprocess or grant host capabilities.
"""

import asyncio
import contextvars
import importlib
import json
import os
import sys
from datetime import datetime, timezone
from uuid import uuid4

from .ipc import MAX_FRAME_BYTES, decode, encode, error_response, failure
from .validation import ContractError, validate_contract, validate_output, validate_schema
from .preflight import preflight
from .event_loop import run as run_event_loop
from .versions import satisfies

_services = contextvars.ContextVar("qavector_worker_services")
MAX_EVENTS = 4096


class _MethodNotFound(ContractError):
    pass


def require_worker_context(context):
    """Guard worker-only transports against accidental use in the host process.

    This is an API guard, not a sandbox; Execution Runtime supplies isolation.
    """
    server = _services.get(None)
    if server is None or server.initialized is None or (
            server.context is not None and server.context["execution_id"] != context["execution_id"]):
        raise ContractError("POLICY_UNSATISFIED", "This transport requires an initialized SDK worker")


async def host_call(handle, operation, arguments):
    """Call a scoped service; backend authors do not implement IPC themselves."""
    server = _services.get()
    if server.attempt is None or server.status not in ("running", "waiting_input"):
        raise ContractError("POLICY_UNSATISFIED", "No active execution scope")
    identifier = "host-" + uuid4().hex
    future = asyncio.get_running_loop().create_future()
    if len(server.callbacks) >= 16:
        raise ContractError("WORKER_PROTOCOL_ERROR", "Too many pending host callbacks")
    server.callbacks[identifier] = future
    params = {"execution_id": server.context["execution_id"], "attempt_id": server.attempt,
              "handle": handle, "operation": operation, "arguments": arguments}
    validate_contract("plugin-host-call", params)
    try:
        server.send({"jsonrpc": "2.0", "id": identifier, "method": "host.call", "params": params})
        # Runtime owns the finite active-time deadline. A worker-local asyncio
        # timer would include SIGSTOP time and expire immediately on Continue.
        return await future
    finally:
        server.callbacks.pop(identifier, None)


class WorkerServer:
    def __init__(self, source, destination):
        self.source, self.destination = source, destination
        self.initialized = None
        self.backend = None
        self.attempt = None
        self.context = None
        self.execute_params = None
        self.status = "pending"
        self.result = None
        self.task = None
        self.sequence = -1
        self.event_ids = {}
        self.callbacks = {}
        self.stopping = False
        self._input_buffer = b""

    def send(self, message):
        self.destination.write(encode(message))
        self.destination.flush()

    def _backend(self):
        if self.backend is None:
            module, attribute = self.initialized["identity"]["contribution"]["entrypoint"].split(":")
            factory = importlib.import_module(module)
            for part in attribute.split("."):
                factory = getattr(factory, part)
            self.backend = factory()
        return self.backend

    @property
    def kind(self):
        return "agent" if self.initialized["identity"]["contribution"]["kind"] == "agent_backend" else "node"

    async def emit(self, event):
        validate_contract(self.kind + "-event", event)
        if event["attempt_id"] != self.attempt or self.result is not None:
            raise ContractError("WORKER_PROTOCOL_ERROR", "Event is outside the active attempt")
        if event["type"] in ("completed", "failed", "cancelled", "input.required"):
            raise ContractError("CAPABILITY_UNSUPPORTED", "Terminal events belong to the SDK; interactive input is unavailable")
        prior = self.event_ids.get(event["event_id"])
        if prior is not None:
            if prior != event:
                raise ContractError("WORKER_PROTOCOL_ERROR", "Conflicting duplicate event ID")
            return
        if event["sequence"] <= self.sequence or len(self.event_ids) >= MAX_EVENTS:
            raise ContractError("WORKER_PROTOCOL_ERROR", "Event ordering or count limit violated")
        # Snapshot because a provider may reuse/mutate its event dictionaries.
        self.event_ids[event["event_id"]] = json.loads(json.dumps(event))
        self.sequence = event["sequence"]
        self.send({"jsonrpc": "2.0", "method": "event", "params": event})

    async def _execute(self, params):
        _services.set(self)
        request = params["request"]
        identity = self.initialized["identity"]
        try:
            backend = self._backend()
            if self.kind == "agent":
                capabilities = await backend.describe(params["profile"], params["context"])
                # The v1 server has no respond_input broker yet.
                capabilities = dict(capabilities, interactive_input=False)
                preflight(params["profile"], request, capabilities, params["requirements"],
                          config_schema=self.initialized["schemas"]["config"])
            result = await backend.execute(request, params["context"], self.emit)
            validate_contract(self.kind + "-result", result)
            reference = "backend_ref" if self.kind == "agent" else "contribution_ref"
            if (result["attempt_id"] != self.attempt or result[reference] != identity["contribution"]["id"]
                    or result["plugin_version"] != identity["version"]):
                raise ContractError("WORKER_PROTOCOL_ERROR", "Result identity differs from the selected contribution")
            if result["status"] == "completed":
                validate_output(result["output"], self.initialized["schemas"]["output"])
                if request["output_schema"] is not None:
                    validate_output(result["output"], request["output_schema"])
            # Providers cannot attest that their process tree has stopped.
            if result["status"] == "cancelled":
                raise ContractError("WORKER_PROTOCOL_ERROR", "Only the host can confirm cancellation")
        except asyncio.CancelledError:
            # A cancelled coroutine is NOT a cancelled process tree. The host
            # calls Runtime.stop/wait/cleanup before publishing cancelled.
            return
        except Exception as exc:
            code = exc.code if isinstance(exc, ContractError) else "WORKER_CRASHED"
            result = self._failure(code, "Plugin execution failed contract or backend checks")
        self.result, self.status = result, result["status"]
        event = {"schema_version": "1.0.0", "event_id": uuid4().hex,
                 "attempt_id": self.attempt, "sequence": self.sequence + 1,
                 "timestamp": datetime.now(timezone.utc).isoformat(),
                 "type": "completed" if self.status == "completed" else "failed",
                 "payload": {"result": result}}
        self.send({"jsonrpc": "2.0", "method": "event", "params": event})

    def _failure(self, code, message):
        identity = self.initialized["identity"]
        result = {"schema_version": "1.0.0", "attempt_id": self.attempt,
                  "plugin_version": identity["version"], "status": "failed", "summary": message,
                  "output": None, "artifact_refs": [], "error": failure(code, message)}
        if self.kind == "agent":
            result.update(backend_ref=identity["contribution"]["id"], session_ref=None, usage=None)
        else:
            result["contribution_ref"] = identity["contribution"]["id"]
        return result

    async def dispatch(self, method, params):
        if method == "initialize":
            validate_contract("plugin-initialize", params)
            if self.initialized is not None:
                if params != self.initialized:
                    raise ContractError("WORKER_PROTOCOL_ERROR", "Worker already has a different identity")
            else:
                for schema in params["schemas"].values():
                    validate_schema(schema, code="WORKER_PROTOCOL_ERROR")
                self.initialized = params
            # No plugin import occurs during version negotiation.
            return {"protocol_version": "1.0.0", "identity": params["identity"],
                    "interactive_input": False, "max_frame_bytes": MAX_FRAME_BYTES}
        if self.initialized is None:
            raise ContractError("WORKER_PROTOCOL_ERROR", "Initialize is required before plugin operations")
        if method == "describe":
            if self.attempt is not None:
                raise ContractError("WORKER_PROTOCOL_ERROR", "Describe cannot mutate an active backend")
            if self.kind == "node":
                if params:
                    raise ContractError("WORKER_PROTOCOL_ERROR", "Node describe takes no parameters")
                return {"kind": "node_provider", "schemas": self.initialized["schemas"]}
            if set(params) != {"profile", "context"}:
                raise ContractError("WORKER_PROTOCOL_ERROR", "Describe requires a profile and context")
            validate_contract("agent-profile", params["profile"])
            validate_contract("execution-context", params["context"])
            token = _services.set(self)
            try:
                capabilities = await self._backend().describe(params["profile"], params["context"])
            finally:
                _services.reset(token)
            validate_contract("agent-capabilities", capabilities)
            return dict(capabilities, interactive_input=False)
        if method == "execute":
            validate_contract("plugin-execute", params)
            validate_contract(self.kind + "-request", params["request"])
            if self.attempt is not None:
                if params != self.execute_params:
                    raise ContractError("WORKER_PROTOCOL_ERROR", "Worker is already bound to another attempt or request")
                return {"execution_id": self.context["execution_id"], "attempt_id": self.attempt}
            if self.kind == "agent":
                validate_contract("agent-profile", params.get("profile"))
                validate_contract("agent-requirements", params.get("requirements"))
                if params["profile"]["backend_ref"] != self.initialized["identity"]["contribution"]["id"]:
                    raise ContractError("WORKER_PROTOCOL_ERROR", "Profile backend differs from worker identity")
                if not satisfies(self.initialized["identity"]["version"], params["profile"]["backend_version"]):
                    raise ContractError("PLUGIN_API_INCOMPATIBLE", "Worker version does not satisfy Agent Profile")
            else:
                validate_output(params["request"]["config"], self.initialized["schemas"]["config"], code="CONFIG_INVALID")
                validate_output(params["request"]["inputs"], self.initialized["schemas"]["input"], code="CONFIG_INVALID")
            self.execute_params = params
            self.attempt, self.context = params["request"]["attempt_id"], params["context"]
            self.status = "running"
            self.task = asyncio.create_task(self._execute(params))
            return {"execution_id": self.context["execution_id"], "attempt_id": self.attempt}
        if method in ("get_status", "cancel"):
            if set(params) != {"attempt_id"} or params["attempt_id"] != self.attempt or self.attempt is None:
                raise ContractError("WORKER_PROTOCOL_ERROR", "Unknown execution attempt")
            if method == "cancel":
                if self.result is None:
                    self.status = "cancelling"
                    self.task.cancel()
                return {"accepted": True, "attempt_id": self.attempt}
            return {"attempt_id": self.attempt, "status": self.status, "result": self.result}
        if method == "respond_input":
            validate_contract("plugin-input-response", params)
            raise ContractError("CAPABILITY_UNSUPPORTED", "Interactive input is not enabled by this worker")
        if method == "shutdown":
            if params:
                raise ContractError("WORKER_PROTOCOL_ERROR", "Shutdown takes no parameters")
            if self.task is not None and not self.task.done():
                self.task.cancel()
            self.stopping = True
            return {"accepted": True}
        raise _MethodNotFound("WORKER_PROTOCOL_ERROR", "Unknown required RPC method")

    async def _readline(self):
        # POSIX pipe readiness stays on the event-loop thread. A thread-pool
        # reader would need cross-thread socket wakeups in network-denied
        # environments and can keep interpreter shutdown waiting on stdin.
        loop = asyncio.get_running_loop()
        fd = self.source.fileno()
        while b"\n" not in self._input_buffer and len(self._input_buffer) <= MAX_FRAME_BYTES:
            ready = loop.create_future()
            def readable():
                if not ready.done():
                    ready.set_result(None)
            loop.add_reader(fd, readable)
            try:
                await ready
            finally:
                loop.remove_reader(fd)
            block = os.read(fd, min(65536, MAX_FRAME_BYTES + 1 - len(self._input_buffer)))
            if not block:
                raw, self._input_buffer = self._input_buffer, b""
                return raw
            self._input_buffer += block
        end = self._input_buffer.find(b"\n") + 1
        end = end or len(self._input_buffer)
        raw, self._input_buffer = self._input_buffer[:end], self._input_buffer[end:]
        return raw

    async def serve(self):
        while not self.stopping:
            raw = await self._readline()
            if not raw:
                break
            try:
                message = decode(raw)
            except ContractError:
                self.send(error_response(None, "WORKER_PROTOCOL_ERROR", "Invalid protocol frame", -32700))
                break
            if "method" not in message:
                callback = self.callbacks.get(message["id"])
                if callback is None or callback.done():
                    raise ContractError("WORKER_PROTOCOL_ERROR", "Unsolicited host response")
                if "error" in message:
                    error = message["error"]["data"]
                    callback.set_exception(ContractError(error["code"], "Host API call was rejected"))
                else:
                    callback.set_result(message["result"])
                continue
            if "id" not in message:
                raise ContractError("WORKER_PROTOCOL_ERROR", "Host cannot publish worker events")
            try:
                result = await self.dispatch(message["method"], message["params"])
                self.send({"jsonrpc": "2.0", "id": message["id"], "result": result})
            except Exception as exc:
                code = exc.code if isinstance(exc, ContractError) else "WORKER_CRASHED"
                rpc_code = -32601 if isinstance(exc, _MethodNotFound) else -32602 if code in ("CONTRACT_INVALID", "CONFIG_INVALID") else -32000
                self.send(error_response(message["id"], code, "Worker operation rejected", rpc_code))
        if self.task is not None and not self.task.done():
            self.task.cancel()


def main():
    source, destination = sys.stdin.buffer, sys.stdout.buffer
    # Python print() from plugin imports/execution goes to bounded runtime logs.
    # Raw writes to fd 1 still corrupt the protocol and are rejected by host.
    sys.stdout = sys.stderr
    try:
        run_event_loop(WorkerServer(source, destination).serve())
    except (ContractError, BrokenPipeError, OSError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
