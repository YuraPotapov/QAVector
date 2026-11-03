"""Cycle package pins remain exact across resume, activation and content changes."""

import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from cycle import checkpoints, executor, model, registry
from cycle.plugin_plan import CyclePluginPlan
from plugin_host import bundled
from plugin_host.registry import PluginRegistry
from qavector_sdk import ContractError


def workflow():
    return model.parse_cycle({"id": "demo", "agent_profiles": {"agent.developer": {
        "backend_ref": "qavector.agent.claude_cli", "model": "sonnet",
        "instructions": "Implement the task.",
        "backend_options": {"binary": "/fake/claude", "tools": ["Read"]}}},
        "steps": [{"id": "work", "plugin": "agent", "with": {
            "agent_profile": "agent.developer", "task": "Fix the parser"}}]}, "demo")


@pytest.fixture
def prepared(tmp_path):
    cycle = workflow()
    store = checkpoints.Store(str(tmp_path / "run"))
    plan = CyclePluginPlan(cycle, registry, store)
    store.write(str(Path(store.directory) / "manifest.json"), {"plugins": plan.bindings})
    return cycle, store, plan


def test_bundled_catalog_never_imports_backend_entrypoint(tmp_path):
    script = ("import sys; sys.path.insert(0, " + repr(str(Path(__file__).parents[1])) + "); "
        "from plugin_host.bundled import register_claude; from plugin_host.registry import PluginRegistry; "
        "catalog = PluginRegistry(" + repr(str(tmp_path / "isolated.db")) + "); "
        "register_claude(catalog); assert 'qavector_agent_claude.backend' not in sys.modules; "
        "assert catalog.resolve('claude_cli')['contribution']['kind'] == 'agent_backend'; catalog.close()")
    process = subprocess.run([sys.executable, "-I", "-B", "-c", script],
                             capture_output=True, text=True, timeout=20)
    assert process.returncode == 0, process.stderr


def test_new_cycle_saves_exact_binding_and_reopens_it(prepared):
    cycle, store, plan = prepared
    binding = plan.bindings["work"]
    assert binding == bundled.claude_binding()
    assert len(binding["digest"]) == 64
    assert CyclePluginPlan(cycle, registry, store, resume=True).bindings == plan.bindings
    assert plan.verify("work") == binding
    without = checkpoints.signatures(cycle, {}, registry)
    pinned = checkpoints.signatures(cycle, {}, registry, plugin_bindings=plan.bindings)
    assert without != pinned


