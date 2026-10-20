"""Policy/lifecycle orchestration with a synthetic backend; native proofs live separately."""

import copy
import json
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

import pytest

from cycle import checkpoints, executor, model, registry, runtime, variables
from cycle.bus import EventObserver
from cycle.plugins.command import ShellCommand
from cycle.registry import PluginResult
from domain.cycle import CycleRun, StepRun, SUCCESS
from execution_runtime._json import Snapshot
from execution_runtime.backends.base import Capability, CapabilitySet, PreparedExecution, RuntimeBackend
from execution_runtime.deployments import DeploymentRegistry, RuntimeDeployment
from execution_runtime.registry import EnvironmentRegistry


class SyntheticBackend(RuntimeBackend):
    provider, platform = "linux-isolated", "linux"

    def __init__(self, path):
        self.path, self.calls, self.requests, self.events = path, [], [], []
        self.states = {}
        self.file_identities = {}

    def runtime_root_identity(self):
        return "sha256:" + "a" * 64

    def runtime_file_identity(self, target):
        from execution_runtime.errors import RuntimePolicyError
        if target not in self.file_identities:
            raise RuntimePolicyError("worker_file_not_pinned", "Synthetic worker artifact is not pinned.")
        return self.file_identities[target]

    def secret_file_locations(self, names):
        return {name: "/synthetic-secrets/" + name for name in names}

    def capabilities(self):
        names = ("runtime_execution", "filesystem_isolation", "network_none", "environment_allowlist", "limit_scratch_bytes", "secrets_grants")
        return CapabilitySet(self.provider, self.platform, tuple(Capability(n, "verified") for n in names), Snapshot({}))

    def record_event(self, event):
        self.events.append(copy.deepcopy(event))

    @contextmanager
    def service_scope(self, key):
        backend = self
        class Scope:
            def bind(self, handle):
                backend.calls.append("bind")
            def clear(self):
                backend.calls.append("clear")
        self.calls.append("scope")
        try:
            yield Scope()
        finally:
            self.calls.append("release")

    def prepare(self, request):
        self.calls.append("prepare")
        self.requests.append(request)
        handle = PreparedExecution("%032x" % len(self.requests), self.provider, request.policy.digest)
        self.states[handle.instance_id] = "prepared"
        directory = self.path / handle.instance_id
        directory.mkdir(parents=True)
        for name in ("stdout.log", "stderr.log"):
            (directory / name).write_text("")
        if request.argv[-1].startswith('{"base":'):
            payload = json.loads(request.argv[-1])
            outputs = {"commit": "c" * 40, "branch": "main"} if payload["operation"] == "checkout" else {}
            (directory / "stdout.log").write_text(json.dumps({"contract": payload["contract"], "status": "success", "outputs": outputs, "code": ""}))
        elif request.argv[-1].startswith('{"contract":"qavector.agent-review/v1"'):
            (directory / "stdout.log").write_text(json.dumps({"contract": "qavector.agent-review/v1", "code": "", "review": {
                "summary": "Synthetic review", "risk": "low", "issues": [], "recommendations": []}}))
        return handle

    def start(self, prepared):
        self.calls.append("start")
        self.states[prepared.instance_id] = "exited"
        return prepared

    def inspect(self, handle):
        return dict(instance_id=handle.instance_id, state=self.states[handle.instance_id], exit_code=0,
                    reason="exited", stdout_tail="", artifacts=[str(self.path / handle.instance_id / n) for n in ("stdout.log", "stderr.log")])

    def wait(self, handle):
        self.calls.append("wait")
        return self.inspect(handle)

    def stop(self, handle, deadline):
        self.calls.append("stop")
        self.states[handle.instance_id] = "exited"

    def cleanup(self, handle):
        self.calls.append("cleanup")
        self.states[handle.instance_id] = "cleaned"


@pytest.fixture
def setup_runtime(tmp_path, monkeypatch):
    raw = {"api_version": "qavector.io/v1", "kind": "RuntimeDeployment", "metadata": {"name": "demo"},
        "spec": {"provider": "linux-isolated", "policy": {"filesystem": [{"binding": "project", "access": "read_write"}],
            "environment": ["ALLOWED"]}, "bindings": [{"name": "project", "source": "/approved", "target": "/workspace"}],
            "working_directory": "/workspace", "shell": ["/approved-shell", "-c"], "backend_options": {"linux": {}}}}
    deployments, profiles = DeploymentRegistry(tmp_path / "deployments"), EnvironmentRegistry(tmp_path / "profiles")
    deployment = deployments.save(RuntimeDeployment(raw))
    backend = SyntheticBackend(tmp_path / "native")
    monkeypatch.setattr(runtime, "DeploymentRegistry", lambda: deployments)
    monkeypatch.setattr(runtime, "EnvironmentRegistry", lambda: profiles)
    monkeypatch.setattr(RuntimeDeployment, "backend", lambda self, protected=(): backend)
    config = dict(mode="isolated", deployment="demo", revision=deployment.digest, project_directory="/approved")
    return config, backend, deployments, raw


