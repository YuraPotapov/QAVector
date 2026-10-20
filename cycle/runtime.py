"""Trusted Cycle lowering into the common runtime; no workload plugin hooks on host.

Reviewed workers accept data of exact built-in command, Git and agent review nodes.
Adding a plugin requires its own reviewed lowering/worker, never trusting metadata
or calling an arbitrary plugin's prepare/execute/cleanup in the control process.
"""

import copy
import json
import os
from pathlib import Path
import shutil
import time

from execution_runtime._json import object_fields
from execution_runtime.backends.base import ExecutionRequest
from execution_runtime.deployments import DeploymentRegistry
from execution_runtime.errors import RuntimePolicyError
from execution_runtime.paths import PathMapper
from execution_runtime.policy import PolicyPatch
from execution_runtime.registry import EnvironmentRegistry, profile_name, revision_name
from execution_runtime.runtime import ExecutionRuntime

from . import checkpoints
from .context import Cancelled
from .registry import PluginResult
from domain.cycle import Artifact, FAILED, SUCCESS

WORKER = "qavector.command-shell/v1"


def configuration(value):
    object_fields(value, ("mode", "deployment", "revision", "project_directory", "overrides"),
                  "execution.runtime", ("mode", "deployment", "revision", "project_directory"))
    if value["mode"] != "isolated":
        raise RuntimePolicyError("invalid_cycle_runtime", "Cycle runtime must select isolated execution.")
    profile_name(value["deployment"])
    revision_name(value["revision"])
    if not isinstance(value["project_directory"], str) or not value["project_directory"]:
        raise RuntimePolicyError("invalid_cycle_runtime", "Select the approved project directory explicitly.")
    PolicyPatch(value.get("overrides", {}))
    return value


def problems(cycle):
    found = []
    configured = "runtime" in cycle.execution
    if configured:
        try:
            configuration(cycle.execution["runtime"])
        except RuntimePolicyError as exc:
            found.append("execution.runtime: %s" % exc)
    for step in cycle.steps:
        try:
            object_fields(step.runtime, ("overrides",), "runtime")
            PolicyPatch(step.runtime.get("overrides", {}))
            if step.runtime and not configured:
                found.append("%s: node runtime requires execution.runtime on the cycle." % step.id)
        except RuntimePolicyError as exc:
            found.append("%s runtime: %s" % (step.id, exc))
    return found


class CycleRuntime:
    """A pinned policy plan, built before Resume/reuse/approvals are evaluated."""

    def __init__(self, cycle, registry, workspace, deployments=None, profiles=None, backend_factory=None):
        self.cycle, self.workspace = cycle, os.path.abspath(workspace)
        self.deployments = deployments or DeploymentRegistry()
        self.profiles = profiles or EnvironmentRegistry()
        self.backend_factory = backend_factory
        self.bindings, self.workers = {}, {}
        errors = problems(cycle)
        if errors:
            raise checkpoints.CheckpointError("; ".join(errors))
        self.enabled = "runtime" in cycle.execution
        if not self.enabled:
            return
        from .plugins.command import ShellCommand
        from .plugins.git import GitCheckout
        from .runtime_git import GitCheckoutWorker
        from .plugins.agent import AgentReview
        from .runtime_agent import AgentReviewWorker
        worker_types = {ShellCommand: CommandWorker, GitCheckout: GitCheckoutWorker, AgentReview: AgentReviewWorker}
        self.config = copy.deepcopy(configuration(cycle.execution["runtime"]))
        import runtime_paths
        self.protected = (self.workspace, str(self.deployments.directory), str(self.profiles.directory),
                          str(runtime_paths.user_data_root()))
        for step in cycle.steps:
            if step.disabled:
                continue
            worker_type = worker_types.get(type(registry.get(step.plugin)))
            if worker_type is None:
                raise checkpoints.CheckpointError(
                    "%s: isolated Cycle worker does not support %s; host execution is forbidden."
                    % (step.id, step.plugin))
            try:
                deployment, backend, effective = self.resolve(step)
                worker_type.validate(deployment, backend, effective)
                worker_type.validate_settings(step.settings, deployment)
                worker_type.validate_configuration(cycle, deployment)
            except RuntimePolicyError as exc:
                raise checkpoints.CheckpointError("%s runtime: %s" % (step.id, exc)) from exc
            binding = self.binding(deployment, effective, worker_type.contract)
            self.bindings[step.id] = binding
            self.workers[step.id] = worker_type(self, step, registry.get(step.plugin).metadata,
                                                 deployment, backend, effective, binding)

    def resolve(self, step):
        deployment = self.deployments.load(self.config["deployment"], self.config["revision"])
        backend = (self.backend_factory(deployment, self.protected) if self.backend_factory
                   else deployment.backend(self.protected))
        try:
            effective = deployment.resolve(backend, self.config["project_directory"],
                step.runtime.get("overrides", {}), self.profiles, cycle_overrides=self.config.get("overrides", {}))
        except RuntimePolicyError as exc:
            ExecutionRuntime(backend, audit=True)._event("runtime.rejected", code=exc.code, operation="resolve_cycle")
            raise
        return deployment, backend, effective

    def binding(self, deployment, effective, contract=WORKER):
        return {"worker": contract, "deployment": deployment.name, "revision": deployment.digest,
                "provider": effective.provider, "policy_digest": effective.digest,
                "effective_policy": effective.to_document()}

    def scope(self, scope):
        if not self.enabled:
            return scope
        scope = dict(scope, env={}, vars=dict(scope["vars"]))
        for name in self.cycle.secrets:
            scope["vars"].pop(name, None)
        return scope


