"""Core-owned attached service; the GUI never constructs an OS sandbox command."""

import codecs
import json
import math
import os
from pathlib import Path
import signal
import sys
import threading

from ._json import MAX_DOCUMENT_BYTES, object_fields, parse_document
from .backends.base import ExecutionRequest
from .deployments import DeploymentRegistry
from .errors import RuntimePolicyError
from .registry import EnvironmentRegistry, revision_name
from .runtime import ExecutionRuntime

PROTOCOL_VERSION = 1


def service_request(packet):
    object_fields(packet, ("protocol_version", "service", "runner_type", "project_directory", "argv",
                          "working_directory", "environment", "overrides", "secret_values", "stop_grace_seconds", "control_paths"),
                  "service_request", ("protocol_version", "service", "runner_type", "project_directory", "argv"))
    if type(packet["protocol_version"]) is not int or packet["protocol_version"] != PROTOCOL_VERSION:
        raise RuntimePolicyError("unsupported_service_protocol", "Unsupported service control protocol.")
    if packet["runner_type"] not in ("python", "shell"):
        raise RuntimePolicyError("unsupported_service_type", "Only attached Python/shell services support this runtime integration.")
    if (not isinstance(packet["service"], str) or not packet["service"] or len(packet["service"]) > 256
            or "\0" in packet["service"]):
        raise RuntimePolicyError("invalid_service_identity", "Expected a bounded service name.")
    grace = packet.get("stop_grace_seconds", 8)
    if type(grace) not in (int, float) or not math.isfinite(grace) or not 0 <= grace <= 60:
        raise RuntimePolicyError("unsupported_stop_grace", "Isolated services require a bounded stop grace of 0..60 seconds.")
    paths = packet.get("control_paths", [])
    if not isinstance(paths, list) or len(paths) > 64 or any(
            not isinstance(one, str) or not one or "\0" in one or len(one) > 4096 for one in paths):
        raise RuntimePolicyError("invalid_control_paths", "Control-plane protection paths must be explicit bounded strings.")
    return packet


class _Output:
    def __init__(self, emit):
        self.emit = emit
        self.offsets = [0, 0]
        self.decoders = [codecs.getincrementaldecoder("utf-8")("replace") for _ in range(2)]

    def drain(self, result, final=False):
        for index, name in enumerate(("stdout.log", "stderr.log")):
            path = next((Path(one) for one in result["artifacts"] if Path(one).name == name), None)
            if path is None:
                continue
            try:
                with path.open("rb") as stream:
                    stream.seek(self.offsets[index])
                    while True:
                        block = stream.read(16384)
                        if not block:
                            break
                        self.offsets[index] += len(block)
                        text = self.decoders[index].decode(block)
                        if text:
                            self.emit("runtime.service.output", stream="stdout" if index == 0 else "stderr", text=text)
            except FileNotFoundError:
                pass
            if final:
                text = self.decoders[index].decode(b"", final=True)
                if text:
                    self.emit("runtime.service.output", stream="stdout" if index == 0 else "stderr", text=text)


