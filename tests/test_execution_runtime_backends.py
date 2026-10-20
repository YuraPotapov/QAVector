"""The same contract on both OSes; verified fake features are not OS evidence."""

import importlib
import json
import os
import subprocess
import sys

import pytest

from execution_runtime import PolicyLayer, PolicyPatch, PolicyResolver, RuntimePolicyError
from execution_runtime._json import Snapshot
from execution_runtime.backends.base import (Capability, CapabilitySet, ExecutionRequest,
                                             RuntimeBackend)
from execution_runtime.backends.registry import BackendRegistry, PROVIDERS
from execution_runtime.runtime import ExecutionRuntime


def effective(platform):
    return PolicyResolver().resolve([PolicyLayer("platform", PolicyPatch({}))],
                                    platform=platform, provider=platform + "-isolated")


class FixtureBackend(RuntimeBackend):
    """A portable stand-in exercises policy negotiation and the client contract."""

    def __init__(self, platform, missing=()):
        self.platform = platform
        self.provider = platform + "-isolated"
        self.missing = missing
        self.calls = []

    def capabilities(self):
        required = ("runtime_execution",) + effective(self.platform).policy.required_capabilities()
        return CapabilitySet(self.provider, self.platform,
                             tuple(Capability(name, "verified", "fixture only") for name in required if name not in self.missing),
                             Snapshot({"fixture_only": True}))

    def prepare(self, request):
        if not self.preflight(request.policy).ready:
            raise RuntimePolicyError("unsupported_guarantee", "Missing required guarantee.")
        self.calls.append("prepare")
        return request.policy.digest

    def start(self, prepared):
        self.calls.append("start")
        return {"instance_id": "fixture", "policy_digest": prepared}

    def stop(self, handle, deadline):
        self.calls.append("stop")

    def wait(self, handle):
        self.calls.append("wait")
        return {"exit_code": 0}

    def inspect(self, handle):
        self.calls.append("inspect")
        return {"state": "fixture"}

    def cleanup(self, handle):
        self.calls.append("cleanup")

    def recover(self):
        self.calls.append("recover")
        return {"recovered": ["fixture"], "active": [], "retained": []}

    def shutdown(self, deadline):
        self.calls.append("shutdown")


class PortableCustomBackend(FixtureBackend):
    def __init__(self):
        super().__init__("linux")
        self.provider = "fixture-isolated"


class AuditedFixtureBackend(FixtureBackend):
    def __init__(self):
        super().__init__("linux")
        self.records = []
        self.audit_failure = ""

    def record_event(self, record):
        if self.audit_failure in (record["kind"], "all"):
            raise RuntimePolicyError("runtime_audit_unavailable", "Cannot persist audit.")
        self.records.append(record)


def test_missing_mandatory_audit_prevents_preparation_and_launch():
    backend = AuditedFixtureBackend()
    backend.audit_failure = "all"
    with pytest.raises(RuntimePolicyError) as exc:
        ExecutionRuntime(backend, audit=True).prepare(ExecutionRequest(effective("linux"), ("tool",), "/"))
    assert exc.value.code == "runtime_audit_unavailable" and backend.calls == []


def test_failure_to_record_a_started_instance_stops_and_cleans_it_before_raising():
    backend = AuditedFixtureBackend()
    runtime = ExecutionRuntime(backend, audit=True)
    prepared = runtime.prepare(ExecutionRequest(effective("linux"), ("tool",), "/"))
    backend.audit_failure = "runtime.started"
    with pytest.raises(RuntimePolicyError):
        runtime.start(prepared)
    assert backend.calls == ["prepare", "start", "stop", "wait", "cleanup"]


def test_audit_failure_never_prevents_stop_or_cleanup():
    backend = AuditedFixtureBackend()
    runtime = ExecutionRuntime(backend, audit=True)
    request = ExecutionRequest(effective("linux"), ("tool",), "/")
    handle = runtime.start(runtime.prepare(request))
    backend.audit_failure = "all"
    with pytest.raises(RuntimePolicyError):
        runtime.stop(handle)
    with pytest.raises(RuntimePolicyError):
        runtime.cleanup(handle)
    assert backend.calls[-2:] == ["stop", "cleanup"]


