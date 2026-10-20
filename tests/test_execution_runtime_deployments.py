"""Provisioning is trusted and revision-pinned; per-service overrides only narrow it."""

import json
import os

import pytest

from execution_runtime import RuntimePolicyError
from execution_runtime.deployments import DeploymentRegistry, RuntimeDeployment
from execution_runtime.service import _Input, RuntimeService, service_request
from execution_runtime.backends.base import Capability, CapabilitySet, RuntimeBackend
from execution_runtime._json import Snapshot


def document(platform="linux"):
    project, target = (("/projects/demo", "/workspace") if platform == "linux"
                       else (r"C:\Projects\Demo", r"D:\Workspace"))
    return {"api_version": "qavector.io/v1", "kind": "RuntimeDeployment", "metadata": {"name": "demo"},
        "spec": {"provider": platform + "-isolated", "policy": {"filesystem": [{"binding": "project", "access": "read_only"}]},
            "bindings": [{"name": "project", "source": project, "target": target}],
            "working_directory": target, "backend_options": {platform: {}}}}


@pytest.mark.parametrize("patch", [None, {"git.checkout": {}}, {"unknown": {}}, {"git.checkout": {
    "contract": "unknown", "entrypoint": "/worker", "argv": ["/python", "/worker"], "git": "/git"}},
    {"git.checkout": {"contract": "qavector.git-checkout/v1", "entrypoint": "/worker", "argv": ["/python", "/other"], "git": "/git"}}])
def test_worker_declarations_are_explicit_versioned_and_cannot_ignore_invalid_fields(patch):
    raw = document()
    raw["spec"]["workers"] = patch
    with pytest.raises(RuntimePolicyError):
        RuntimeDeployment(raw)


@pytest.mark.parametrize("patch", [{"contract": "unknown"}, {"provider_argv": []},
    {"provider_argv": ["/python", "/other"]}, {"provider_entrypoint": None}, {"extra": True}])
def test_agent_worker_declaration_requires_versioned_pinned_provider_prefix(patch):
    raw = document()
    raw["spec"]["workers"] = {"agent.review": dict(contract="qavector.agent-review/v1", entrypoint="/worker",
        argv=["/python", "/worker"], provider_argv=["/python", "/provider"], provider_entrypoint="/provider")}
    raw["spec"]["workers"]["agent.review"].update(patch)
    with pytest.raises(RuntimePolicyError):
        RuntimeDeployment(raw)


@pytest.mark.parametrize("credentials", [None, [], {"Bad Alias": "TOKEN"}, {"api_key": "bad secret value"},
    {"one": "TOKEN", "two": "TOKEN"}, {"key%d" % i: "TOKEN%d" % i for i in range(9)}])
def test_worker_credential_references_are_strict_and_bounded(credentials):
    raw = document()
    raw["spec"]["workers"] = {"agent.review": dict(contract="qavector.agent-review/v1", entrypoint="/worker",
        argv=["/python", "/worker"], provider_argv=["/python", "/provider"], provider_entrypoint="/provider", credentials=credentials)}
    with pytest.raises(RuntimePolicyError):
        RuntimeDeployment(raw)


@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_deployment_policy_resolution_is_portable_and_project_bound(platform):
    class Backend:
        provider = platform + "-isolated"
        def runtime_root_identity(self):
            return "sha256:" + "a" * 64
    backend = Backend()
    backend.platform = platform
    raw = document(platform)
    deployment = RuntimeDeployment(raw)
    assert deployment.resolve(backend, raw["spec"]["bindings"][0]["source"]).policy.to_document()["network"]["mode"] == "none"
    with pytest.raises(RuntimePolicyError):
        deployment.resolve(backend, raw["spec"]["bindings"][0]["source"],
                           {"filesystem": [{"binding": "project", "access": "read_write"}]})
    wrong = "/projects/other" if platform == "linux" else r"C:\Projects\Other"
    with pytest.raises(RuntimePolicyError) as exc:
        deployment.resolve(backend, wrong)
    assert exc.value.code == "service_project_mismatch"


def test_deployment_changes_require_reviewed_revision_and_invalidate_old_service_reference(tmp_path):
    registry = DeploymentRegistry(tmp_path / "deployments")
    original = registry.save(RuntimeDeployment(document()))
    raw = document(); raw["metadata"]["description"] = "changed"
    replacement = RuntimeDeployment(raw)
    with pytest.raises(RuntimePolicyError):
        registry.save(replacement)
    registry.save(replacement, original.digest)
    with pytest.raises(RuntimePolicyError) as exc:
        registry.load("demo", original.digest)
    assert exc.value.code == "deployment_revision_conflict"
    assert registry.load("demo", replacement.digest) == replacement