class RuntimeService:
    def __init__(self, deployments=None, profiles=None, emit=None, backend_factory=None):
        self.deployments = deployments or DeploymentRegistry()
        self.profiles = profiles or EnvironmentRegistry()
        self.emit = emit or (lambda kind, **fields: None)
        self.backend_factory = backend_factory

    def preview(self, name, revision, packet):
        object_fields(packet, ("project_directory", "overrides", "check_enforcement", "cycle_overrides", "policy_level", "worker"), "service_policy", ("project_directory",))
        if packet.get("worker") not in (None, "agent.review", "git.checkout", "command.shell"):
            raise RuntimePolicyError("invalid_request", "Unknown policy preview worker.")
        if packet.get("policy_level", "node") not in ("node", "cycle"):
            raise RuntimePolicyError("invalid_request", "Unknown policy preview level.")
        if type(packet.get("check_enforcement", False)) is not bool:
            raise RuntimePolicyError("invalid_request", "check_enforcement must be boolean.")
        deployment = self.deployments.load(name, revision_name(revision))
        protected = (str(self.deployments.directory), str(self.profiles.directory))
        backend = (self.backend_factory(deployment, protected) if self.backend_factory else deployment.backend(protected))
        inherited = deployment.resolve(backend, packet["project_directory"], profiles=self.profiles,
                                       cycle_overrides=packet.get("cycle_overrides"))
        from .workers import worker_status
        result = {"ok": True, "enforcement": "not_evaluated", "inherited_policy": inherited.to_document(),
                  "workers": worker_status(deployment, backend)}
        try:
            effective = deployment.resolve(backend, packet["project_directory"],
                None if packet.get("policy_level") == "cycle" else packet.get("overrides"), self.profiles,
                cycle_overrides=packet.get("overrides", {}) if packet.get("policy_level") == "cycle" else packet.get("cycle_overrides"))
            result.update(policy_digest=effective.digest, effective_policy=effective.to_document())
        except RuntimePolicyError as exc:
            result.update(ok=False, problems=[exc.to_document()])
            effective = inherited
        if packet.get("check_enforcement", False):
            config = deployment.to_document()["spec"].get("workers", {}).get("agent.review", {})
            if config.get("transport") == "claude_cli" and (packet.get("worker") == "agent.review" or packet.get("policy_level") == "cycle"):
                from .gateway import ProviderGateway
                route = ProviderGateway(deployment.to_document()["spec"]["provider_gateways"][config["gateway"]])
                try:
                    report = backend.gateway_preflight(effective, route)
                except RuntimePolicyError as exc:
                    result.update(ok=False, problems=result.get("problems", []) + [exc.to_document()])
                    report = backend.preflight(effective)
            else:
                report = backend.preflight(effective)
            result.update(preflight=report.to_document(), enforcement="verified" if report.ready else "unavailable")
        return result

    def _runtime_event(self, kind, **fields):
        self.emit(kind, **{key: value for key, value in fields.items()
                          if key in ("event_id", "instance_id", "provider", "policy_digest", "code", "operation", "reason", "state", "exit_code")})

    def run(self, deployment_name, revision, packet, stopped=None):
        runtime, handle, scope, result = None, None, None, None
        cleaned, failed = True, False
        stopped = stopped or threading.Event()
        output = _Output(self.emit)
        try:
            revision_name(revision)
            packet = service_request(packet)
            deployment = self.deployments.load(deployment_name, revision)
            import runtime_paths
            protected = (str(self.deployments.directory), str(self.profiles.directory),
                str(Path(runtime_paths.user_data_root()) / "services.json"),
                str(Path.home() / runtime_paths.USER_DIR_NAME / "services.json")) + tuple(packet.get("control_paths", []))
            backend = (self.backend_factory(deployment, protected) if self.backend_factory
                       else deployment.backend(protected))
            runtime = ExecutionRuntime(backend, audit=True, event_sink=self._runtime_event)
            try:
                effective = deployment.resolve(backend, packet["project_directory"], packet.get("overrides"), self.profiles)
                request = ExecutionRequest(effective, packet["argv"],
                    packet.get("working_directory") or deployment.to_document()["spec"]["working_directory"],
                    environment=packet.get("environment", {}), secret_values=packet.get("secret_values", {}))
            except RuntimePolicyError as exc:
                runtime._event("runtime.rejected", code=exc.code, operation="resolve_service")
                raise
            project_source = next(one["source"] for one in effective.to_document()["bindings"] if one["name"] == "project")
            scope = backend.service_scope(project_source + "\0" + packet["service"])
            with scope:
                try:
                    if stopped.is_set():
                        return 0
                    handle = runtime.prepare(request)
                    cleaned = False
                    scope.bind(handle)
                    if stopped.is_set():
                        return 0
                    handle = runtime.start(handle)
                    ready = False
                    while True:
                        result = runtime.inspect(handle)
                        output.drain(result)
                        if result["state"] in ("exited", "failed", "cleaned"):
                            break
                        if result["state"] == "running" and not ready:
                            ready = True
                            self.emit("runtime.service.ready", deployment=deployment_name, revision=revision,
                                instance_id=result["instance_id"], provider=result["provider"],
                                policy_digest=result["policy_digest"], enforcement="verified",
                                artifacts=result["artifacts"], secret_files=result.get("secret_files", []),
                                effective_policy=effective.to_document())
                        if stopped.wait(0.02):
                            runtime.stop(handle, packet.get("stop_grace_seconds", 8))
                            break
                    result = runtime.wait(handle)
                    output.drain(result, final=True)
                    failed = (not stopped.is_set() and (result["exit_code"] != 0 or result["reason"] != "exited"))
                    return 1 if failed else 0
                finally:
                    if handle is not None:
                        state = runtime.inspect(handle)["state"]
                        try:
                            if state not in ("prepared", "cleaned", "exited", "failed"):
                                runtime.stop(handle, 0)
                        finally:
                            runtime.cleanup(handle)
                            cleaned = True
                            scope.clear()
        except (OSError, RuntimePolicyError) as exc:
            failed = True
            problem = exc.to_document() if isinstance(exc, RuntimePolicyError) else {
                "code": "runtime_service_io_error", "message": "The isolated service supervisor could not complete safely."}
            self.emit("runtime.service.error", problem=problem)
            return 1
        finally:
            self.emit("runtime.service.end", ok=not failed, cleanup_confirmed=cleaned,
                exit_code=result["exit_code"] if result else None, reason=result["reason"] if result else "not_started")