def test_rejected_launch_audit_contains_resolved_policy_but_no_payload_or_error_message():
    backend = AuditedFixtureBackend()
    events = []
    runtime = ExecutionRuntime(backend, audit=True, event_sink=lambda kind, **fields: events.append(dict(fields, kind=kind)))
    request = ExecutionRequest(effective("linux"), ("tool", "sensitive-argument"), "/")
    def denied(request):
        raise RuntimePolicyError("fixture_denied", "sensitive-error-message")
    backend.prepare = denied
    with pytest.raises(RuntimePolicyError):
        runtime.prepare(request)
    record = backend.records[-1]
    assert record["kind"] == "runtime.rejected" and record["operation"] == "prepare"
    assert record["effective_policy"] == request.policy.to_document()
    assert "sensitive" not in json.dumps(backend.records)
    assert events == backend.records


@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_shared_client_uses_the_same_lifecycle_contract(platform):
    backend = FixtureBackend(platform)
    request = ExecutionRequest(effective(platform), ["fixture-executable"], "logical-workspace")
    prepared = backend.prepare(request)
    handle = backend.start(prepared)
    backend.inspect(handle)
    backend.stop(handle, 10)
    assert backend.wait(handle) == {"exit_code": 0}
    backend.cleanup(handle)
    assert backend.calls == ["prepare", "start", "inspect", "stop", "wait", "cleanup"]
    assert handle["policy_digest"] == request.policy.digest
    assert request.argv == ("fixture-executable",)


@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_shared_runtime_restart_finishes_cleanup_before_starting_replacement(platform):
    backend = FixtureBackend(platform)
    runtime = ExecutionRuntime(backend)
    request = ExecutionRequest(effective(platform), ("fixture",), "logical-workspace")
    handle = runtime.start(runtime.prepare(request))
    backend.calls.clear()
    runtime.restart(handle, request, grace_seconds=0)
    assert backend.calls == ["stop", "wait", "cleanup", "prepare", "start"]


@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_shared_recovery_and_shutdown_do_not_need_native_process_objects(platform):
    backend = FixtureBackend(platform)
    runtime = ExecutionRuntime(backend)
    assert runtime.recover()["recovered"] == ["fixture"]
    runtime.shutdown(grace_seconds=0)
    assert backend.calls == ["recover", "shutdown"]


@pytest.mark.parametrize("grace", [-1, float("nan"), float("inf"), True, "1", 61])
def test_shared_runtime_rejects_unbounded_or_invalid_stop_grace(grace):
    backend = FixtureBackend("windows")
    with pytest.raises(RuntimePolicyError) as exc:
        ExecutionRuntime(backend).stop("handle", grace)
    assert exc.value.code == "invalid_deadline"
    assert not backend.calls


def test_explicit_environment_is_immutable_and_must_be_allowed_by_policy():
    policy = PolicyResolver().resolve([PolicyLayer("platform", PolicyPatch({"environment": ["VISIBLE"]}))],
                                    platform="windows", provider="windows-isolated")
    values = {"VISIBLE": "original"}
    request = ExecutionRequest(policy, ("fixture",), "logical-workspace", environment=values)
    values["VISIBLE"] = "replacement"
    assert request.environment.to_document() == {"VISIBLE": "original"}
    assert "original" not in repr(request)
    with pytest.raises(RuntimePolicyError) as exc:
        ExecutionRequest(policy, ("fixture",), "logical-workspace", environment={"HOST_SECRET": "synthetic"})
    assert exc.value.code == "environment_denied"


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "1"])
def test_execution_request_rejects_invalid_timeouts(timeout):
    with pytest.raises(RuntimePolicyError):
        ExecutionRequest(effective("linux"), ("fixture",), "/", timeout_seconds=timeout)


