"""Exact installed and bundled contribution bindings before any workload runs."""

import copy
import os
import sqlite3
from uuid import uuid4

from cycle import checkpoints
from plugin_host import bundled
from plugin_host.registry import PluginRegistry
from qavector_sdk import ContractError


def _requirements(cycle, registry):
    from cycle.agent_profiles import selected
    from cycle.plugins.agent import AgentReview
    from cycle.plugins.agent_edit import AgentEdit
    from cycle.plugins.agent_implement import AgentImplement
    from cycle.plugins.agent_merge import AgentMerge
    from cycle.plugins.profile_agent import ProfileAgent
    from cycle.plugins.installed import InstalledNode
    steps, constraints = {}, {}
    for step in cycle.steps:
        plugin = registry.get(step.plugin)
        if step.disabled:
            continue
        if type(plugin) is InstalledNode:
            if 'runtime' not in cycle.execution:
                raise ValueError('%s: installed plugins require execution.runtime' % step.id)
            reference, constraint = step.plugin, step.settings.get('plugin_version', '>=0.0.0')
        elif type(plugin) is ProfileAgent:
            profile = selected(cycle, step.settings.get("agent_profile"))
            reference, constraint = profile["backend_ref"], profile["backend_version"]
        elif "runtime" in cycle.execution:
            continue  # built-in native workers carry their own runtime binding
        elif (type(plugin) in (AgentEdit, AgentImplement, AgentMerge) or
              type(plugin) is AgentReview and step.settings.get("framework", "claude_cli")
              not in ("crewai", "autogen", "runtime")):
            reference, constraint = "claude_cli", ">=1.0.0,<2.0.0"
        else:
            continue
        from qavector_sdk.versions import satisfies
        satisfies('0.0.0', constraint)  # Validate before aggregation/fingerprinting.
        steps[step.id] = reference
        constraints.setdefault(reference, set()).add(constraint)
    return steps, {ref: {"constraint": ",".join(sorted(values))}
                   for ref, values in constraints.items()}


class CyclePluginPlan:
    def __init__(self, cycle, registry, store, *, resume=False):
        self.bindings, self.specs, self.lock = {}, {}, None
        self.path = bundled.registry_path()
        try:
            self.steps, self.requirements = _requirements(cycle, registry)
        except (ContractError, ValueError) as exc:
            raise checkpoints.CheckpointError("Plugin plan: %s" % exc) from exc
        saved_path = os.path.join(store.directory, "plugin-lock.json")
        manifest_path = os.path.join(store.directory, "manifest.json")
        manifest = checkpoints._read(manifest_path) if resume and os.path.isfile(manifest_path) else {}
        if resume and not os.path.isfile(saved_path):
            if manifest.get("plugins"):
                raise checkpoints.CheckpointError("The saved plugin lock is missing; Resume cannot select a replacement backend.")
            if 'runtime' in cycle.execution and self.requirements:
                raise checkpoints.CheckpointError('Installed workers cannot Resume without the original plugin lock.')
            # Historical runs have no package attestation. Preserve their old
            # fingerprint/receipt behavior rather than inventing one retroactively.
            return
        if not self.requirements and not os.path.isfile(saved_path):
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            catalog = PluginRegistry(self.path)
            try:
                if any(ref in ('claude_cli', 'qavector.agent.claude_cli') for ref in self.requirements):
                    bundled.register_claude(catalog)
                if os.path.isfile(saved_path):
                    saved = checkpoints._read(saved_path)
                    if (not isinstance(saved, dict) or set(saved) != {"schema_version", "catalog_lock", "steps"}
                            or saved["schema_version"] != "1.0.0"
                            or not isinstance(saved["catalog_lock"], dict)):
                        raise checkpoints.CheckpointError("Invalid saved plugin lock")
                    original = saved["catalog_lock"]
                    self.lock = catalog.lock_run(original.get("run_id"), self.requirements, require_existing=True)
                    if original != self.lock or saved["steps"] != self.steps:
                        raise checkpoints.CheckpointError("The Cycle plugin requirements changed; start a new run.")
                else:
                    self.lock = catalog.lock_run("cycle-" + uuid4().hex, self.requirements)
                self.bindings = {step: copy.deepcopy(self.lock["bindings"][reference])
                                 for step, reference in self.steps.items()}
                for step, binding in self.bindings.items():
                    if binding['plugin_id'] == 'qavector.agent.claude_cli':
                        bundled.verify_claude(binding)
                    else:
                        self.specs[step] = self.worker_spec(step)
                if resume and manifest.get("plugins", {}) != self.bindings:
                    raise checkpoints.CheckpointError("Saved plugin bindings differ from the execution manifest")
                if not os.path.isfile(saved_path):
                    store.write(saved_path, {"schema_version": "1.0.0", "catalog_lock": self.lock,
                                            "steps": self.steps})
            finally:
                catalog.close()
        except (ContractError, ValueError, OSError, sqlite3.Error) as exc:
            raise checkpoints.CheckpointError("Plugin plan: %s" % exc) from exc

    def verify(self, step_id):
        """Recheck quarantine and actual bundled content immediately before use."""
        binding = self.bindings.get(step_id)
        if binding is None:
            return None  # a historical/unadapted run, never an external import
        catalog = None
        try:
            catalog = PluginRegistry(self.path)
            lock = catalog.lock_run(self.lock["run_id"], self.requirements, require_existing=True)
            if lock != self.lock:
                raise ContractError("PLUGIN_NOT_AVAILABLE", "Original backend lock changed")
            if binding['plugin_id'] == 'qavector.agent.claude_cli':
                bundled.verify_claude(binding)
            else:
                if self.worker_spec(step_id) != self.specs[step_id]:
                    raise ContractError('PLUGIN_NOT_AVAILABLE', 'Pinned worker environment changed')
            return copy.deepcopy(binding)
        except (ContractError, ValueError, OSError, sqlite3.Error) as exc:
            raise checkpoints.CheckpointError("Pinned backend unavailable: %s" % exc) from exc
        finally:
            if catalog is not None:
                catalog.close()

    def worker_spec(self, step_id):
        from plugin_host.catalog import manager
        host = manager()
        try:
            return host.locked_worker_spec(self.lock, self.steps[step_id])
        finally:
            host.close()
