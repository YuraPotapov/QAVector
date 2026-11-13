"""Version-one read contracts. No GUI, process or storage ownership."""

import json
import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Optional, Tuple

from .errors import QCRError


def text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(
            ord(char) < 32 for char in value):
        raise QCRError("invalid_contract", "Invalid %s." % name)


def duration(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise QCRError("invalid_contract", "Expected a finite nonnegative duration.")


@dataclass(frozen=True)
class ResourceRef:
    project_id: str
    resource_id: str
    resource_type: str

    def __post_init__(self):
        for name in ("project_id", "resource_id", "resource_type"):
            text(getattr(self, name), name)


@dataclass(frozen=True)
class PropertyDescriptor:
    name: str
    schema: Any
    permission: str

    def __post_init__(self):
        text(self.name, "property name")
        text(self.permission, "permission")


@dataclass(frozen=True)
class ResourceDescriptor:
    ref: ResourceRef
    namespace: str
    display_name: str
    permission: str
    aliases: Tuple[str, ...] = ()
    properties: Tuple[PropertyDescriptor, ...] = ()

    def __post_init__(self):
        if not isinstance(self.ref, ResourceRef):
            raise QCRError("invalid_contract", "Expected a resource reference.")
        if not isinstance(self.aliases, (tuple, list)) or not isinstance(self.properties, (tuple, list)) or any(
                not isinstance(prop, PropertyDescriptor) for prop in self.properties):
            raise QCRError("invalid_contract", "Expected aliases and property descriptors.")
        for name in ("namespace", "display_name", "permission"):
            text(getattr(self, name), name)
        for alias in self.aliases:
            text(alias, "alias")
        if len(set(self.aliases)) != len(self.aliases) or len({p.name for p in self.properties}) != len(self.properties):
            raise QCRError("invalid_contract", "Duplicate alias or property.")


@dataclass(frozen=True)
class CapabilityDescriptor:
    capability_id: str
    resource: ResourceRef
    version: int
    description: str
    input_schema: Any
    output_schema: Any
    permission: str
    risk_level: str = "low"
    mode: str = "sync"
    timeout_seconds: float = 30
    supports_cancel: bool = False
    supports_idempotency: bool = False
    read_only: bool = False

    def __post_init__(self):
        text(self.capability_id, "capability id")
        if not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[A-Za-z0-9_-]+){2,}", self.capability_id):
            raise QCRError("invalid_contract", "Invalid capability id.")
        if not isinstance(self.resource, ResourceRef) or type(self.version) is not int or self.version < 1:
            raise QCRError("invalid_contract", "Invalid capability reference or version.")
        text(self.description, "description")
        text(self.permission, "permission")
        if self.risk_level not in ("low", "medium", "high", "critical") or self.mode not in ("sync", "long_running"):
            raise QCRError("invalid_contract", "Invalid risk or mode.")
        if any(type(value) is not bool for value in (self.supports_cancel, self.supports_idempotency, self.read_only)):
            raise QCRError("invalid_contract", "Invalid capability support flags.")
        duration(self.timeout_seconds)


@dataclass(frozen=True)
class PropertySnapshot:
    value: Any
    source: str
    observed_at: Optional[float]
    ttl_seconds: float
    availability: str = "available"

    def __post_init__(self):
        text(self.source, "source")
        duration(self.ttl_seconds)
        if self.observed_at is not None:
            duration(self.observed_at)
        if self.availability not in ("available", "unavailable", "stale"):
            raise QCRError("invalid_contract", "Invalid property availability.")
        if self.availability == "available" and self.observed_at is None:
            raise QCRError("invalid_contract", "Available properties need an observation time.")
        if self.availability != "available" and self.value is not None:
            raise QCRError("invalid_contract", "Unavailable properties must not carry a value.")


def document(contract):
    """A detached JSON-compatible DTO, including its wire version."""
    try:
        return json.loads(json.dumps(dict(schema_version=1, **asdict(contract)), allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        raise QCRError("invalid_contract", "Contract is not finite JSON data.") from None
