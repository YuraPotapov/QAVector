"""Versioned, core-owned workload artifacts; native backends pin them into images."""

import hashlib
from pathlib import Path

from ..errors import RuntimePolicyError
from ..paths import PathMapper

GIT_CONTRACT = "qavector.git-checkout/v1"
AGENT_CONTRACT = "qavector.agent-review/v1"


def git_artifact(platform):
    return artifact(platform, "git_checkout_linux.py")


def agent_artifact(platform):
    return artifact(platform, "agent_review_linux.py")


def claude_artifact(platform):
    return artifact(platform, "claude_review_linux.py")


def artifact(platform, name):
    if platform != "linux":
        raise RuntimePolicyError("worker_platform_unsupported", "No native workload worker artifact is available for this platform yet.")
    path = Path(__file__).with_name(name)
    try:
        return path, hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        raise RuntimePolicyError("worker_artifact_unavailable", "The core package is missing its workload worker artifact.") from None


def validate_git_worker(deployment, backend):
    config = deployment.to_document()["spec"].get("workers", {}).get("git.checkout")
    if config is None:
        raise RuntimePolicyError("git_worker_not_configured", "The approved deployment must provide a Git checkout worker.")
    mapper = PathMapper(backend.platform)
    for path in (config["entrypoint"], config["argv"][0], config["git"]):
        mapper.absolute(path, "worker.path")
        backend.runtime_file_identity(path)
    _, expected = git_artifact(backend.platform)
    if backend.runtime_file_identity(config["entrypoint"]) != expected:
        raise RuntimePolicyError("worker_artifact_mismatch", "The approved image does not contain this core's versioned Git worker artifact.")
    return config


def worker_status(deployment, backend):
    result = {"command.shell": {"contract": "qavector.command-shell/v1", "available": True},
              "git.checkout": {"contract": GIT_CONTRACT, "available": False}}
    result["agent.review"] = {"contract": AGENT_CONTRACT, "available": False}
    for name, validate in (("git.checkout", validate_git_worker), ("agent.review", validate_agent_worker)):
        try:
            config = validate(deployment, backend)
            result[name]["available"] = True
            if name == "agent.review":
                result[name]["credentials"] = config.get("credentials", {})
                if config.get("transport") == "claude_cli":
                    from ..gateway import ProviderGateway
                    result[name]["gateway"] = ProviderGateway(deployment.to_document()["spec"]["provider_gateways"][config["gateway"]]).summary()
                    result[name]["credential_delivery"] = "core_oauth_header"
        except RuntimePolicyError as exc:
            result[name]["problem"] = exc.to_document()
    return result


def validate_agent_worker(deployment, backend):
    config = deployment.to_document()["spec"].get("workers", {}).get("agent.review")
    if config is None:
        raise RuntimePolicyError("agent_worker_not_configured", "The approved deployment must provide an agent review worker.")
    mapper = PathMapper(backend.platform)
    bridge = config.get("transport") == "claude_cli"
    paths = (config["entrypoint"], config["argv"][0], config["claude"]) if bridge else (config["entrypoint"], config["argv"][0], config["provider_argv"][0], config["provider_entrypoint"])
    for path in paths:
        mapper.absolute(path, "worker.path")
        backend.runtime_file_identity(path)
    if backend.runtime_file_identity(config["entrypoint"]) != (claude_artifact if bridge else agent_artifact)(backend.platform)[1]:
        raise RuntimePolicyError("worker_artifact_mismatch", "The approved image does not contain this core's versioned agent review artifact.")
    if config.get("credentials") and not bridge:
        backend.secret_file_locations(config["credentials"].values())
    return config