class _Input:
    """Bounded raw pipe reads: daemon control must not hold Python stdin locks at exit."""

    def __init__(self, fd):
        self.fd = fd
        self.pending = b""

    def readline(self, limit):
        while True:
            end = self.pending.find(b"\n")
            if end >= 0:
                if end + 1 > limit:
                    raise RuntimePolicyError("document_too_large", "Service input exceeds its protocol bound.")
                line, self.pending = self.pending[:end + 1], self.pending[end + 1:]
                return line
            if len(self.pending) >= limit:
                raise RuntimePolicyError("document_too_large", "Service input exceeds its protocol bound.")
            block = os.read(self.fd, min(65536, limit - len(self.pending)))
            if not block:
                line, self.pending = self.pending, b""
                return line
            self.pending += block


class _Control(threading.Thread):
    def __init__(self, stream):
        super().__init__(daemon=True)
        self.stream = stream
        self.stopped = threading.Event()

    def run(self):
        try:
            while True:
                raw = self.stream.readline(8193)
                if not raw or len(raw) > 8192:
                    return
                packet = parse_document(raw)
                object_fields(packet, ("op",), "service_control", ("op",))
                if packet["op"] != "stop":
                    return
                return
        except (OSError, RuntimePolicyError):
            pass
        finally:
            self.stopped.set()


def main(argv=None):
    from engine import events
    from .cli import _Parser
    parser = _Parser(description="Supervised isolated service (JSONL stdin control and stdout events).")
    commands = parser.add_mutually_exclusive_group(required=True)
    commands.add_argument("--runtime-service")
    commands.add_argument("--runtime-service-policy")
    parser.add_argument("--runtime-service-revision", required=True)
    parser.add_argument("--runtime-deployments-dir")
    parser.add_argument("--runtime-profiles-dir")
    events.configure("-")
    def emit(kind, **fields):
        events.emit(kind, protocol_version=PROTOCOL_VERSION, **fields)
    try:
        arguments = list(sys.argv[1:] if argv is None else argv)
        flags = [one.split("=", 1)[0] for one in arguments if one.startswith("--")]
        if len(flags) != len(set(flags)):
            raise RuntimePolicyError("invalid_arguments", "Service options may only be supplied once.")
        options = parser.parse_args(arguments)
        incoming = _Input(sys.stdin.fileno())
        raw = incoming.readline(MAX_DOCUMENT_BYTES + 1)
        packet = parse_document(raw)
        if options.runtime_service_policy:
            from .cli import emit as json_emit
            try:
                result = RuntimeService(DeploymentRegistry(options.runtime_deployments_dir),
                    EnvironmentRegistry(options.runtime_profiles_dir)).preview(
                    options.runtime_service_policy, options.runtime_service_revision, packet)
                return json_emit(0 if result["ok"] else 2, result)
            except RuntimePolicyError as exc:
                return json_emit(2, {"ok": False, "problems": [exc.to_document()]})
        control = _Control(incoming)
        control.start()
        for number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(number, lambda *_: control.stopped.set())
        return RuntimeService(DeploymentRegistry(options.runtime_deployments_dir),
            EnvironmentRegistry(options.runtime_profiles_dir), emit=emit).run(
            options.runtime_service, options.runtime_service_revision, packet, control.stopped)
    except (OSError, RuntimePolicyError) as exc:
        emit("runtime.service.error", problem=exc.to_document() if isinstance(exc, RuntimePolicyError)
             else {"code": "runtime_service_io_error", "message": "Cannot initialize the isolated service supervisor."})
        emit("runtime.service.end", ok=False, cleanup_confirmed=True, reason="not_started", exit_code=None)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
