"""Strict portable policy schema, immutable snapshots and narrowing resolution."""

import ipaddress
import math
import re
from dataclasses import dataclass

from ._json import Snapshot, digest, object_fields
from .errors import RuntimePolicyError
from .paths import PathMapper, contains_path, identifier, relative_path

SCHEMA_VERSION = 1
LAYER_ORDER = ("platform", "project", "cycle", "environment", "node")
POLICY_FIELDS = ("filesystem", "network", "resources", "security", "tools",
                 "integrations", "secrets", "environment")
RESOURCE_FIELDS = ("cpu_cores", "cpu_percent", "memory", "processes", "threads", "tasks", "scratch_bytes")
_GRANT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:/-]{0,255}$")


def _array(value, field):
    if not isinstance(value, list):
        raise RuntimePolicyError("invalid_type", "Expected an array.", field)
    if len(value) > 256:
        raise RuntimePolicyError("too_many_grants", "A grant array cannot exceed 256 entries.", field)
    return value


def _names(value, field):
    values = _array(value, field)
    if any(not isinstance(one, str) or not _GRANT_NAME.fullmatch(one) for one in values):
        raise RuntimePolicyError("invalid_grant", "Expected grant identifiers, never secret values.", field)
    if len(set(values)) != len(values):
        raise RuntimePolicyError("duplicate_grant", "Grant identifiers must be unique.", field)
    return sorted(values)


def _positive(value, field, integer=False):
    try:
        valid = (type(value) in (int, float) and math.isfinite(value) and value > 0
                 and (not integer or type(value) is int and value <= 2 ** 63 - 1))
    except OverflowError:
        valid = False
    if not valid:
        raise RuntimePolicyError("invalid_limit", "Expected a finite positive %s."
                                 % ("integer" if integer else "number"), field)
    return value if integer else float(value)


def _filesystem(raw):
    result = []
    for entry in _array(raw, "filesystem"):
        object_fields(entry, ("binding", "path", "access"), "filesystem",
                      ("binding", "access"))
        grant = {"binding": identifier(entry["binding"], "filesystem.binding"),
                 "path": relative_path(entry.get("path", "."))}
        if entry["access"] not in ("read_only", "read_write"):
            raise RuntimePolicyError("invalid_access", "Access must be read_only or read_write.", "filesystem.access")
        grant["access"] = entry["access"]
        if any(one["binding"] == grant["binding"] and
               (contains_path(one["path"], grant["path"]) or
                contains_path(grant["path"], one["path"])) for one in result):
            raise RuntimePolicyError("overlapping_grants", "Filesystem grants cannot overlap within one binding.", "filesystem")
        result.append(grant)
    return sorted(result, key=lambda one: (one["binding"], one["path"]))


def _network(raw):
    object_fields(raw, ("mode", "endpoints"), "network", ("mode",))
    mode = raw["mode"]
    if mode not in ("none", "restricted", "full"):
        raise RuntimePolicyError("invalid_network", "Network mode must be none, restricted or full.", "network.mode")
    endpoints = []
    for entry in _array(raw.get("endpoints", []), "network.endpoints"):
        object_fields(entry, ("host", "port", "protocol"), "network.endpoints", ("host", "port"))
        host, port, protocol = entry["host"], entry["port"], entry.get("protocol", "tcp")
        if not isinstance(host, str) or len(host) > 253 or "%" in host:
            raise RuntimePolicyError("invalid_endpoint", "Expected a hostname or IP address.", "network.endpoints.host")
        try:
            host = str(ipaddress.ip_address(host))
        except ValueError:
            host = host.lower()
            if not host or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                                   for part in host.split(".")):
                raise RuntimePolicyError("invalid_endpoint", "Expected an exact hostname or IP address.", "network.endpoints.host")
        if type(port) is not int or not 1 <= port <= 65535 or protocol not in ("tcp", "udp"):
            raise RuntimePolicyError("invalid_endpoint", "Expected port 1..65535 and tcp or udp.", "network.endpoints")
        endpoint = {"host": host, "port": port, "protocol": protocol}
        if endpoint in endpoints:
            raise RuntimePolicyError("duplicate_endpoint", "Network endpoints must be unique.", "network.endpoints")
        endpoints.append(endpoint)
    if mode != "restricted" and endpoints:
        raise RuntimePolicyError("invalid_network", "Only restricted mode accepts endpoints.", "network.endpoints")
    return {"mode": mode, "endpoints": sorted(endpoints, key=lambda one: (one["host"], one["port"], one["protocol"]))}


