"""QAVector Capability Runtime: scoped discovery, reads, and the execution engine."""

from .engine import Engine, Job
from .contracts import (CapabilityDescriptor, PropertyDescriptor, PropertySnapshot,
                        ResourceDescriptor, ResourceRef, document)
from .errors import QCRError
from .policy import Authority, Caller, Grant
from .registry import Registration, Registry

__all__ = ["Engine", "Job", "Authority", "Caller", "Grant", "Registry", "Registration", "QCRError",
           "ResourceRef", "ResourceDescriptor", "CapabilityDescriptor",
           "PropertyDescriptor", "PropertySnapshot", "document"]