def test_tampered_deployment_cannot_be_reapproved_by_loading_it(tmp_path):
    registry = DeploymentRegistry(tmp_path)
    registry.save(RuntimeDeployment(document()))
    path = tmp_path / "demo.json"
    raw = json.loads(path.read_text()); raw["deployment"]["spec"]["policy"] = {"network": {"mode": "full"}}
    path.write_text(json.dumps(raw))
    with pytest.raises(RuntimePolicyError) as exc:
        registry.load("demo")
    assert exc.value.code == "deployment_integrity_failed"


@pytest.mark.parametrize("patch", [{"runner_type": "docker"}, {"stop_grace_seconds": 61},
                                   {"protocol_version": True}, {"unexpected": True}])
def test_service_control_refuses_unsupported_mode_or_protocol(patch):
    packet = {"protocol_version": 1, "runner_type": "shell", "project_directory": "/project",
              "service": "Demo", "argv": ["/bin/tool"]}
    packet.update(patch)
    with pytest.raises(RuntimePolicyError):
        service_request(packet)


def test_raw_service_input_preserves_coalesced_request_and_stop_packets():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b'{"service":"worker"}\n{"op":"stop"}\n')
        incoming = _Input(read_fd)
        assert incoming.readline(1024) == b'{"service":"worker"}\n'
        assert incoming.readline(8193) == b'{"op":"stop"}\n'
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_raw_service_input_bounds_a_packet_without_waiting_for_eof():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b'x' * 128)
        with pytest.raises(RuntimePolicyError) as exc:
            _Input(read_fd).readline(128)
        assert exc.value.code == "document_too_large"
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_service_preview_reports_inherited_permissions_and_probes_only_when_requested(tmp_path, platform):
    class Backend:
        provider = platform + "-isolated"
        contract_version = 1
        preflight = RuntimeBackend.preflight
        def runtime_root_identity(self):
            return "sha256:" + "a" * 64
        def capabilities(self):
            self.probes += 1
            return CapabilitySet(self.provider, self.platform, tuple(Capability(name, "verified", "fixture") for name in (
                "runtime_execution", "filesystem_isolation", "environment_allowlist", "network_none", "descendant_containment", "privilege_restrictions")), Snapshot({}))
    backend = Backend(); backend.platform = platform; backend.probes = 0
    registry = DeploymentRegistry(tmp_path)
    raw = document(platform)
    raw["spec"]["policy"]["filesystem"][0]["access"] = "read_write"
    deployment = registry.save(RuntimeDeployment(raw))
    service = RuntimeService(registry, backend_factory=lambda one, protected: backend)
    packet = {"project_directory": raw["spec"]["bindings"][0]["source"],
              "overrides": {"filesystem": [{"binding": "project", "access": "read_only"}]}}
    result = service.preview(deployment.name, deployment.digest, packet)
    assert result["inherited_policy"]["policy"]["filesystem"][0]["access"] == "read_write"
    assert result["effective_policy"]["policy"]["filesystem"][0]["access"] == "read_only"
    assert result["enforcement"] == "not_evaluated" and backend.probes == 0
    checked = service.preview(deployment.name, deployment.digest, dict(packet, check_enforcement=True))
    assert checked["preflight"]["ready"] and checked["enforcement"] == "verified" and backend.probes == 1


def test_invalid_preview_retains_inherited_context_for_repair_without_an_effective_policy(tmp_path):
    class Backend:
        provider, platform = "linux-isolated", "linux"
        def runtime_root_identity(self):
            return "sha256:" + "a" * 64
    registry = DeploymentRegistry(tmp_path)
    deployment = registry.save(RuntimeDeployment(document()))
    result = RuntimeService(registry, backend_factory=lambda one, protected: Backend()).preview(
        deployment.name, deployment.digest, {"project_directory": "/projects/demo", "overrides": {"network": {"mode": "full"}}})
    assert result["ok"] is False and result["problems"][0]["code"] == "policy_widening"
    assert result["inherited_policy"]["policy"]["network"]["mode"] == "none"
    assert "effective_policy" not in result