def workflow(config, *steps):
    return model.parse_cycle({"id": "runtime", "execution": {"runtime": copy.deepcopy(config)},
                              "steps": list(steps) or [node("a")]}, "runtime")


def node(name, **extra):
    return dict({"id": name, "plugin": "command.shell", "with": {"command": ["/approved-tool"]}}, **extra)


@pytest.fixture
def git_runtime(setup_runtime):
    from execution_runtime.workers import GIT_CONTRACT, git_artifact
    config, backend, deployments, raw = setup_runtime
    raw["spec"]["workers"] = {"git.checkout": {"contract": GIT_CONTRACT,
        "entrypoint": "/workers/git.py", "argv": ["/approved-python", "-I", "/workers/git.py"], "git": "/approved-git"}}
    backend.file_identities = {"/workers/git.py": git_artifact("linux")[1], "/approved-python": "b" * 64, "/approved-git": "d" * 64}
    replacement = deployments.save(RuntimeDeployment(raw), config["revision"])
    config["revision"] = replacement.digest
    return config, backend, deployments, raw


def git_node(**settings):
    return {"id": "checkout", "plugin": "git.checkout", "with": {"repository": "repo", **settings}}


@pytest.fixture
def agent_runtime(setup_runtime):
    from execution_runtime.workers import AGENT_CONTRACT, agent_artifact
    config, backend, deployments, raw = setup_runtime
    raw["spec"]["policy"]["resources"] = {"scratch_bytes": 4 * 1024 * 1024}
    raw["spec"]["workers"] = {"agent.review": {"contract": AGENT_CONTRACT,
        "entrypoint": "/workers/agent.py", "argv": ["/approved-python", "-I", "/workers/agent.py"],
        "provider_argv": ["/approved-python", "-I", "/workers/provider.py"], "provider_entrypoint": "/workers/provider.py"}}
    backend.file_identities = {"/workers/agent.py": agent_artifact("linux")[1], "/approved-python": "b" * 64, "/workers/provider.py": "d" * 64}
    replacement = deployments.save(RuntimeDeployment(raw), config["revision"])
    config["revision"] = replacement.digest
    return config, backend, deployments, raw


def agent_node(**settings):
    return {"id": "review", "plugin": "agent.review", "runtime": {"overrides": {
        "filesystem": [{"binding": "project", "access": "read_only"}]}},
        "with": {"framework": "runtime", "model": "local-fixture", "task": "Review local evidence.", "repository": "src", **settings}}


def test_saved_gateway_profile_does_not_enable_unisolated_cli_or_host_sign_in(agent_runtime, tmp_path, monkeypatch):
    from execution_runtime.gateway import CONTRACT
    config, backend, deployments, raw = agent_runtime
    raw["spec"]["provider_gateways"] = {"claude": {"contract": CONTRACT, "provider": "claude_cli",
        "endpoint": "https://api.anthropic.com/v1/messages", "addresses": ["1.1.1.1"], "models": ["sonnet"]}}
    replacement = deployments.save(RuntimeDeployment(raw), config["revision"])
    config["revision"] = replacement.digest
    monkeypatch.setattr("cycle.plugins.agent_cli.auth_status", lambda *_a, **_k: pytest.fail("No host sign-in access"))
    with pytest.raises(checkpoints.CheckpointError, match="framework runtime"):
        executor.run_cycle(workflow(config, agent_node(framework="claude_cli")), str(tmp_path / "run"))
    assert backend.requests == []


@pytest.fixture
def credential_runtime(agent_runtime):
    config, backend, deployments, raw = agent_runtime
    raw["spec"]["workers"]["agent.review"]["credentials"] = {"api_key": "PROVIDER_TOKEN"}
    raw["spec"]["policy"]["secrets"] = ["PROVIDER_TOKEN", "OTHER_TOKEN"]
    replacement = deployments.save(RuntimeDeployment(raw), config["revision"])
    config["revision"] = replacement.digest
    return config, backend, deployments, raw


def credential_cycle(config, declared=True, grants=None):
    step = agent_node()
    step["runtime"]["overrides"]["secrets"] = ["PROVIDER_TOKEN"] if grants is None else grants
    raw = model.to_document(workflow(config, step))
    if declared:
        raw["variables"] = {"PROVIDER_TOKEN": {"secret": True}, "OTHER_TOKEN": {"secret": True}}
    return model.parse_cycle(raw, "runtime")


def test_schema_roundtrips_cycle_and_node_restrictions(setup_runtime):
    config, *_ = setup_runtime
    cycle = workflow(config, node("a", runtime={"overrides": {"filesystem": []}}))
    assert not model.problems(cycle)
    document = model.to_document(cycle)
    assert model.to_document(model.parse_cycle(document, "runtime")) == document
    graph = model.to_graph(cycle)
    assert graph["execution"]["runtime"] == config
    assert graph["nodes"][0]["runtime_enabled"]
    assert graph["nodes"][0]["runtime"] == {"overrides": {"filesystem": []}}


