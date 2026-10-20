"""Trusted, content-pinned execution deployments; service requests may only narrow them."""

from dataclasses import dataclass
from pathlib import Path
import re

from ._json import Snapshot, object_fields, read_document
from .backends.registry import BackendRegistry
from .errors import RuntimePolicyError
from .paths import PathMapper
from .policy import PolicyLayer, PolicyPatch, PolicyResolver
from .registry import EnvironmentRegistry, _atomic, profile_name, revision_name


def default_directory():
    import runtime_paths
    return str(Path(runtime_paths.user_data_root()) / "execution-deployments")


@dataclass(frozen=True, init=False)
class RuntimeDeployment(Snapshot):
    def __init__(self, document):
        object_fields(document, ("api_version", "kind", "metadata", "spec"), "deployment",
                      ("api_version", "kind", "metadata", "spec"))
        if document["api_version"] != "qavector.io/v1" or document["kind"] != "RuntimeDeployment":
            raise RuntimePolicyError("unsupported_schema", "Expected qavector.io/v1 RuntimeDeployment.")
        meta, spec = document["metadata"], document["spec"]
        object_fields(meta, ("name", "description"), "metadata", ("name",))
        profile_name(meta["name"])
        if not isinstance(meta.get("description", ""), str) or len(meta.get("description", "")) > 4096:
            raise RuntimePolicyError("invalid_description", "Deployment description must be bounded text.")
        object_fields(spec, ("provider", "policy", "bindings", "backend_options", "working_directory", "environment", "shell", "workers", "provider_gateways"),
                      "spec", ("provider", "policy", "bindings", "backend_options", "working_directory"))
        if spec["provider"] not in ("auto", "linux-isolated", "linux-system-isolated", "windows-isolated"):
            raise RuntimePolicyError("unknown_provider", "Unknown deployment provider.")
        PolicyPatch(spec["policy"])
        if not isinstance(spec["bindings"], list) or not isinstance(spec["backend_options"], dict):
            raise RuntimePolicyError("invalid_deployment", "Expected trusted bindings and namespaced backend options.")
        if not isinstance(spec["working_directory"], str) or not spec["working_directory"] or "\0" in spec["working_directory"]:
            raise RuntimePolicyError("invalid_deployment", "Expected an explicit workload working directory.")
        if "shell" in spec and (not isinstance(spec["shell"], list) or not spec["shell"] or
                any(not isinstance(one, str) or not one or "\0" in one for one in spec["shell"])):
            raise RuntimePolicyError("invalid_deployment", "Expected an explicit shell argv prefix supplied by the deployment owner.")
        if "environment" in spec:
            object_fields(spec["environment"], ("name", "revision"), "environment", ("name", "revision"))
            profile_name(spec["environment"]["name"])
            revision_name(spec["environment"]["revision"])
        workers = spec.get("workers", {})
        gateways = spec.get("provider_gateways", {})
        if not isinstance(gateways, dict) or len(gateways) > 8:
            raise RuntimePolicyError("invalid_provider_gateways", "Expected at most eight named provider gateway profiles.")
        for gateway_name, gateway in gateways.items():
            profile_name(gateway_name)
            from .gateway import ProviderGateway
            ProviderGateway(gateway)
        object_fields(workers, ("git.checkout", "agent.review"), "spec.workers")
        for name, value in workers.items():
            bridge = name == "agent.review" and isinstance(value, dict) and value.get("transport") == "claude_cli"
            extra = ("git",) if name == "git.checkout" else ("claude", "gateway", "transport") if bridge else ("provider_argv", "provider_entrypoint")
            fields = ("contract", "entrypoint", "argv") + extra
            object_fields(value, fields + (("credentials",) if name == "agent.review" else ()), "worker", fields)
            contract = "qavector.git-checkout/v1" if name == "git.checkout" else "qavector.agent-review/v1"
            if value["contract"] != contract:
                raise RuntimePolicyError("unsupported_worker_contract", "Unknown workload worker contract.")
            if (not isinstance(value["argv"], list) or not 2 <= len(value["argv"]) <= 32 or
                    any(not isinstance(one, str) or not one or "\0" in one or len(one) > 4096 for one in value["argv"]) or
                    value["argv"][-1] != value["entrypoint"] or
                    any(not isinstance(value[key], str) or not value[key] or "\0" in value[key] for key in ("entrypoint",) + (extra if name == "git.checkout" else ("claude",) if bridge else ("provider_entrypoint",)))):
                raise RuntimePolicyError("invalid_worker", "Expected an approved interpreter prefix ending in the worker entrypoint, and explicit native tool paths.")
            if name == "agent.review" and not bridge and (not isinstance(value["provider_argv"], list) or not 1 <= len(value["provider_argv"]) <= 32 or
                    any(not isinstance(one, str) or not one or "\0" in one or len(one) > 4096 for one in value["provider_argv"]) or
                    value["provider_argv"][-1] != value["provider_entrypoint"]):
                raise RuntimePolicyError("invalid_worker", "Expected a pinned provider argv prefix ending in its entrypoint.")
            if name == "agent.review":
                credentials = value.get("credentials", {})
                if (not isinstance(credentials, dict) or len(credentials) > 8 or
                        any(not isinstance(alias, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", alias) for alias in credentials)):
                    raise RuntimePolicyError("invalid_worker_credentials", "Expected at most eight provider aliases mapped to secret grant IDs.")
                PolicyPatch({"secrets": list(credentials.values())})
                if bridge:
                    from .gateway import STREAM_CONTRACT
                    profile_name(value["gateway"])
                    route = gateways.get(value["gateway"], {})
                    if route.get("contract") != STREAM_CONTRACT or credentials != {"oauth_token": route.get("credential")}:
                        raise RuntimePolicyError("invalid_claude_gateway", "The Claude worker must reference a v2 gateway and its exact OAuth secret mapping.")
        super().__init__(document)

    @property
    def name(self):
        return self.to_document()["metadata"]["name"]

    def backend(self, protected_paths=()):
        spec = self.to_document()["spec"]
        return BackendRegistry().select(spec["provider"]).configure(spec["backend_options"], protected_paths)

    def resolve(self, backend, project_directory, overrides=None, profiles=None, cycle_overrides=None):
        spec = self.to_document()["spec"]
        mapper = PathMapper(backend.platform)
        bindings = mapper.bindings(spec["bindings"])
        project = next((binding for binding in bindings if binding.name == "project"), None)
        if project is None or mapper.path_type(mapper.absolute(project_directory, "project_directory")) != mapper.path_type(project.source):
            raise RuntimePolicyError("service_project_mismatch", "This environment is not approved for the selected project directory.")
        layers = [PolicyLayer("platform", PolicyPatch(spec["policy"]))]
        if cycle_overrides is not None:
            layers.append(PolicyLayer("cycle", PolicyPatch(cycle_overrides)))
        revision = ""
        if "environment" in spec:
            reference = spec["environment"]
            record = (profiles or EnvironmentRegistry()).load(reference["name"], reference["revision"])
            if record.profile.provider not in ("auto", backend.provider):
                raise RuntimePolicyError("provider_profile_conflict", "Profile and deployment providers must agree.")
            layers.append(PolicyLayer("environment", record.profile.policy))
            revision = record.revision
        layers.append(PolicyLayer("node", PolicyPatch({} if overrides is None else overrides)))
        return PolicyResolver().resolve(layers, spec["bindings"], platform=backend.platform, provider=backend.provider,
            profile_revision=revision, runtime_root_identity=backend.runtime_root_identity())


class DeploymentRegistry:
    def __init__(self, directory=None):
        self.directory = Path(default_directory() if directory is None else directory).absolute()

    def _path(self, name):
        profile_name(name)
        path = self.directory / (name + ".json")
        if self.directory.is_symlink() or path.is_symlink():
            raise RuntimePolicyError("unsafe_deployment_path", "Deployment storage cannot use symlinks.")
        return path

    def load(self, name, revision=None):
        path = self._path(name)
        if not path.is_file():
            raise RuntimePolicyError("deployment_not_found", "Execution environment was not found.")
        raw = read_document(path)
        object_fields(raw, ("revision", "deployment"), "deployment_record", ("revision", "deployment"))
        deployment = RuntimeDeployment(raw["deployment"])
        if deployment.name != name or raw["revision"] != deployment.digest:
            raise RuntimePolicyError("deployment_integrity_failed", "Deployment content differs from its recorded revision.")
        if revision is not None and revision_name(revision) != deployment.digest:
            raise RuntimePolicyError("deployment_revision_conflict", "Execution environment changed; review it before starting.")
        return deployment

    def list(self):
        if self.directory.is_symlink():
            raise RuntimePolicyError("unsafe_deployment_path", "Deployment storage cannot use symlinks.")
        if not self.directory.exists():
            return ()
        return tuple(self.load(path.stem) for path in sorted(self.directory.glob("*.json")))

    def save(self, deployment, expected_revision=None):
        if not isinstance(deployment, RuntimeDeployment):
            raise RuntimePolicyError("invalid_deployment", "Expected a validated runtime deployment.")
        path = self._path(deployment.name)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = self.directory / ("." + deployment.name + ".writer-lock")
        try:
            lock.mkdir(mode=0o700)
        except FileExistsError:
            raise RuntimePolicyError("registry_busy", "Another deployment writer is active.") from None
        try:
            current = self.load(deployment.name).digest if path.exists() else ""
            if current and expected_revision is None:
                raise RuntimePolicyError("revision_required", "Updating a deployment requires its reviewed revision.")
            if expected_revision is not None and expected_revision != current:
                raise RuntimePolicyError("deployment_revision_conflict", "Execution environment changed; reload before saving.")
            _atomic(path, {"revision": deployment.digest, "deployment": deployment.to_document()})
        finally:
            lock.rmdir()
        return deployment