@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_unconfigured_native_backends_never_claim_enforcement_or_start(platform, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Stage 1 diagnostics must not spawn a process")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    backend = BackendRegistry().select(host=platform)
    policy = effective(platform)
    report = backend.preflight(policy)
    assert not report.ready
    assert report.policy_digest == policy.digest
    assert any(one.name == "runtime_execution" and one.status == "unsupported" for one in report.checks)
    for call in (lambda: backend.prepare(ExecutionRequest(policy, ("unused",), "workspace")),
                 lambda: backend.start(None)):
        with pytest.raises(RuntimePolicyError) as exc:
            call()
        assert exc.value.code in ("runtime_not_configured", "backend_not_implemented", "invalid_handle")


def test_missing_feature_fails_negotiation_instead_of_downgrading_policy():
    backend = FixtureBackend("windows", missing=("network_none",))
    policy = effective("windows")
    report = backend.preflight(policy)
    assert not report.ready
    assert next(one for one in report.checks if one.name == "network_none").status == "unsupported"
    with pytest.raises(RuntimePolicyError):
        backend.prepare(ExecutionRequest(policy, ("unused",), "workspace"))
    assert backend.calls == []


def test_another_provider_needs_only_registration_not_changes_to_shared_policy(monkeypatch):
    monkeypatch.setitem(PROVIDERS, "fixture-isolated", ("linux", __name__, "PortableCustomBackend"))
    backend = BackendRegistry().select("fixture-isolated", host="linux")
    policy = PolicyResolver().resolve([PolicyLayer("platform", PolicyPatch({}))],
                                      platform=backend.platform, provider=backend.provider)
    assert backend.preflight(policy).ready
    assert policy.provider == "fixture-isolated"


@pytest.mark.parametrize("provider,host,code", [
    ("windows-isolated", "linux", "provider_platform_mismatch"),
    ("linux-isolated", "windows", "provider_platform_mismatch"),
    ("trusted-host", "linux", "unknown_provider"),
    ("auto", "darwin", "unsupported_platform"),
    ("unknown", "windows", "unknown_provider"),
])
def test_selection_never_falls_back_to_unrestricted_execution(provider, host, code):
    with pytest.raises(RuntimePolicyError) as exc:
        BackendRegistry().select(provider, host=host)
    assert exc.value.code == code


def test_unrestricted_provider_remains_unavailable_without_an_authorized_profile(monkeypatch):
    from execution_runtime.backends.native import NativeProcessBackend
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("No unrestricted launch is authorized"))
    backend = NativeProcessBackend("linux")
    assert backend.capabilities().features[0].status == "unsupported"
    with pytest.raises(RuntimePolicyError) as exc:
        backend.start(None)
    assert exc.value.code == "backend_not_implemented"


def test_backend_identity_and_contract_version_are_bound_to_preflight():
    backend = FixtureBackend("windows")
    with pytest.raises(RuntimePolicyError) as exc:
        backend.preflight(effective("linux"))
    assert exc.value.code == "provider_platform_mismatch"

    class WrongVersion(FixtureBackend):
        def capabilities(self):
            original = super().capabilities()
            return CapabilitySet(self.provider, self.platform, original.features, original.observations, 99)
    with pytest.raises(RuntimePolicyError) as exc:
        WrongVersion("windows").preflight(effective("windows"))
    assert exc.value.code == "backend_contract_mismatch"


@pytest.mark.parametrize("argv,cwd", [("shell", "workspace"), ([""], "workspace"),
                                     (["valid"], ""), (["valid"], {"unexpected": "value"}),
                                     (["valid", "bad\0argument"], "workspace")])
def test_execution_request_does_not_accept_shell_string_or_invalid_working_directory(argv, cwd):
    with pytest.raises(RuntimePolicyError):
        ExecutionRequest(effective("linux"), argv, cwd)


def test_windows_backend_does_not_probe_linux_or_import_windows_dlls(monkeypatch):
    from pathlib import Path
    def forbidden(*args, **kwargs):
        pytest.fail("Windows extension point must not probe POSIX files")
    monkeypatch.setattr(Path, "read_text", forbidden)
    report = BackendRegistry().select(host="windows").preflight(effective("windows"))
    assert report.capabilities.observations.to_document()["native_probes_performed"] is False


def test_common_import_is_lazy_in_a_fresh_interpreter():
    completed = subprocess.run([sys.executable, "-c",
        "import json,sys; import execution_runtime; import execution_runtime.backends.registry; "
        "print(json.dumps([x for x in sys.modules if x.startswith(('execution_runtime.backends.linux',"
        "'execution_runtime.backends.windows','PySide6'))]))"], capture_output=True, text=True, check=True)
    assert json.loads(completed.stdout) == []


def test_native_selection_loads_only_the_requested_provider(monkeypatch):
    calls = []
    original = importlib.import_module
    def load(name):
        calls.append(name)
        return original(name)
    monkeypatch.setattr("execution_runtime.backends.registry.importlib.import_module", load)
    assert BackendRegistry().select(host="windows").platform == "windows"
    assert calls == ["execution_runtime.backends.windows"]