@pytest.mark.parametrize("config", [None, {}, {"mode": "host"}, {"mode": "isolated", "deployment": "demo", "revision": "bad", "project_directory": "/approved"}])
def test_malformed_configuration_never_becomes_host_execution(tmp_path, config):
    with pytest.raises(checkpoints.CheckpointError):
        executor.run_cycle(workflow(config), str(tmp_path / "run"))
    assert not (tmp_path / "run" / "steps").exists()


def test_node_cannot_select_its_own_provider_or_escape_cycle(setup_runtime, tmp_path):
    config, *_ = setup_runtime
    for value in ({"mode": "host"}, {"deployment": "other"}, {"overrides": {"network": {"mode": "full"}}}):
        with pytest.raises(checkpoints.CheckpointError):
            executor.run_cycle(workflow(config, node("a", runtime=value)), str(tmp_path / "run"))
    cycle = model.parse_cycle({"id": "x", "steps": [node("a", runtime={"overrides": {"filesystem": []}})]}, "x")
    assert any("requires execution.runtime" in e for e in model.problems(cycle))


def test_legacy_fingerprints_do_not_change_when_runtime_is_absent():
    cycle = model.parse_cycle({"id": "x", "steps": [node("a")]}, "x")
    definition = asdict(cycle.steps[0])
    for key in checkpoints._PRESENTATION:
        definition.pop(key, None)
    for key, value in checkpoints._ADDED_LATER.items():
        if definition.get(key) == value:
            definition.pop(key, None)
    expected = checkpoints.digest({"step": definition, "variables": {}, "version": registry.get("command.shell").metadata.version})
    assert checkpoints.signatures(cycle, {}, registry)["a"] == expected
    assert "runtime" not in definition


def test_whole_builtin_hook_path_is_replaced_and_explicit_environment_only(setup_runtime, tmp_path, monkeypatch):
    config, backend, *_ = setup_runtime
    def forbidden(*args):
        pytest.fail("A workload plugin hook ran on the host")
    for method in ("prepare", "execute", "cleanup"):
        monkeypatch.setattr(ShellCommand, method, forbidden)
    cycle = workflow(config, node("a", **{"with": {"command": "printf ok", "env": {"ALLOWED": "explicit"}}}))
    run = executor.run_cycle(cycle, str(tmp_path / "run"), environment={"HOST_ONLY": "never-forward"})
    assert run.ok, run.steps["a"].message
    assert backend.requests[0].argv == ("/approved-shell", "-c", "printf ok")
    assert backend.requests[0].environment.to_document() == {"ALLOWED": "explicit"}
    assert backend.calls == ["scope", "prepare", "bind", "start", "wait", "cleanup", "clear", "release"]
    assert run.steps["a"].runtime["cleanup_confirmed"]
    assert json.loads((tmp_path / "run" / ".execution" / "manifest.json").read_text())["runtime"]["a"]["worker"] == runtime.WORKER


@pytest.mark.parametrize("reference", ["${env.HOST_ONLY}", "${vars.TOKEN}"])
def test_host_environment_and_cycle_secret_references_cannot_lower_to_command(setup_runtime, tmp_path, reference):
    config, backend, *_ = setup_runtime
    cycle = workflow(config, node("a", **{"with": {"command": ["/tool", reference]}}))
    cycle.secrets = ("TOKEN",)
    cycle.variables["TOKEN"] = "placeholder"
    run = executor.run_cycle(cycle, str(tmp_path / "run"), environment={"HOST_ONLY": "private"}, secrets={"TOKEN": "secret"})
    assert not run.ok
    assert not backend.requests
    assert "private" not in json.dumps(asdict(run)) and "secret" not in run.steps["a"].message


def test_metadata_and_subclasses_do_not_authorize_custom_host_hooks(setup_runtime, tmp_path):
    config, backend, *_ = setup_runtime
    class Impostor(ShellCommand):
        def prepare(self, context, step):
            pytest.fail("custom prepare was reached")
    class Plugins:
        def get(self, name):
            return Impostor()
    with pytest.raises(checkpoints.CheckpointError, match="does not support"):
        executor.run_cycle(workflow(config), str(tmp_path / "run"), registry=Plugins())
    assert not backend.calls


def test_retries_clean_each_instance_and_keep_one_step_deadline(setup_runtime, tmp_path):
    config, backend, *_ = setup_runtime
    cycle = workflow(config, node("a", retry={"attempts": 2}, timeout=10,
                                  **{"with": {"command": ["/tool"], "expect_exit": 1}}))
    run = executor.run_cycle(cycle, str(tmp_path / "run"))
    assert not run.ok and run.steps["a"].attempts == 2
    assert len(backend.requests) == backend.calls.count("cleanup") == backend.calls.count("clear") == 2
    assert 0 < backend.requests[1].timeout_seconds < backend.requests[0].timeout_seconds <= 10
    assert backend.calls.index("cleanup") < backend.calls.index("prepare", backend.calls.index("prepare") + 1)


