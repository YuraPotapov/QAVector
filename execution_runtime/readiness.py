"""Local-only worker readiness: no credential access or provider transport."""

from ._json import object_fields
from .deployments import RuntimeDeployment
from .errors import RuntimePolicyError
from .gateway import ProviderGateway


def check(request, deployments, profiles):
    object_fields(request, ("deployment", "worker", "model"), "readiness", ("deployment", "worker", "model"))
    if request["worker"] != "agent.review":
        raise RuntimePolicyError("unsupported_readiness_worker", "This readiness check supports the Claude review worker.")
    if not isinstance(request["model"], str) or not request["model"] or len(request["model"]) > 256:
        raise RuntimePolicyError("invalid_gateway_model", "Choose the exact approved full model name.")
    deployment = RuntimeDeployment(request["deployment"])
    backend = deployment.backend((str(deployments.directory), str(profiles.directory)))
    spec = deployment.to_document()["spec"]
    worker = spec.get("workers", {}).get("agent.review", {})
    if worker.get("transport") != "claude_cli":
        raise RuntimePolicyError("claude_worker_required", "Choose an environment with the pinned Claude CLI worker.")
    project = next((one["source"] for one in spec["bindings"] if one["name"] == "project"), "")
    policy = deployment.resolve(backend, project, profiles=profiles)
    route = ProviderGateway(spec["provider_gateways"][worker["gateway"]])
    route.authorize(policy, request["model"])
    from cycle.runtime_agent import AgentReviewWorker
    AgentReviewWorker.validate(deployment, backend, policy)
    report = backend.gateway_preflight(policy, route).to_document()
    return {"ok":True, "ready":report["ready"], "revision":deployment.digest,
        "preflight":report, "gateway":route.summary(), "model":request["model"],
        "credential_reference":route.to_document()["credential"], "credential_access":"not_read",
        "provider_request":"not_sent", "account_acceptance":"not_verified",
        "execution":"not_started", "enforcement":"not_evaluated"}
