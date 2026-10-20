"""Git checkout data adapter. All filesystem/Git/cleanup work belongs to the image."""

import copy
from pathlib import Path
import re
import time
import uuid

from execution_runtime._json import canonical, object_fields, parse_document
from execution_runtime.backends.base import ExecutionRequest
from execution_runtime.errors import RuntimePolicyError
from execution_runtime.paths import PathMapper, relative_path
from execution_runtime.workers import GIT_CONTRACT, validate_git_worker

from . import checkpoints
from .registry import PluginResult
from .runtime import CommandWorker
from domain.cycle import FAILED, SUCCESS


def worker_result(result):
    path = next((Path(one) for one in result["artifacts"] if Path(one).name == "stdout.log"), None)
    if path is None:
        raise RuntimePolicyError("worker_result_missing", "The Git worker did not produce its result.")
    with path.open("rb") as stream:
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise RuntimePolicyError("worker_result_too_large", "Git worker result exceeds its protocol bound.")
    value = parse_document(raw)
    object_fields(value, ("contract", "status", "outputs", "code"), "worker_result", ("contract", "status", "outputs", "code"))
    if (value["contract"] != GIT_CONTRACT or value["status"] not in ("success", "failed") or
            not isinstance(value["outputs"], dict) or not isinstance(value["code"], str) or
            value["code"] and not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", value["code"])):
        raise RuntimePolicyError("invalid_worker_result", "Git worker returned an invalid protocol result.")
    return value


class GitCheckoutWorker(CommandWorker):
    contract = GIT_CONTRACT
    structured_output = True

    @classmethod
    def validate(cls, deployment, backend, policy):
        super().validate(deployment, backend, policy)
        validate_git_worker(deployment, backend)

    def prepare(self, context, step):
        previous = getattr(context, "earlier", {}).get(step.id)
        if previous and previous[1].runtime and not previous[1].runtime.get("cleanup_confirmed", True):
            raise checkpoints.CheckpointError("Previous Git workspace cleanup is unconfirmed. Review retained evidence before starting a new run.")
        super().prepare(context, step)

    def _request(self, context, step):
        deployment, backend, effective = self.plan.resolve(self.step)
        self.validate(deployment, backend, effective)
        if self.plan.binding(deployment, effective, self.contract) != self.binding:
            raise RuntimePolicyError("cycle_runtime_changed", "Execution policy changed after approval; start a reviewed run.")
        spec, settings = deployment.to_document()["spec"], step.settings
        object_fields(settings, ("repository", "branch", "commit", "path", "depth", "submodules"), "git.checkout", ("repository",))
        mapper = PathMapper(self.policy.platform)
        base = next(one.target for one in self.policy.bindings if one.name == "project")
        source = settings["repository"]
        if (not isinstance(source, str) or not source or len(source) > 4096 or "://" in source or source.startswith("file:") or
                re.match(r"[^/\\]+@[^/\\]+:", source)):
            raise RuntimePolicyError("local_git_repository_required", "This worker accepts local repository paths in approved mounts; remote transports are unavailable.")
        source = str(mapper.path_type(source) if mapper.path_type(source).is_absolute() else mapper.path_type(base) / source)
        source = mapper.absolute(source, "git.repository")
        destination = relative_path(settings.get("path") or "source/" + step.id, "git.path")
        if len(destination) > 4096 or destination == "." or any(part.lower() == ".git" or part.startswith(".qavector-checkout-") for part in destination.split("/")):
            raise RuntimePolicyError("invalid_checkout_path", "Choose a new checkout below the approved project, outside Git/control directories.")
        target = mapper.path_type(base).joinpath(*destination.split("/"))
        mapper.absolute(str(target), "git.destination")
        mounts = self.policy.to_document()["resolved_filesystem"]
        def contains(mount, path):
            root = mapper.path_type(mount["target"])
            return root == path or root in path.parents
        if not any(contains(one, target.parent) and one["access"] == "read_write" for one in mounts):
            raise RuntimePolicyError("git_checkout_write_denied", "Git checkout requires write access to the destination's parent and its staging directory.")
        if not any(contains(one, mapper.path_type(source)) for one in mounts):
            raise RuntimePolicyError("git_repository_read_denied", "The local repository must be in an approved filesystem grant.")
        depth = settings.get("depth", 0)
        try:
            full_copy = not isinstance(depth, bool) and float(depth) == 0
        except (TypeError, ValueError, OverflowError):
            full_copy = False
        if not full_copy:
            raise RuntimePolicyError("local_clone_depth_unsupported", "Local-copy checkouts require depth 0; shallow transport is not available yet.")
        if settings.get("submodules", False) is not False:
            raise RuntimePolicyError("git_submodules_unsupported", "Submodule initialization requires a separate reviewed transport worker.")
        for key in ("branch", "commit"):
            if not isinstance(settings.get(key, ""), str) or len(settings.get(key, "")) > 4096 or "\0" in settings.get(key, ""):
                raise RuntimePolicyError("invalid_git_reference", "Git branch and commit must be resolved text.")
        config = spec["workers"]["git.checkout"]
        self.payload = dict(contract=self.contract, operation="checkout", nonce=uuid.uuid4().hex, base=base,
            repository=source, path=destination, branch=settings.get("branch", ""), commit=settings.get("commit", ""), depth=0, git=config["git"])
        packet = canonical(self.payload)
        if len(packet.encode("utf-8")) > 16384:
            raise RuntimePolicyError("worker_input_too_large", "The Git checkout request exceeds its protocol bound.")
        self.prefix = config["argv"]
        self.info.update(checkout_nonce=self.payload["nonce"], workspace=str(target), workspace_cleanup_confirmed=False,
                         staging_directory=str(target.parent / (".qavector-checkout-" + self.payload["nonce"])))
        deadline = getattr(context, "runtime_deadline", None)
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            context.cancel.set("timed out before Git worker preparation")
            context.cancel.raise_if_set()
        return ExecutionRequest(self.policy, self.prefix + [packet], base, timeout_seconds=remaining)

    def _finalize(self, context, step, owner):
        # Reap the first namespace before starting cleanup: a killed Git process
        # must not still be writing to a directory the cleanup process removes.
        payload = dict(self.payload, operation="cleanup")
        request = ExecutionRequest(self.policy, self.prefix + [canonical(payload)], self.payload["base"], timeout_seconds=3)
        self.handle = self.runtime.prepare(request)
        self.info["cleanup_instance_id"] = self.handle.instance_id
        owner.bind(self.handle)
        try:
            self.handle = self.runtime.start(self.handle)
            result = self.runtime.wait(self.handle)
            value = worker_result(result)
            if result["reason"] != "exited" or result["exit_code"] != 0 or value["status"] != "success" or value["outputs"] or value["code"]:
                raise RuntimePolicyError("git_cleanup_unconfirmed", "The isolated Git worker could not confirm attempt-owned workspace cleanup.")
            self.info["workspace_cleanup_confirmed"] = True
        finally:
            try:
                state = self.runtime.inspect(self.handle)["state"]
                if state not in ("prepared", "exited", "failed", "cleaned"):
                    self.runtime.stop(self.handle, 0)
            finally:
                self.runtime.cleanup(self.handle)
                self.handle = None

    def _result(self, step, result, problem, outputs, artifacts):
        values, message, ok = {}, problem, False
        if result is not None and not problem:
            try:
                value = worker_result(result)
                if result["reason"] == "exited" and result["exit_code"] == 0 and value["status"] == "success" and not value["code"]:
                    object_fields(value["outputs"], ("commit", "branch"), "git.outputs", ("commit", "branch"))
                    revision, branch = value["outputs"]["commit"], value["outputs"]["branch"]
                    if (not isinstance(revision, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", revision) or
                            not isinstance(branch, str) or not branch or len(branch) > 1024 or any(c in branch for c in ("\n", "\r", "\0"))):
                        raise RuntimePolicyError("invalid_git_result", "Git worker returned invalid commit/branch evidence.")
                    values, ok = dict(workspace=self.info["workspace"], commit=revision, branch=branch), True
                    message = "checked out %s" % revision[:12]
                else:
                    message = value["code"] or "isolated Git checkout failed"
            except (OSError, RuntimePolicyError) as exc:
                message = "Git worker result unavailable: %s" % (exc.code if isinstance(exc, RuntimePolicyError) else "io_error")
        return PluginResult(SUCCESS if ok else FAILED, outputs=values, artifacts=artifacts,
                            message=message or "isolated Git checkout did not start", runtime=copy.deepcopy(self.info))