def test_approval_contains_runtime_binding_and_deployment_change_refuses_launch(setup_runtime, tmp_path):
    config, backend, deployments, raw = setup_runtime
    receipts = []
    def event(kind, **fields):
        if kind == "cycle.node.approval" and fields["receipt"]["status"] == "pending":
            receipt = fields["receipt"]
            receipts.append(receipt)
            raw["metadata"]["description"] = "changed after review"
            deployments.save(RuntimeDeployment(raw), config["revision"])
            assert not executor.answer_node_approval(dict(run_id=receipt["run_id"], request_id=receipt["id"],
                fingerprint=receipt["fingerprint"], approved=True))
    run = executor.run_cycle(workflow(config), str(tmp_path / "run"), controlled=True, mode="approve", observer=EventObserver(sink=event))
    assert not run.ok and not backend.requests
    assert receipts[0]["runtime"]["revision"] == config["revision"]
    assert "deployment_revision_conflict" in run.steps["a"].message


def test_resume_refuses_changed_policy_for_preserved_success(setup_runtime, tmp_path):
    config, backend, *_ = setup_runtime
    cycle = workflow(config, node("a"), node("b", needs=["a"], **{"with": {"command": ["/tool"], "expect_exit": 1}}))
    run = executor.run_cycle(cycle, str(tmp_path / "run"))
    assert run.steps["a"].ok and not run.ok
    cycle.steps[0].runtime = {"overrides": {"filesystem": []}}
    before = len(backend.requests)
    with pytest.raises(checkpoints.CheckpointError, match="changed"):
        executor.run_cycle(cycle, str(tmp_path / "run"), resume=True)
    assert len(backend.requests) == before


def test_partial_run_cannot_borrow_host_result_as_isolated_evidence(setup_runtime, tmp_path):
    config, backend, *_ = setup_runtime
    cycle = workflow(config, node("a"), node("b", needs=["a"]))
    old = CycleRun("old", cycle.id, steps={"a": StepRun("a", status=SUCCESS)})
    with pytest.raises(checkpoints.CheckpointError, match="changed"):
        executor.run_cycle(cycle, str(tmp_path / "run"), only=["b"], reuse=old)
    assert not backend.requests


def test_unconfirmed_cleanup_stops_run_and_forbids_retry_even_with_continue(setup_runtime, tmp_path, monkeypatch):
    config, backend, *_ = setup_runtime
    def broken(handle):
        backend.calls.append("cleanup_failed")
        raise OSError("synthetic cleanup failure")
    monkeypatch.setattr(backend, "cleanup", broken)
    cycle = workflow(config, node("a", retry={"attempts": 3}, on_failure="continue"), node("b", needs=["a"]))
    run = executor.run_cycle(cycle, str(tmp_path / "run"))
    assert not run.ok and len(backend.requests) == 1
    assert not run.steps["a"].runtime["cleanup_confirmed"]
    assert "clear" not in backend.calls
    assert run.steps["a"].attempts == 1


def test_working_directory_is_native_and_never_created_on_host(setup_runtime, tmp_path):
    config, backend, *_ = setup_runtime
    missing = tmp_path / "run" / "must-not-be-created"
    cycle = workflow(config, node("a", **{"with": {"command": ["/tool"], "dir": "must-not-be-created"}}))
    executor.run_cycle(cycle, str(tmp_path / "run"))
    assert backend.requests[0].working_directory == "/workspace/must-not-be-created"
    assert not missing.exists()
    root = workflow(config, node("a", **{"with": {"command": ["/tool"], "dir": "/"}}))
    assert executor.run_cycle(root, str(tmp_path / "run-root")).ok
    assert backend.requests[-1].working_directory == "/"


def test_cycle_lowering_uses_backend_platform_paths_and_configured_windows_shell(setup_runtime, tmp_path):
    config, backend, deployments, raw = setup_runtime
    backend.platform, backend.provider = "windows", "windows-isolated"
    raw["spec"].update(provider="windows-isolated", working_directory=r"D:\Workspace",
        shell=[r"C:\Runtime\pwsh.exe", "-Command"],
        bindings=[dict(name="project", source=r"C:\Approved", target=r"D:\Workspace")])
    replacement = deployments.save(RuntimeDeployment(raw), config["revision"])
    config.update(revision=replacement.digest, project_directory=r"C:\Approved")
    cycle = workflow(config, node("a", **{"with": {"command": "Write-Output ok", "dir": "subdir"}}))
    assert executor.run_cycle(cycle, str(tmp_path / "run")).ok
    request = backend.requests[0]
    assert request.argv == (r"C:\Runtime\pwsh.exe", "-Command", "Write-Output ok")
    assert request.working_directory == r"D:\Workspace\subdir"


def test_resume_artifacts_use_a_new_attempt_directory(setup_runtime, tmp_path):
    config, backend, *_ = setup_runtime
    cycle = workflow(config, node("a", **{"with": {"command": ["/tool"], "expect_exit": 1}}))
    executor.run_cycle(cycle, str(tmp_path / "run"))
    run = executor.run_cycle(cycle, str(tmp_path / "run"), resume=True)
    assert run.steps["a"].attempts == 2
    assert run.steps["a"].runtime["attempt"] == 2
    assert any("runtime-0002" in one.path for one in run.steps["a"].artifacts)


