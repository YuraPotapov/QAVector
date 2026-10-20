"""Portable execution policy and backend contracts.

The Linux backend supports explicitly provisioned offline runtime images;
Windows execution remains unavailable. Importing this package never probes the
host or imports an OS-specific backend.
"""

from .errors import RuntimePolicyError
from .policy import EffectivePolicy, Policy, PolicyLayer, PolicyPatch, PolicyResolver
from .runtime import ExecutionRuntime
from .secrets import SecretValues

__all__ = ["EffectivePolicy", "Policy", "PolicyLayer", "PolicyPatch",
           "PolicyResolver", "RuntimePolicyError", "ExecutionRuntime", "SecretValues"]