def _resources(raw):
    object_fields(raw, RESOURCE_FIELDS, "resources")
    result = {}
    for key, value in raw.items():
        if key == "memory":
            object_fields(value, ("bytes", "accounting"), "resources.memory", ("bytes", "accounting"))
            if value["accounting"] not in ("resident", "committed", "charged"):
                raise RuntimePolicyError("invalid_accounting", "Memory accounting must be resident, committed or charged.", "resources.memory.accounting")
            result[key] = {"bytes": _positive(value["bytes"], "resources.memory.bytes", True),
                           "accounting": value["accounting"]}
        else:
            result[key] = _positive(value, "resources." + key, key not in ("cpu_cores", "cpu_percent"))
            if key == "cpu_percent" and value > 100:
                raise RuntimePolicyError("invalid_limit", "CPU percentage cannot exceed 100.", "resources.cpu_percent")
    return result


def _security(raw):
    object_fields(raw, ("contain_descendants", "privilege_escalation"), "security")
    if any(type(value) is not bool for value in raw.values()):
        raise RuntimePolicyError("invalid_type", "Security flags must be boolean.", "security")
    return dict(raw)


def _integrations(raw):
    result = []
    for entry in _array(raw, "integrations"):
        object_fields(entry, ("service", "resource", "actions"), "integrations", ("service", "resource", "actions"))
        service = identifier(entry["service"], "integrations.service")
        resource = _names([entry["resource"]], "integrations.resource")[0]
        if any(one["service"] == service and one["resource"] == resource for one in result):
            raise RuntimePolicyError("duplicate_grant", "Integration resources must be unique.", "integrations")
        result.append({"service": service, "resource": resource,
                       "actions": _names(entry["actions"], "integrations.actions")})
    return sorted(result, key=lambda one: (one["service"], one["resource"]))


_PARSERS = {"filesystem": _filesystem, "network": _network, "resources": _resources,
            "security": _security, "integrations": _integrations}


def _parse(raw):
    object_fields(raw, POLICY_FIELDS, "policy")
    return {key: _PARSERS[key](value) if key in _PARSERS else _names(value, key)
            for key, value in raw.items()}


@dataclass(frozen=True, init=False)
class PolicyPatch(Snapshot):
    """Missing fields inherit; empty grant arrays explicitly remove permissions."""

    def __init__(self, document):
        super().__init__(_parse(document))


@dataclass(frozen=True, init=False)
class Policy(Snapshot):
    def __init__(self, document):
        result = {"filesystem": [], "network": {"mode": "none", "endpoints": []},
                  "resources": {}, "security": {"contain_descendants": True, "privilege_escalation": False},
                  "tools": [], "integrations": [], "secrets": [], "environment": []}
        for key, value in _parse(document).items():
            if key == "security":
                result[key].update(value)
            else:
                result[key] = value
        super().__init__(result)

    def required_capabilities(self):
        raw = self.to_document()
        required = {"filesystem_isolation", "environment_allowlist"}
        required.add("network_" + raw["network"]["mode"])
        if raw["security"]["contain_descendants"]:
            required.add("descendant_containment")
        if not raw["security"]["privilege_escalation"]:
            required.add("privilege_restrictions")
        for key, value in raw["resources"].items():
            required.add("memory_" + value["accounting"] if key == "memory" else "limit_" + key)
        for key in ("tools", "integrations", "secrets"):
            if raw[key]:
                required.add(key + "_grants")
        return tuple(sorted(required))


@dataclass(frozen=True)
class PolicyLayer:
    name: str
    patch: PolicyPatch

    def __post_init__(self):
        if self.name not in LAYER_ORDER or not isinstance(self.patch, PolicyPatch):
            raise RuntimePolicyError("invalid_layer", "Expected a named policy layer and PolicyPatch.")

    def to_document(self):
        return {"name": self.name, "policy": self.patch.to_document()}


def _deny(field, layer):
    raise RuntimePolicyError("policy_widening", "This layer exceeds its inherited permissions.", field, layer)


def _narrow(parent, patch, layer, provenance):
    for key, value in patch.items():
        before = parent[key]
        if key in ("tools", "secrets", "environment"):
            if not set(value) <= set(before):
                _deny(key, layer)
        elif key == "filesystem":
            for grant in value:
                if not any(one["binding"] == grant["binding"] and
                           contains_path(one["path"], grant["path"]) and
                           (one["access"] == "read_write" or grant["access"] == "read_only") for one in before):
                    _deny(key, layer)
        elif key == "network":
            rank = {"none": 0, "restricted": 1, "full": 2}
            if rank[value["mode"]] > rank[before["mode"]]:
                _deny(key, layer)
            if value["mode"] == before["mode"] == "restricted" and any(one not in before["endpoints"] for one in value["endpoints"]):
                _deny(key, layer)
        elif key == "integrations":
            for grant in value:
                if not any(one["service"] == grant["service"] and one["resource"] == grant["resource"]
                           and set(grant["actions"]) <= set(one["actions"]) for one in before):
                    _deny(key, layer)
        elif key == "security":
            for flag, setting in value.items():
                if (flag == "contain_descendants" and before[flag] and not setting or
                        flag == "privilege_escalation" and not before[flag] and setting):
                    _deny("security." + flag, layer)
                parent[key][flag] = setting
                provenance["security." + flag] = layer
            continue
        elif key == "resources":
            for limit, setting in value.items():
                if limit in before:
                    previous = before[limit]
                    if limit == "memory":
                        if previous["accounting"] != setting["accounting"]:
                            raise RuntimePolicyError("incompatible_accounting", "Memory accounting cannot change across ceilings.", "resources.memory", layer)
                        setting = dict(setting, bytes=min(previous["bytes"], setting["bytes"]))
                        tighter = setting["bytes"] < previous["bytes"]
                    else:
                        tighter = setting < previous
                        setting = min(previous, setting)
                    if not tighter:
                        continue
                parent[key][limit] = setting
                provenance["resources." + limit] = layer
            continue
        parent[key] = value
        provenance[key] = layer