def test_resume_of_pending_node_requires_new_approval_after_policy_change(setup_runtime, tmp_path):
    from cycle.context import CancelToken
    config, backend, *_ = setup_runtime
    cycle = workflow(config)
    token, old = CancelToken(), []
    def first(kind, **fields):
        if kind == "cycle.node.approval" and fields["receipt"]["status"] == "pending":
            old.append(copy.deepcopy(fields["receipt"]))
            token.set("stop before consent")
    run = executor.run_cycle(cycle, str(tmp_path / "run"), controlled=True, mode="approve", cancel=token,
                             observer=EventObserver(sink=first))
    assert not run.ok and old and not backend.requests
    cycle.steps[0].runtime = {"overrides": {"filesystem": [{"binding": "project", "access": "read_only"}]}}
    fresh, refusals = [], []
    def second(kind, **fields):
        if kind == "cycle.control.refused":
            refusals.append(fields)
        if kind != "cycle.node.approval" or fields["receipt"]["status"] != "pending":
            return
        receipt = fields["receipt"]
        fresh.append(copy.deepcopy(receipt))
        def packet(one):
            return dict(run_id=one["run_id"], request_id=one["id"], fingerprint=one["fingerprint"], approved=True)
        # Public control API queues answers; the scheduler judges them later.
        assert not executor.answer_node_approval(packet(old[0]))
        assert not backend.requests
        assert not executor.answer_node_approval(packet(receipt))
    resumed = executor.run_cycle(cycle, str(tmp_path / "run"), resume=True, controlled=True,
                                 observer=EventObserver(sink=second))
    assert resumed.ok and len(backend.requests) == 1
    assert refusals and refusals[0]["action"] == "node_approval"
    assert resumed.node_approvals[old[0]["id"]]["status"] == "invalidated"
    assert fresh[0]["definition_digest"] != old[0]["definition_digest"]
    assert fresh[0]["runtime"]["policy_digest"] != old[0]["runtime"]["policy_digest"]


def test_git_worker_replaces_host_hooks_and_finishes_isolated_cleanup_before_downstream(git_runtime, tmp_path, monkeypatch):
    from cycle.plugins.git import GitCheckout
    from execution_runtime.workers import GIT_CONTRACT
    config, backend, *_ = git_runtime
    def forbidden(*args):
        pytest.fail("A Git workload hook ran on the host")
    for method in ("prepare", "execute", "cleanup"):
        monkeypatch.setattr(GitCheckout, method, forbidden)
    cycle = workflow(config, git_node(), node("inspect", needs=["checkout"], **{"with": {
        "command": ["/approved-tool"], "dir": "${steps.checkout.outputs.workspace}"}}))
    run = executor.run_cycle(cycle, str(tmp_path / "run"), environment={"GIT_CONFIG": "host-only"})
    assert run.ok, run.steps["checkout"].message
    assert run.steps["checkout"].outputs == dict(workspace="/workspace/source/checkout", commit="c" * 40, branch="main")
    assert run.steps["checkout"].runtime["worker"] == GIT_CONTRACT
    assert run.steps["checkout"].runtime["workspace_cleanup_confirmed"]
    assert len(backend.requests) == 3
    assert json.loads(backend.requests[0].argv[-1])["operation"] == "checkout"
    assert json.loads(backend.requests[1].argv[-1])["operation"] == "cleanup"
    assert backend.requests[1].timeout_seconds == 3
    assert backend.requests[2].working_directory == "/workspace/source/checkout"
    assert all(not one.environment.to_document() for one in backend.requests)
    assert backend.calls.count("cleanup") == 3


@pytest.mark.parametrize("settings,code", [
    ({"repository": "https://example.invalid/repo.git"}, "local_git_repository_required"),
    ({"repository": "git@example.invalid:repo"}, "local_git_repository_required"),
    ({"repository": "/outside/repo"}, "git_repository_read_denied"),
    ({"path": "../escape"}, "invalid_path"),
    ({"path": ".git/repository"}, "invalid_checkout_path"),
    ({"path": ".qavector-checkout-owned/repo"}, "invalid_checkout_path"),
    ({"depth": 1}, "local_clone_depth_unsupported"),
    ({"submodules": True}, "git_submodules_unsupported"),
])
def test_git_requests_fail_closed_before_native_prepare(git_runtime, tmp_path, settings, code):
    config, backend, *_ = git_runtime
    run = executor.run_cycle(workflow(config, git_node(**settings)), str(tmp_path / "run"))
    assert not run.ok and code in run.steps["checkout"].message
    assert not backend.requests


def test_git_worker_requires_pinned_current_artifact_and_explicit_configuration(setup_runtime, tmp_path):
    config, backend, *_ = setup_runtime
    with pytest.raises(checkpoints.CheckpointError, match="provide a Git checkout worker"):
        executor.run_cycle(workflow(config, git_node()), str(tmp_path / "run"))
    assert not backend.requests


def test_git_worker_rejects_mismatched_artifact_before_any_host_or_native_execution(git_runtime, tmp_path):
    config, backend, *_ = git_runtime
    backend.file_identities["/workers/git.py"] = "0" * 64
    with pytest.raises(checkpoints.CheckpointError, match="versioned Git worker artifact"):
        executor.run_cycle(workflow(config, git_node()), str(tmp_path / "run"))
    assert not backend.requests


