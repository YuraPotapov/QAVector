"""OS-independent runtime lifecycle and capability negotiation."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from dataclasses import field
import math
import re

from .._json import Snapshot
from ..errors import RuntimePolicyError
from ..secrets import SecretValues

CONTRACT_VERSION = 1


@dataclass(frozen=True)
class Capability:
    name: str
    status: str
    reason: str = ""

    def __post_init__(self):
        if (not isinstance(self.name, str) or not self.name or not isinstance(self.reason, str)
                or self.status not in ("verified", "unverified", "unsupported")):
            raise RuntimePolicyError("invalid_capability", "Unknown capability enforcement state.")

    def to_document(self):
        return {"name": self.name, "status": self.status, "reason": self.reason}


@dataclass(frozen=True)
class CapabilitySet:
    provider: str
    platform: str
    features: tuple
    observations: Snapshot
    contract_version: int = CONTRACT_VERSION

    def __post_init__(self):
        object.__setattr__(self, "features", tuple(self.features))
        if (any(not isinstance(one, Capability) for one in self.features)
                or not isinstance(self.observations, Snapshot)
                or not isinstance(self.provider, str) or not isinstance(self.platform, str)
                or type(self.contract_version) is not int or self.contract_version < 1):
            raise RuntimePolicyError("invalid_capability", "Expected immutable capability values and observations.")
        names = [one.name for one in self.features]
        if len(names) != len(set(names)):
            raise RuntimePolicyError("duplicate_capability", "Capabilities must have unique names.")

    def to_document(self):
        return {"provider": self.provider, "platform": self.platform,
                "contract_version": self.contract_version,
                "features": [one.to_document() for one in sorted(self.features, key=lambda one: one.name)],
                "observations": self.observations.to_document()}


@dataclass(frozen=True)
class PreflightReport:
    capabilities: CapabilitySet
    policy_digest: str
    checks: tuple

    def __post_init__(self):
        object.__setattr__(self, "checks", tuple(self.checks))

    @property
    def ready(self):
        return bool(self.checks) and all(one.status == "verified" for one in self.checks)

    def to_document(self):
        return {"ready": self.ready, "policy_digest": self.policy_digest,
                "capabilities": self.capabilities.to_document(),
                "checks": [one.to_document() for one in self.checks]}


@dataclass(frozen=True)
class ExecutionRequest:
    """A workload request must carry a resolved policy, not just a profile name."""

    policy: object                 # EffectivePolicy; no native process objects
    argv: tuple
    working_directory: str
    environment: Snapshot = field(default_factory=lambda: Snapshot({}), repr=False)
    timeout_seconds: float = None
    secret_values: SecretValues = field(default_factory=SecretValues, repr=False)
    gateway: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        from ..policy import EffectivePolicy
        if (not isinstance(self.policy, EffectivePolicy) or not isinstance(self.argv, (list, tuple))
                or not self.argv or not self.argv[0]
                or any(not isinstance(part, str) or "\0" in part for part in self.argv)
                or not isinstance(self.working_directory, str) or not self.working_directory
                or "\0" in self.working_directory):
            raise RuntimePolicyError("invalid_request", "Expected an effective policy and a nonempty argv.")
        object.__setattr__(self, "argv", tuple(self.argv))
        if not isinstance(self.environment, Snapshot):
            object.__setattr__(self, "environment", Snapshot(self.environment))
        env = self.environment.to_document()
        if (not isinstance(env, dict) or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                or not isinstance(value, str) or "\0" in value or len(value) > 16384 for key, value in env.items())):
            raise RuntimePolicyError("invalid_environment", "Expected a bounded string environment mapping.")
        if not set(env) <= set(self.policy.policy.to_document()["environment"]):
            raise RuntimePolicyError("environment_denied", "Environment variable was not granted by policy.")
        if not isinstance(self.secret_values, SecretValues):
            object.__setattr__(self, "secret_values", SecretValues(self.secret_values))
        if not set(self.secret_values.names) <= set(self.policy.policy.to_document()["secrets"]):
            raise RuntimePolicyError("secret_denied", "A supplied secret was not granted by the effective policy.")
        if self.gateway is not None:
            from ..gateway_session import GatewaySession
            if not isinstance(self.gateway, GatewaySession) or self.gateway.policy.digest != self.policy.digest or self.secret_values.names:
                raise RuntimePolicyError("invalid_gateway_session", "The native channel must match the exact operation policy and own its credential delivery.")
        if (self.timeout_seconds is not None and (type(self.timeout_seconds) not in (int, float)
                or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0)):
            raise RuntimePolicyError("invalid_timeout", "Timeout must be a finite positive duration.")


@dataclass(frozen=True)
class PreparedExecution:
    instance_id: str
    provider: str
    policy_digest: str


@dataclass(frozen=True)
class ExecutionHandle:
    instance_id: str
    provider: str
    policy_digest: str


class RuntimeBackend(ABC):
    provider = ""
    platform = ""
    contract_version = CONTRACT_VERSION

    def gateway_preflight(self, policy, route):
        raise RuntimePolicyError("gateway_channel_unavailable", "This OS backend has no native provider channel yet.")

    def record_event(self, record):
        raise RuntimePolicyError("runtime_audit_unavailable", "This backend cannot persist execution audit records.")

    def configure(self, options, protected_paths=()):
        raise RuntimePolicyError("backend_configuration_unsupported", "Native runtime deployment is unavailable for this provider.")

    def deployment_draft(self, request):
        """Return an unsaved deployment using this provider's native defaults."""
        raise RuntimePolicyError("provisioning_unavailable", "Environment creation is not implemented for this OS yet.")

    def runtime_root_identity(self):
        raise RuntimePolicyError("runtime_not_configured", "No approved runtime image is configured.")

    def runtime_file_identity(self, target):
        """Digest of a file pinned by the provider's approved workload image."""
        raise RuntimePolicyError("runtime_file_identity_unsupported", "This provider cannot verify worker artifacts.")

    def secret_file_locations(self, names):
        """Native paths for explicitly supplied ephemeral secrets; no values or I/O."""
        raise RuntimePolicyError("secret_delivery_unavailable", "This provider cannot describe scoped secret file delivery.")

    def service_scope(self, key):
        raise RuntimePolicyError("service_ownership_unsupported", "This provider cannot reserve a supervised service identity.")

    def execution_scope(self, kind, key):
        """Reserve one workload identity using the provider's durable ownership mechanism."""
        if kind not in ("cycle",):
            raise RuntimePolicyError("unsupported_execution_scope", "Unknown execution scope.")
        return self.service_scope(kind + "\0" + key)

    def bundle_info(self):
        raise RuntimePolicyError("bundle_target_unsupported", "This provider has no supported bundled native runtime.")

    def recover(self):
        raise RuntimePolicyError("recovery_unsupported", "This backend does not implement crash recovery.")

    def shutdown(self, deadline):
        raise RuntimePolicyError("shutdown_unsupported", "This backend does not implement supervisor shutdown.")

    @abstractmethod
    def capabilities(self):
        """Return observed prerequisites separately from verified guarantees."""

    def preflight(self, policy):
        if policy.provider != self.provider or policy.platform != self.platform:
            raise RuntimePolicyError("provider_platform_mismatch", "Policy and selected backend must agree.")
        capabilities = self.capabilities()
        if (capabilities.provider != self.provider or capabilities.platform != self.platform
                or self.contract_version != policy.backend_contract_version
                or capabilities.contract_version != policy.backend_contract_version):
            raise RuntimePolicyError("backend_contract_mismatch", "Backend identity or contract version does not match policy.")
        available = {one.name: one for one in capabilities.features}
        required = ("runtime_execution",) + policy.policy.required_capabilities()
        checks = tuple(available.get(name, Capability(name, "unsupported", "This backend does not implement this guarantee."))
                       for name in required)
        return PreflightReport(capabilities, policy.digest, checks)

    @abstractmethod
    def prepare(self, request):
        """Apply all restrictions before any workload code can execute."""

    @abstractmethod
    def start(self, prepared):
        """Consume a prepared execution and return a supervised handle."""

    @abstractmethod
    def stop(self, handle, deadline):
        """Terminate the instance and descendants by a bounded deadline."""

    @abstractmethod
    def wait(self, handle):
        """Return a portable result after the execution has ended."""

    @abstractmethod
    def inspect(self, handle):
        """Return actual enforcement, lifecycle and resource observations."""

    @abstractmethod
    def cleanup(self, handle):
        """Release instance resources idempotently after descendants exit."""


class UnimplementedBackend(RuntimeBackend):
    """Honest lifecycle refusal used until native enforcement is implemented."""

    def _unavailable(self):
        raise RuntimePolicyError("backend_not_implemented", "Native execution is not implemented; no workload was launched.")

    def prepare(self, request):
        self.preflight(request.policy)
        self._unavailable()

    def start(self, prepared):
        self._unavailable()

    def stop(self, handle, deadline):
        self._unavailable()

    def wait(self, handle):
        self._unavailable()

    def inspect(self, handle):
        self._unavailable()

    def cleanup(self, handle):
        self._unavailable()