@dataclass(frozen=True)
class EffectivePolicy:
    policy: Policy
    provider: str
    platform: str
    bindings: tuple
    layers: tuple
    provenance: Snapshot
    profile_revision: str = ""
    runtime_root_identity: str = ""
    backend_contract_version: int = 1

    def __post_init__(self):
        from .paths import PathBinding
        if (not isinstance(self.policy, Policy) or not isinstance(self.provenance, Snapshot)
                or not isinstance(self.bindings, (list, tuple))
                or not isinstance(self.layers, (list, tuple))
                or any(not isinstance(one, PathBinding) for one in self.bindings)
                or any(not isinstance(one, PolicyLayer) for one in self.layers)
                or type(self.backend_contract_version) is not int or self.backend_contract_version < 1):
            raise RuntimePolicyError("invalid_effective_policy", "Expected immutable policy components and a contract version.")
        if any(not isinstance(value, str) for value in (self.platform, self.profile_revision, self.runtime_root_identity)):
            raise RuntimePolicyError("invalid_effective_policy", "Policy identity fields must be immutable strings.")
        identifier(self.provider, "provider")
        object.__setattr__(self, "bindings", tuple(self.bindings))
        object.__setattr__(self, "layers", tuple(self.layers))

    def to_document(self):
        mapper = PathMapper(self.platform)
        by_name = {one.name: one for one in self.bindings}
        mounts = [dict(mapper.resolve(by_name[grant["binding"]], grant["path"]), access=grant["access"])
                  for grant in self.policy.to_document()["filesystem"]]
        return {"schema_version": SCHEMA_VERSION, "provider": self.provider, "platform": self.platform,
                "backend_contract_version": self.backend_contract_version,
                "profile_revision": self.profile_revision, "runtime_root_identity": self.runtime_root_identity,
                "policy": self.policy.to_document(), "bindings": [one.to_document() for one in self.bindings],
                "resolved_filesystem": mounts, "layers": [one.to_document() for one in self.layers],
                "provenance": self.provenance.to_document()}

    @property
    def digest(self):
        return digest(self.to_document())


class PolicyResolver:
    def resolve(self, layers, bindings=(), *, platform, provider,
                profile_revision="", runtime_root_identity=""):
        layers = tuple(layers)
        if (not layers or any(not isinstance(one, PolicyLayer) for one in layers)
                or layers[0].name != "platform"):
            raise RuntimePolicyError("missing_baseline", "The first layer must be the trusted platform baseline.")
        order = [LAYER_ORDER.index(one.name) for one in layers]
        if order != sorted(set(order)):
            raise RuntimePolicyError("invalid_layer_order", "Policy layers must be unique and follow authority order.")
        if not isinstance(profile_revision, str) or (profile_revision and not re.fullmatch(r"[0-9a-f]{64}", profile_revision)):
            raise RuntimePolicyError("invalid_revision", "Expected a canonical profile revision.")
        if not isinstance(runtime_root_identity, str) or (runtime_root_identity and not re.fullmatch(r"sha256:[0-9a-f]{64}", runtime_root_identity)):
            raise RuntimePolicyError("invalid_runtime_root", "Expected an explicit sha256 runtime root identity.")
        # Compatibility/OS selection belongs to BackendRegistry, not policy.
        # New providers on an existing platform must not change this model.
        identifier(provider, "provider")
        resolved = Policy(layers[0].patch.to_document()).to_document()
        provenance = {key: "platform" for key in resolved}
        provenance.update({"security." + key: "platform" for key in resolved["security"]})
        provenance.update({"resources." + key: "platform" for key in resolved["resources"]})
        for layer in layers[1:]:
            _narrow(resolved, layer.patch.to_document(), layer.name, provenance)
        mapper = PathMapper(platform)
        bound = mapper.bindings(list(bindings))
        known = {one.name for one in bound}
        if any(one["binding"] not in known for one in resolved["filesystem"]):
            raise RuntimePolicyError("missing_binding", "A filesystem grant needs a trusted path binding.", "filesystem")
        result = EffectivePolicy(Policy(resolved), provider, platform, bound, layers,
                                 Snapshot(provenance), profile_revision, runtime_root_identity)
        result.to_document()  # Validate all derived Windows/POSIX subpaths now.
        return result