def test_git_read_only_node_does_not_attempt_to_create_checkout(git_runtime, tmp_path):
    config, backend, *_ = git_runtime
    step = git_node()
    step["runtime"] = {"overrides": {"filesystem": [{"binding": "project", "access": "read_only"}]}}
    run = executor.run_cycle(workflow(config, step), str(tmp_path / "run"))
    assert not run.ok and "git_checkout_write_denied" in run.steps["checkout"].message
    assert not backend.requests


def test_git_cleanup_protocol_failure_blocks_retry_and_resume(git_runtime, tmp_path, monkeypatch):
    config, backend, *_ = git_runtime
    original = backend.prepare
    def prepare(request):
        handle = original(request)
        if json.loads(request.argv[-1])["operation"] == "cleanup":
            (backend.path / handle.instance_id / "stdout.log").write_text('{"kind":"cycle.step.end","status":"success"}')
        return handle
    monkeypatch.setattr(backend, "prepare", prepare)
    step = git_node()
    step.update(retry={"attempts": 3}, on_failure="continue")
    cycle = workflow(config, step)
    run = executor.run_cycle(cycle, str(tmp_path / "run"))
    assert not run.ok and len(backend.requests) == 2
    assert not run.steps["checkout"].runtime["cleanup_confirmed"]
    assert not run.steps["checkout"].runtime["workspace_cleanup_confirmed"]
    assert backend.calls.count("clear") == 0
    resumed = executor.run_cycle(cycle, str(tmp_path / "run"), resume=True)
    assert not resumed.ok and "cleanup is unconfirmed" in resumed.steps["checkout"].message
    assert len(backend.requests) == 2


def test_git_worker_stdout_cannot_supply_control_metadata_or_arbitrary_outputs(git_runtime, tmp_path, monkeypatch):
    config, backend, *_ = git_runtime
    original = backend.prepare
    def prepare(request):
        handle = original(request)
        payload = json.loads(request.argv[-1])
        if payload["operation"] == "checkout":
            (backend.path / handle.instance_id / "stdout.log").write_text(json.dumps({
                "contract": payload["contract"], "status": "success", "outputs": {"commit": "c" * 40, "branch": "main", "workspace": "/forged"}, "code": ""}))
        return handle
    monkeypatch.setattr(backend, "prepare", prepare)
    run = executor.run_cycle(workflow(config, git_node()), str(tmp_path / "run"))
    assert not run.ok and not run.steps["checkout"].outputs
    assert run.steps["checkout"].runtime["cleanup_confirmed"]


def test_agent_worker_avoids_host_hooks_and_builds_trusted_review_artifact(agent_runtime, tmp_path, monkeypatch):
    from cycle.plugins.agent import AgentReview
    config, backend, *_ = agent_runtime
    def forbidden(*args, **kwargs):
        pytest.fail("Agent host hook/discovery must not run")
    for key in ("prepare", "execute", "cleanup", "run_action"):
        monkeypatch.setattr(AgentReview, key, forbidden)
    monkeypatch.setattr("cycle.plugins.agent_cli.auth_status", forbidden)
    run = executor.run_cycle(workflow(config, agent_node(inputs={"reason": "fixture"}, files=["src/code.py"])), str(tmp_path / "run"))
    step = run.steps["review"]
    assert run.ok, step.message
    assert step.outputs["summary"] == "Synthetic review" and step.outputs["issue_count"] == 0
    assert step.runtime["worker"] == "qavector.agent-review/v1" and step.runtime["cleanup_confirmed"]
    payload = json.loads(backend.requests[0].argv[-1])
    assert payload["request"]["files"] == ["/workspace/src/code.py"]
    assert backend.requests[0].working_directory == "/workspace/src" and backend.requests[0].environment.to_document() == {}
    report = tmp_path / "run" / step.outputs["report_path"]
    assert json.loads(report.read_text())["summary"] == "Synthetic review"
    receipt = next((tmp_path / "run" / "steps/review/operations").glob("*/receipt.json"))
    assert json.loads(receipt.read_text())["input_delivered"] is True


@pytest.mark.parametrize("settings,code", [
    ({"python": "/host-python"}, "review_provider_override_forbidden"),
    ({"api_key_env": "HOST_KEY"}, "review_provider_override_forbidden"),
    ({"model_options": {"base_url": "https://example.invalid"}}, "review_provider_override_forbidden"),
    ({"files": ["/control/secret"]}, "review_read_denied"),
    ({"repository": "../escape"}, "invalid_path"),
    ({"files": ["src/a"] * 17}, "invalid_review_evidence"),
    ({"task": "x" * 32769}, "invalid_review_text"),
    ({"inputs": {"large": "x" * 66000}}, "review_packet_too_large"),
])
def test_agent_worker_denies_unapproved_inputs_before_native_prepare(agent_runtime, tmp_path, settings, code):
    config, backend, *_ = agent_runtime
    run = executor.run_cycle(workflow(config, agent_node(**settings)), str(tmp_path / "run"))
    assert not run.ok and code in run.steps["review"].message
    assert not backend.requests


