"""Read-only reviews through a deployment-owned JSON provider or isolated CLI bridge."""

import copy
from pathlib import Path
import re
import time

from domain.cycle import Artifact, FAILED, SUCCESS
from execution_runtime._json import canonical, object_fields, parse_document
from execution_runtime.backends.base import ExecutionRequest
from execution_runtime.errors import RuntimePolicyError
from execution_runtime.paths import PathMapper
from execution_runtime.secrets import SecretValues
from execution_runtime.workers import AGENT_CONTRACT, validate_agent_worker

from . import checkpoints
from .operations import Operation, result_document, result_from_document
from .registry import PluginResult
from .runtime import CommandWorker


class AgentReviewWorker(CommandWorker):
    contract = AGENT_CONTRACT
    structured_output = True

    @classmethod
    def validate_configuration(cls, cycle, deployment):
        required = set(deployment.to_document()["spec"]["workers"]["agent.review"].get("credentials", {}).values())
        if not required <= set(cycle.secrets):
            raise RuntimePolicyError("review_credential_not_declared", "Required provider credential references must be declared as Cycle secrets.")

    @classmethod
    def validate_settings(cls, settings, deployment=None):
        framework = settings.get("framework", "claude_cli")
        bridge = deployment is not None and deployment.to_document()["spec"]["workers"]["agent.review"].get("transport") == "claude_cli"
        if not isinstance(framework, str) or "${" not in framework and framework not in (("runtime", "claude_cli") if bridge else ("runtime",)):
            raise RuntimePolicyError("review_framework_unsupported", "Isolated review requires framework runtime, or claude_cli with an approved deployment-owned gateway.")

    def prepare(self, context, step):
        previous = getattr(context, "earlier", {}).get(step.id)
        self.previous_runtime = copy.deepcopy(previous[1].runtime) if previous else None
        super().prepare(context, step)

    @classmethod
    def validate(cls, deployment, backend, policy):
        config = validate_agent_worker(deployment, backend)
        if config.get("transport") == "claude_cli":
            from execution_runtime.gateway import ProviderGateway
            route = ProviderGateway(deployment.to_document()["spec"]["provider_gateways"][config["gateway"]])
            route.authorize(policy)
        if set(config.get("credentials", {}).values()) != set(policy.to_document()["policy"]["secrets"]):
            raise RuntimePolicyError("review_credential_scope_mismatch", "Review secret grants must match the approved provider credential references exactly.")
        if any(one["access"] != "read_only" for one in policy.to_document()["resolved_filesystem"]):
            raise RuntimePolicyError("review_write_access_forbidden", "Agent reviews require read-only filesystem grants. Narrow this node's mounts first.")
        if not policy.to_document()["policy"]["resources"].get("scratch_bytes"):
            raise RuntimePolicyError("review_scratch_required", "The approved review environment must grant a bounded scratch capacity for provider protocol data.")
        if config.get("transport") == "claude_cli" and policy.policy.to_document()["resources"]["scratch_bytes"] < 8 * 1024 ** 2:
            raise RuntimePolicyError("claude_scratch_required", "The isolated CLI needs at least 8 MiB of approved scratch capacity.")

    def _request(self, context, step):
        deployment, backend, effective = self.plan.resolve(self.step)
        config = validate_agent_worker(deployment, backend)
        self.validate(deployment, backend, effective)
        if self.plan.binding(deployment, effective, self.contract) != self.binding:
            raise RuntimePolicyError("cycle_runtime_changed", "Execution policy changed after approval; start a reviewed run.")
        settings = step.settings
        object_fields(settings, ("framework", "model", "task", "repository", "inputs", "files", "assess", "scope", "verdict",
                                "claude", "python", "model_client", "model_options", "api_key_env", "effort", "min_effort", "max_iterations"), "agent.review", ("model", "task"))
        bridge = config.get("transport") == "claude_cli"
        if settings.get("framework", "claude_cli") not in (("runtime", "claude_cli") if bridge else ("runtime",)):
            raise RuntimePolicyError("review_framework_unsupported", "Isolated review requires framework runtime and a deployment-owned JSON provider; host CLI/framework discovery is unavailable.")
        if any(settings.get(key) not in (None, "", {}) for key in ("claude", "python", "model_client", "model_options", "api_key_env", "effort", "min_effort", "max_iterations")):
            raise RuntimePolicyError("review_provider_override_forbidden", "Provider tools, credentials and options belong to the approved deployment, not node settings.")
        for key in ("task", "model"):
            if not isinstance(settings[key], str) or not settings[key].strip() or "\0" in settings[key] or len(settings[key]) > 32768:
                raise RuntimePolicyError("invalid_review_text", "Review task/model must be bounded resolved text.")
        if settings.get("assess", "") not in ("", "complexity") or any(type(settings.get(key, False)) is not bool for key in ("scope", "verdict")):
            raise RuntimePolicyError("invalid_review_options", "Use explicit review assessment and boolean scope/verdict options.")
        inputs, files = settings.get("inputs", {}), settings.get("files", [])
        if not isinstance(inputs, dict) or not isinstance(files, list) or len(files) > 16:
            raise RuntimePolicyError("invalid_review_evidence", "Review inputs must be a mapping and files a list of at most 16 paths.")
        mapper = PathMapper(self.policy.platform)
        spec = deployment.to_document()["spec"]
        project = next(one.target for one in self.policy.bindings if one.name == "project")
        mounts = self.policy.to_document()["resolved_filesystem"]
        def path(value):
            if not isinstance(value, str) or not value or len(value) > 4096:
                raise RuntimePolicyError("invalid_review_path", "Review paths must name files/directories inside the runtime.")
            native = mapper.path_type(value)
            native = mapper.path_type(mapper.absolute(str(native if native.is_absolute() else mapper.path_type(project) / native), "review.path"))
            if not any(mapper.path_type(one["target"]) == native or mapper.path_type(one["target"]) in native.parents for one in mounts):
                raise RuntimePolicyError("review_read_denied", "Review evidence must be within an approved filesystem grant.")
            return str(native)
        repository = path(settings["repository"]) if settings.get("repository") else ""
        cwd = repository or mapper.absolute(spec["working_directory"], "working_directory", allow_root=True)
        data = dict(contract="qavector.review-provider/v1", task=settings["task"], model=settings["model"], repository=repository,
                    inputs=inputs, files=[path(one) for one in files], assess=settings.get("assess", ""),
                    scope=settings.get("scope", False), verdict=settings.get("verdict", False), clarifications=copy.deepcopy(self.snapshot.items))
        credentials = config.get("credentials", {})
        values = {}
        if credentials:
            self.validate_configuration(context.cycle, deployment)
            for reference in credentials.values():
                value = getattr(context, "secrets", {}).get(reference)
                if not isinstance(value, str) or not value:
                    raise RuntimePolicyError("review_credential_unavailable", "A required provider credential is unavailable in the Cycle secret store.")
                values[reference] = value
            locations = {} if bridge else backend.secret_file_locations(values)
            mapper = PathMapper(self.policy.platform)
            if not bridge and set(locations) != set(values):
                raise RuntimePolicyError("invalid_secret_locations", "The native backend did not describe the requested secret files.")
            if not bridge:
                data.update(contract="qavector.review-provider/v2", credential_files={alias: mapper.absolute(locations[reference], "credential_file") for alias, reference in credentials.items()})
            self.info.update(credential_references=copy.deepcopy(credentials), credential_delivery="core_oauth_header" if bridge else "native_read_only_files")
        channel = None
        if bridge:
            from execution_runtime.gateway import ProviderGateway
            from execution_runtime.gateway_session import GatewaySession
            from .plugins.agent import _claude_cli_prompt
            from .plugins.agent_run import with_clarifications
            route = ProviderGateway(spec["provider_gateways"][config["gateway"]])
            channel = GatewaySession(route, self.policy, settings["model"], SecretValues(values), getattr(context, "runtime_deadline", None))
            self.info["gateway"] = channel.summary()
            prompt = _claude_cli_prompt(settings["task"], inputs, [], repository, data["assess"] == "complexity", data["scope"], data["verdict"])
            prompt = prompt.replace("\n# Answer with", "\n{{QAVECTOR_NATIVE_EVIDENCE}}\n# Answer with", 1)
            prompt = with_clarifications(prompt, self.snapshot)
            payload = canonical(dict(contract=self.contract, claude=config["claude"], cwd=cwd, model=settings["model"],
                prompt=prompt, files=data["files"], capability=channel.capability,
                max_requests=route.to_document().get("max_requests", 4), request_bytes=route.to_document().get("request_bytes", 524288)))
        else:
            payload = canonical(dict(contract=self.contract, provider=config["provider_argv"], cwd=cwd, request=data))
        if len(payload.encode("utf-8")) > 65536:
            raise RuntimePolicyError("review_packet_too_large", "The review request exceeds its protocol bound.")
        deadline = getattr(context, "runtime_deadline", None)
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            context.cancel.set("timed out before review preparation")
            context.cancel.raise_if_set()
        return ExecutionRequest(self.policy, config["argv"] + [payload], cwd, timeout_seconds=remaining,
                                secret_values=SecretValues() if bridge else SecretValues(values), gateway=channel)

    def execute(self, context, step):
        inbox = context.inbox(step.id)
        self.snapshot = inbox.take("runtime-review", last=True)
        operation = Operation(context, step, "runtime-review", {"binding": self.binding, "settings": step.settings, "messages": self.snapshot.digest})
        cached = operation.cached()
        if cached is not None:
            if cached.get("input_delivered"):
                inbox.delivered(self.snapshot, context.relative(operation.directory))
            else:
                inbox.release(self.snapshot, "The isolated review did not start.")
            return result_from_document(cached["result"])
        # An unknown local/provider call never silently becomes a new call.
        try:
            operation.begin()
        except checkpoints.CheckpointError as exc:
            exc.runtime = copy.deepcopy(self.info if "instance_id" in self.info else self.previous_runtime or self.info)
            raise
        inbox.bind(self.snapshot, context.relative(operation.directory))
        self.review = None
        result = super().execute(context, step)
        if self.review is not None:
            from .plugins.agent import counts
            directory = Path(context.workspace) / next(one.path for one in result.artifacts if one.name == "runtime.json")
            report = directory.with_name("review.json")
            report.write_text(canonical(self.review) + "\n", encoding="utf-8")
            relative = context.relative(str(report))
            result.outputs = dict(self.review, **counts(self.review), out_of_scope_count=len(self.review.get("out_of_scope", [])),
                                  effort="", report_path=relative)
            result.artifacts.append(Artifact(path=relative, type="json", producer=step.id, name="review", bytes=report.stat().st_size))
        not_started = result.runtime.get("reason") == "not_started" and result.runtime.get("cleanup_confirmed")
        if result.runtime.get("gateway", {}).get("calls") == 0 and result.runtime.get("cleanup_confirmed"):
            not_started = True
        if self.review is not None or not_started:
            delivered = self.review is not None
            operation.complete({"result": result_document(result), "input_delivered": delivered}, result.ok, input_delivered=delivered)
            if delivered:
                inbox.delivered(self.snapshot, context.relative(operation.directory))
            else:
                inbox.release(self.snapshot, "The isolated review did not start.")
        return result

    def _result(self, step, result, problem, outputs, artifacts):
        message = problem
        if result is not None and not problem:
            try:
                source = next(Path(one) for one in result["artifacts"] if Path(one).name == "stdout.log")
                with source.open("rb") as stream:
                    raw = stream.read(65537)
                if len(raw) > 65536:
                    raise RuntimePolicyError("review_packet_too_large", "Review result exceeds its protocol bound.")
                value = parse_document(raw)
                object_fields(value, ("contract", "review", "code"), "review_result", ("contract", "review", "code"))
                if value["contract"] != self.contract or not isinstance(value["code"], str) or value["code"] and not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", value["code"]):
                    raise RuntimePolicyError("invalid_review_result", "Review worker returned invalid protocol data.")
                if result["reason"] == "exited" and result["exit_code"] == 0 and not value["code"]:
                    from .plugins.agent_worker import validate_review
                    self.review = validate_review(value["review"], step.settings.get("assess") == "complexity",
                                                  step.settings.get("scope", False), step.settings.get("verdict", False))
                    # Keep only issue data, never extra provider-supplied fields.
                    keys = ("severity", "description", "file", "line", "fixable")
                    self.review["issues"] = [{k: v for k, v in one.items() if k in keys} for one in self.review["issues"]]
                    message = "isolated review: %s" % self.review["risk"]
                else:
                    message = value["code"] or "isolated review failed"
            except (OSError, StopIteration, ValueError, RuntimePolicyError) as exc:
                message = "Review result unavailable: %s" % (exc.code if isinstance(exc, RuntimePolicyError) else "invalid_review")
        return PluginResult(SUCCESS if self.review is not None else FAILED, artifacts=artifacts,
                            message=message or "isolated review did not start", runtime=copy.deepcopy(self.info))