class CommandWorker:
    """Only trusted orchestration runs here; all command code runs natively isolated."""

    contract = WORKER
    structured_output = False

    @classmethod
    def validate(cls, deployment, backend, policy):
        if policy.to_document()["policy"]["secrets"]:
            raise RuntimePolicyError("cycle_credential_delivery_unsupported", "This Cycle worker does not deliver credentials. Narrow its secret grants to an empty list.")

    @classmethod
    def validate_settings(cls, settings, deployment=None):
        pass

    @classmethod
    def validate_configuration(cls, cycle, deployment):
        pass

    def __init__(self, plan, step, metadata, deployment, backend, policy, binding):
        self.plan, self.step, self.metadata = plan, step, metadata
        self.deployment, self.backend, self.policy = deployment, backend, policy
        self.binding = copy.deepcopy(binding)
        self.runtime = ExecutionRuntime(backend, audit=True)
        self.handle = None
        self.info = dict(self.binding, enforcement="not_evaluated", cleanup_confirmed=True)
        self.attempt = 0

    def prepare(self, context, step):
        # No user plugin hook: actual preparation belongs to each native attempt.
        self.attempt = context.run.steps[step.id].attempts
        context.cancel.raise_if_set()

    def _request(self, context, step):
        deployment, _backend, effective = self.plan.resolve(self.step)
        if self.plan.binding(deployment, effective, self.contract) != self.binding:
            raise RuntimePolicyError("cycle_runtime_changed", "Execution policy changed after approval; start a reviewed run.")
        spec, settings = deployment.to_document()["spec"], step.settings
        command = settings.get("command")
        if isinstance(command, (list, tuple)):
            argv = command
        elif isinstance(command, str) and command.strip() and settings.get("shell", True) is True:
            if not spec.get("shell"):
                raise RuntimePolicyError("shell_not_configured", "The approved deployment must supply a shell argv prefix, or use a command list.")
            argv = spec["shell"] + [command]
        else:
            raise RuntimePolicyError("invalid_command", "Use an argv list for direct execution, or an explicitly configured deployment shell.")
        mapper = PathMapper(self.policy.platform)
        cwd = settings.get("dir") or spec["working_directory"]
        if not isinstance(cwd, str):
            raise RuntimePolicyError("invalid_path", "Working directory must be a path inside the runtime.")
        if not mapper.path_type(cwd).is_absolute():
            cwd = str(mapper.path_type(spec["working_directory"]) / cwd)
        cwd = mapper.absolute(cwd, "working_directory", allow_root=True)
        deadline = getattr(context, "runtime_deadline", None)
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            context.cancel.set("timed out before native preparation")
            context.cancel.raise_if_set()
        return ExecutionRequest(self.policy, argv, cwd, environment=settings.get("env", {}), timeout_seconds=remaining)

    def _stage(self, context, step, status):
        context.stage(step.id, {"kind": "runtime", "title": "Isolated execution", "status": status,
                              "body": {"runtime": copy.deepcopy(self.info)}})

    def execute(self, context, step):
        from execution_runtime.service import _Output
        self.attempt += 1
        self.info = dict(self.binding, attempt=self.attempt, enforcement="not_evaluated", cleanup_confirmed=True)
        result, outputs, artifacts = None, {}, []
        # A previous uncertain instance blocks the same run/node, including Resume.
        scope = self.backend.execution_scope("cycle", self.plan.workspace + "\0" + step.id)
        buffers = {"stdout": "", "stderr": ""}
        def emit(_kind, stream, text):
            if self.structured_output and stream == "stdout":
                return  # Worker protocol is data, never a user log or control event.
            pending = buffers[stream] + text
            while "\n" in pending or len(pending) >= 8192:
                end = min(pending.find("\n") + 1 if "\n" in pending else 8192, 8192)
                context.log(step.id, "out" if stream == "stdout" else "err", [pending[:end].rstrip("\n")])
                pending = pending[end:]
            buffers[stream] = pending
        output = _Output(emit)
        problem = ""
        try:
            with scope as owner:
                try:
                    context.cancel.raise_if_set()
                    request = self._request(context, step)
                    self.handle = self.runtime.prepare(request)
                    self.info.update(instance_id=self.handle.instance_id, cleanup_confirmed=False)
                    owner.bind(self.handle)
                    context.cancel.raise_if_set()
                    self.handle = self.runtime.start(self.handle)
                    self.info["enforcement"] = "verified"
                    self._stage(context, step, "running")
                    while True:
                        result = self.runtime.inspect(self.handle)
                        if result.get("gateway"):
                            self.info["gateway"] = result["gateway"]
                        output.drain(result)
                        if result["state"] in ("exited", "failed", "cleaned"):
                            break
                        if context.cancel.wait(0.02):
                            self.runtime.stop(self.handle, 2)
                            break
                    result = self.runtime.wait(self.handle)
                    output.drain(result, final=True)
                    if result.get("reason") == "timeout":
                        context.cancel.set("timed out in isolated runtime")
                finally:
                    if self.handle is not None:
                        try:
                            state = self.runtime.inspect(self.handle)["state"]
                            if state not in ("prepared", "cleaned", "exited", "failed"):
                                self.runtime.stop(self.handle, 0)
                        finally:
                            try:
                                self.runtime.cleanup(self.handle)
                                self.handle = None
                                self._finalize(context, step, owner)
                                owner.clear()
                                self.info["cleanup_confirmed"] = True
                            except Exception as exc:
                                error = checkpoints.CheckpointError("Isolated cleanup is unconfirmed; replacement execution is blocked.")
                                error.runtime = copy.deepcopy(self.info)
                                raise error from exc
                            finally:
                                if self.info["cleanup_confirmed"]:
                                    self.handle = None
        except RuntimePolicyError as exc:
            self.runtime._event("runtime.rejected", code=exc.code, operation="cycle_command")
            problem = "%s: %s" % (exc.code, exc)
        except Cancelled as exc:
            problem = str(exc)
        finally:
            for stream, pending in buffers.items():
                if pending:
                    context.log(step.id, "out" if stream == "stdout" else "err", [pending])
            self.info.update(exit_code=result.get("exit_code") if result else None,
                             reason=result.get("reason") if result else "not_started")
            if result and result.get("gateway"):
                self.info["gateway"] = result["gateway"]
            self._stage(context, step, "done" if self.info["cleanup_confirmed"] else "failed")
        if result:
            outputs, artifacts = self._collect(context, step, result)
        return self._result(step, result, problem, outputs, artifacts)

    def _finalize(self, context, step, owner):
        """Optional workload cleanup, in another isolated invocation after native reap."""

    def _result(self, step, result, problem, outputs, artifacts):
        expected = int(float(step.settings.get("expect_exit", 0)))
        ok = bool(result and result["reason"] == "exited" and (expected == -1 or result["exit_code"] == expected))
        return PluginResult(SUCCESS if ok and not problem else FAILED, outputs=outputs, artifacts=artifacts,
                            message=problem or ("exit %s" % result["exit_code"] if result else "not started"),
                            runtime=copy.deepcopy(self.info))

    def _collect(self, context, step, result):
        # Native log files are bounded and redacted by the backend. The run directory
        # is a trusted destination and is never a mount visible to the workload.
        directory = Path(context.step_dir(step.id)) / ("runtime-%04d" % self.attempt)
        directory.mkdir()
        outputs, artifacts = {"exit_code": result["exit_code"], "stdout_tail": "\n".join(result.get("stdout_tail", "").splitlines()[-40:])}, []
        for name, key in (("stdout.log", "stdout_path"), ("stderr.log", "stderr_path")):
            source = next((Path(one) for one in result["artifacts"] if Path(one).name == name), None)
            if source is None or not source.is_file():
                continue
            target = directory / name
            shutil.copyfile(source, target)
            outputs[key] = context.relative(str(target))
            artifacts.append(Artifact(path=outputs[key], type="log", producer=step.id, name=name, bytes=target.stat().st_size))
        receipt = directory / "runtime.json"
        receipt.write_text(json.dumps(self.info, indent=2) + "\n", encoding="utf-8")
        artifacts.append(Artifact(path=context.relative(str(receipt)), type="json", producer=step.id,
                                  name="runtime.json", bytes=receipt.stat().st_size))
        return outputs, artifacts

    def cleanup(self, context, step):
        if self.handle is not None:
            try:
                self.runtime.stop(self.handle, 0)
            finally:
                self.runtime.cleanup(self.handle)