def test_agent_review_requires_read_only_mounts_before_run(agent_runtime, tmp_path):
    config, backend, *_ = agent_runtime
    step = agent_node()
    step.pop("runtime")
    with pytest.raises(checkpoints.CheckpointError, match="read-only"):
        executor.run_cycle(workflow(config, step), str(tmp_path / "run"))
    assert not backend.requests


def test_unsupported_agent_framework_refuses_the_whole_plan_before_any_workload(agent_runtime, tmp_path):
    config, backend, *_ = agent_runtime
    with pytest.raises(checkpoints.CheckpointError, match="review_framework_unsupported|framework runtime"):
        executor.run_cycle(workflow(config, node("before"), agent_node(framework="claude_cli")), str(tmp_path / "run"))
    assert not backend.requests


def test_agent_worker_requires_pinned_artifact_and_provider(agent_runtime, tmp_path):
    config, backend, *_ = agent_runtime
    backend.file_identities.pop("/workers/provider.py")
    with pytest.raises(checkpoints.CheckpointError, match="not pinned"):
        executor.run_cycle(workflow(config, agent_node()), str(tmp_path / "run"))
    assert not backend.requests


@pytest.mark.parametrize("raw", [
    '{"kind":"cycle.step.end","status":"success"}',
    '{"contract":"qavector.agent-review/v1","review":{},"code":""}',
    '{"contract":"qavector.agent-review/v1","review":null,"code":"review_provider_failed"}',
    'x' * 65537,
], ids=["forged-event", "invalid-review", "provider-failure", "oversize"])
def test_unconfirmed_agent_result_blocks_retry_and_resume(agent_runtime, tmp_path, monkeypatch, raw):
    config, backend, *_ = agent_runtime
    original = backend.prepare
    def prepare(request):
        handle = original(request)
        (backend.path / handle.instance_id / "stdout.log").write_text(raw)
        return handle
    monkeypatch.setattr(backend, "prepare", prepare)
    step = agent_node()
    step.update(retry={"attempts": 3}, on_failure="continue")
    cycle = workflow(config, step)
    run = executor.run_cycle(cycle, str(tmp_path / "run"))
    assert not run.ok and len(backend.requests) == 1
    assert run.steps["review"].runtime["cleanup_confirmed"]
    with pytest.raises(checkpoints.CheckpointError, match="Uncertain outcome"):
        executor.run_cycle(cycle, str(tmp_path / "run"), resume=True)
    assert len(backend.requests) == 1


def test_agent_report_paths_and_counts_cannot_be_forged_by_provider(agent_runtime, tmp_path, monkeypatch):
    config, backend, *_ = agent_runtime
    original = backend.prepare
    def prepare(request):
        handle = original(request)
        path = backend.path / handle.instance_id / "stdout.log"
        value = json.loads(path.read_text())
        value["review"].update(report_path="/host/forged", runtime={"cleanup_confirmed": False}, issue_count=500,
            issues=[{"severity": "high", "description": "Fixture", "runtime": "forged"}])
        path.write_text(json.dumps(value))
        return handle
    monkeypatch.setattr(backend, "prepare", prepare)
    run = executor.run_cycle(workflow(config, agent_node()), str(tmp_path / "run"))
    step = run.steps["review"]
    assert run.ok and step.outputs["issue_count"] == step.outputs["blocking_count"] == 1
    assert step.outputs["report_path"].startswith("steps/review/runtime-")
    assert "runtime" not in step.outputs and "runtime" not in step.outputs["issues"][0]
    assert step.runtime["cleanup_confirmed"]


def test_runtime_review_never_falls_back_to_legacy_host_execution(tmp_path, monkeypatch):
    from cycle.plugins.agent import AgentReview
    monkeypatch.setattr("cycle.plugins.agent_cli.auth_status", lambda *a, **kw: pytest.fail("No host auth call"))
    step = agent_node()
    step.pop("runtime")
    cycle = model.parse_cycle({"id": "host", "steps": [step]}, "host")
    run = executor.run_cycle(cycle, str(tmp_path / "run"))
    assert not run.ok and "requires isolated" in run.steps["review"].message
    assert not AgentReview().run_action("sign_in_status", {"framework": "runtime"})["ok"]


def test_runtime_review_snapshots_clarifications_and_reuses_only_receipted_answer(agent_runtime, tmp_path):
    from types import SimpleNamespace
    from cycle import interactions
    from cycle.context import RunContext
    config, backend, *_ = agent_runtime
    cycle = workflow(config, agent_node())
    run = CycleRun(id="clarifications", cycle_id=cycle.id, workspace=str(tmp_path / "run"), steps={
        "review": StepRun("review", plugin="agent.review", status="running")})
    context = RunContext(run, cycle, run.workspace)
    store = interactions.RunInteractions(str(Path(run.workspace) / ".execution"), run.id, workspace=run.workspace)
    store.open(run, cycle, registry)
    store.begin("review", 1, registry.get("agent.review"))
    context.interactions = store
    message, problem = interactions.parse_request({"id": "note", "run_id": run.id, "scope": "step", "step": "review",
        "execution": 1, "kind": "text", "payload": {"text": "Also check the timer caller."}})
    assert not problem
    _, problem = store.accept(message)
    assert not problem
    worker = runtime.CycleRuntime(cycle, registry, run.workspace).workers["review"]
    step = SimpleNamespace(id="review", settings=cycle.steps[0].settings)
    worker.prepare(context, step)
    first = worker.execute(context, step)
    assert first.ok
    payload = json.loads(backend.requests[0].argv[-1])
    assert payload["request"]["clarifications"][0]["text"] == "Also check the timer caller."
    assert store.messages()[0]["state"] == "delivered"
    second = worker.execute(context, step)
    assert second.ok and second.outputs == first.outputs and len(backend.requests) == 1