def test_concurrent_first_runs_observe_atomic_bundled_activation(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = str(tmp_path / "shared.db")

    def register_and_resolve(_):
        catalog = PluginRegistry(path)
        try:
            bundled.register_claude(catalog)
            return catalog.resolve("claude_cli")
        finally:
            catalog.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        bindings = list(pool.map(register_and_resolve, range(4)))
    assert bindings == [bundled.claude_binding()] * 4


def test_activating_other_version_does_not_redirect_existing_run(prepared, tmp_path):
    cycle, store, plan = prepared
    catalog = PluginRegistry(bundled.registry_path())
    try:
        newer = bundled.claude_manifest()
        newer["version"] = "1.1.0"
        catalog.register(newer, "a" * 64, publisher_identity="qavector:bundled", bundled=True)
        catalog.set_state(newer["plugin_id"], newer["version"], "active")
        resumed = CyclePluginPlan(cycle, registry, store, resume=True)
        assert resumed.verify("work") == plan.bindings["work"]
        # A database entry alone is insufficient to execute another version
        # using the current bundled class. Managed versioned workers come later.
        with pytest.raises(checkpoints.CheckpointError, match="Exact pinned bundled backend"):
            CyclePluginPlan(cycle, registry, checkpoints.Store(str(tmp_path / "new")))
    finally:
        catalog.close()


def test_disabled_package_is_not_reactivated_by_discovery_but_old_lock_can_finish(prepared, tmp_path):
    cycle, store, plan = prepared
    catalog = PluginRegistry(bundled.registry_path())
    try:
        catalog.set_state("qavector.agent.claude_cli", bundled.claude_binding()["version"], "disabled")
        bundled.register_claude(catalog)
        with pytest.raises(ContractError):
            catalog.resolve("claude_cli")
        assert CyclePluginPlan(cycle, registry, store, resume=True).verify("work") == plan.bindings["work"]
        with pytest.raises(checkpoints.CheckpointError, match="No active version"):
            CyclePluginPlan(cycle, registry, checkpoints.Store(str(tmp_path / "new")))
    finally:
        catalog.close()


def test_quarantine_revokes_even_a_running_plan(prepared):
    cycle, store, plan = prepared
    catalog = PluginRegistry(bundled.registry_path())
    try:
        catalog.set_state("qavector.agent.claude_cli", bundled.claude_binding()["version"], "quarantined")
        with pytest.raises(checkpoints.CheckpointError, match="cannot resume"):
            plan.verify("work")
        with pytest.raises(checkpoints.CheckpointError, match="cannot resume"):
            CyclePluginPlan(cycle, registry, store, resume=True)
    finally:
        catalog.close()


def test_changed_bundled_content_refuses_before_execution(prepared, monkeypatch):
    cycle, store, plan = prepared
    original = bundled._payload

    def changed(root, prefix):
        for name, data in original(root, prefix):
            yield name, data + b"changed" if name.endswith("/backend.py") else data

    monkeypatch.setattr(bundled, "_payload", changed)
    with pytest.raises(checkpoints.CheckpointError, match="Exact pinned bundled backend"):
        plan.verify("work")
    with pytest.raises(checkpoints.CheckpointError, match="different content"):
        CyclePluginPlan(cycle, registry, store, resume=True)


@pytest.mark.parametrize("missing", ["file", "database_lock"])
def test_missing_pin_never_resolves_active_version_as_fallback(prepared, missing):
    cycle, store, plan = prepared
    if missing == "file":
        (Path(store.directory) / "plugin-lock.json").unlink()
    else:
        catalog = PluginRegistry(bundled.registry_path())
        catalog.db.execute("DELETE FROM run_locks")
        catalog.close()
    with pytest.raises(checkpoints.CheckpointError, match="lock is missing"):
        CyclePluginPlan(cycle, registry, store, resume=True)


def test_legacy_run_has_no_invented_attestation_or_catalog_write(tmp_path, monkeypatch):
    store = checkpoints.Store(str(tmp_path / "run"))
    store.write(str(Path(store.directory) / "manifest.json"), {"schema": 1, "steps": {}})
    monkeypatch.setattr(bundled, "register_claude", lambda *a: pytest.fail("Historical run must not acquire new pins"))
    plan = CyclePluginPlan(workflow(), registry, store, resume=True)
    assert plan.bindings == {} and plan.verify("work") is None
    assert not (Path(store.directory) / "plugin-lock.json").exists()


def test_plain_cycle_does_not_create_plugin_catalog(tmp_path):
    cycle = model.parse_cycle({"steps": [{"id": "wait", "plugin": "wait", "with": {"seconds": 0}}]}, "demo")
    plan = CyclePluginPlan(cycle, registry, checkpoints.Store(str(tmp_path / "run")))
    assert plan.bindings == {} and not Path(bundled.registry_path()).exists()


def test_legacy_claude_nodes_use_reserved_alias_without_changing_yaml(tmp_path):
    raw = {"id": "demo", "steps": [{"id": "review", "plugin": "agent.review", "with": {
        "framework": "claude_cli", "task": "Review", "model": "sonnet"}},
        {"id": "worker", "plugin": "agent.review", "with": {"framework": "crewai"}}]}
    cycle = model.parse_cycle(copy.deepcopy(raw), "demo")
    plan = CyclePluginPlan(cycle, registry, checkpoints.Store(str(tmp_path / "run")))
    assert plan.steps == {"review": "claude_cli"}
    assert model.to_document(cycle) == raw


def test_existing_execution_refusal_does_not_write_new_lock_into_old_run(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    (root / "metadata.json").write_text("{}")
    with pytest.raises(checkpoints.CheckpointError, match="already exists"):
        executor.run_cycle(workflow(), str(root), registry=registry)
    assert not (root / ".execution/plugin-lock.json").exists()


def test_execution_manifest_and_paid_receipt_include_the_same_binding(tmp_path, monkeypatch):
    from cycle.plugins import agent_run
    cycle = workflow()
    monkeypatch.setattr("cycle.plugins.agent_cli.auth_status", lambda *a, **k: {"problem": "", "binary": "/fake/claude"})

    def process(ctx, step, argv, cwd, name, **kwargs):
        Path(ctx.step_dir(step.id), "stdout.log").write_text(json.dumps({"type": "result", "result": "Implemented"}))
        return registry.succeeded()

    monkeypatch.setattr(agent_run, "run_process", process)
    run = executor.run_cycle(cycle, str(tmp_path / "run"), registry=registry)
    assert run.status == "success", run.steps["work"].message
    manifest = json.loads(Path(run.workspace, ".execution/manifest.json").read_text())
    [receipt] = Path(run.workspace).glob("steps/work/operations/*/receipt.json")
    assert json.loads(receipt.read_text())["answer"]["backend_binding"] == manifest["plugins"]["work"]