def test_runtime_review_requires_approved_scratch_capacity(agent_runtime, tmp_path):
    config, backend, deployments, raw = agent_runtime
    raw["spec"]["policy"].pop("resources")
    replacement = deployments.save(RuntimeDeployment(raw), config["revision"])
    config["revision"] = replacement.digest
    with pytest.raises(checkpoints.CheckpointError, match="bounded scratch"):
        executor.run_cycle(workflow(config, agent_node()), str(tmp_path / "run"))
    assert not backend.requests


def test_review_credentials_are_explicitly_scoped_and_never_enter_argv_or_receipts(credential_runtime, tmp_path):
    config, backend, *_ = credential_runtime
    sensitive = "synthetic-provider-sensitive-token"
    run = executor.run_cycle(credential_cycle(config), str(tmp_path / "run"),
        secrets={"PROVIDER_TOKEN": sensitive, "OTHER_TOKEN": "unrelated-sensitive-token"})
    assert run.ok, run.steps["review"].message
    request = backend.requests[0]
    assert request.secret_values.items() == (("PROVIDER_TOKEN", sensitive),)
    assert request.environment.to_document() == {}
    payload = json.loads(request.argv[-1])
    assert payload["request"]["contract"] == "qavector.review-provider/v2"
    assert payload["request"]["credential_files"] == {"api_key": "/synthetic-secrets/PROVIDER_TOKEN"}
    assert sensitive not in repr(request) + request.argv[-1]
    info = run.steps["review"].runtime
    assert info["credential_references"] == {"api_key": "PROVIDER_TOKEN"}
    assert info["credential_delivery"] == "native_read_only_files" and info["cleanup_confirmed"]
    for path in (tmp_path / "run").rglob("*"):
        if path.is_file():
            assert sensitive.encode() not in path.read_bytes()
    assert not run.steps["review"].outputs.get("credential_files")


@pytest.mark.parametrize("grants", [[], ["OTHER_TOKEN"], ["PROVIDER_TOKEN", "OTHER_TOKEN"]])
def test_review_credentials_require_exact_node_grants_before_any_workload(credential_runtime, tmp_path, grants):
    config, backend, *_ = credential_runtime
    with pytest.raises(checkpoints.CheckpointError, match="match the approved provider credential"):
        executor.run_cycle(credential_cycle(config, grants=grants), str(tmp_path / "run"), secrets={"PROVIDER_TOKEN": "not-forwarded"})
    assert not backend.requests


def test_review_credential_reference_must_be_declared_as_a_cycle_secret(credential_runtime, tmp_path):
    config, backend, *_ = credential_runtime
    with pytest.raises(checkpoints.CheckpointError, match="declared as Cycle secrets"):
        executor.run_cycle(credential_cycle(config, declared=False), str(tmp_path / "run"), secrets={"PROVIDER_TOKEN": "not-forwarded"})
    assert not backend.requests


@pytest.mark.parametrize("values", [{}, {"PROVIDER_TOKEN": ""}, {"PROVIDER_TOKEN": 123}])
def test_review_missing_credentials_never_fall_back_to_host_environment(credential_runtime, tmp_path, monkeypatch, values):
    config, backend, *_ = credential_runtime
    monkeypatch.setenv("PROVIDER_TOKEN", "host-only-sensitive-value")
    run = executor.run_cycle(credential_cycle(config), str(tmp_path / "run"), secrets=values)
    assert not run.ok and "review_credential_unavailable" in run.steps["review"].message
    assert not backend.requests


def test_review_credential_delivery_requires_native_backend_support(credential_runtime, tmp_path, monkeypatch):
    config, backend, *_ = credential_runtime
    monkeypatch.setattr(backend, "secret_file_locations", lambda names: RuntimeBackend.secret_file_locations(backend, names))
    with pytest.raises(checkpoints.CheckpointError, match="scoped secret file delivery"):
        executor.run_cycle(credential_cycle(config), str(tmp_path / "run"), secrets={"PROVIDER_TOKEN": "not-forwarded"})
    assert not backend.requests


def test_non_provider_cycle_node_must_revoke_inherited_credential_grants(credential_runtime, tmp_path):
    config, backend, *_ = credential_runtime
    with pytest.raises(checkpoints.CheckpointError, match="does not deliver credentials"):
        executor.run_cycle(workflow(config, node("shell")), str(tmp_path / "run"), secrets={"PROVIDER_TOKEN": "not-forwarded"})
    assert not backend.requests
