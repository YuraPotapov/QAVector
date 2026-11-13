"""Scoped discovery/read facade; registrations own metadata, never processes."""

import copy
import time
from dataclasses import dataclass, replace
from typing import Callable, Optional, Tuple

from .contracts import (CapabilityDescriptor, PropertySnapshot, ResourceDescriptor,
                        ResourceRef, duration, text)
from .errors import QCRError
from .schema import Schemas


@dataclass(frozen=True)
class Registration:
    resource: ResourceDescriptor
    capabilities: Tuple[CapabilityDescriptor, ...]
    read_properties: Callable
    read_capability: Optional[Callable] = None
    #: The trusted adapter that carries out non-read capabilities - an object
    #: with ``requirements(capability, request)``, ``start(job)`` and
    #: ``cancel(job)`` (see :mod:`qcr.engine`). None: nothing here is invocable.
    invoker: Optional[object] = None


class Registry:
    def __init__(self, authority, *, local_schemas=None, clock=time.time):
        self._authority = authority
        self._schemas = Schemas(local_schemas)
        self._clock = clock
        self._entries = {}

    def register(self, registration):
        self.replace(object(), (registration,))

    def replace(self, owner, registrations):
        """Atomically replace one trusted adapter's metadata, retaining others.

        Composition-root calls only. Callables are retained, descriptors copied.
        The caller serializes registry updates with reads on its owning thread.
        """
        entries = {ref: entry for ref, entry in self._entries.items() if entry[0] is not owner}
        for registration in registrations:
            if not isinstance(registration, Registration):
                raise QCRError("invalid_contract", "Expected a resource registration.")
            resource = copy.deepcopy(registration.resource)
            capabilities = copy.deepcopy(tuple(registration.capabilities))
            if not isinstance(resource, ResourceDescriptor) or not callable(registration.read_properties):
                raise QCRError("invalid_contract", "Invalid resource registration.")
            if resource.ref in entries:
                raise QCRError("duplicate_resource", "Resource is already registered.")
            for prop in resource.properties:
                self._schemas.check(prop.schema)
            for cap in capabilities:
                if not isinstance(cap, CapabilityDescriptor) or cap.resource != resource.ref:
                    raise QCRError("invalid_contract", "Capability resource does not match.")
                if cap.capability_id.split(".", 1)[0] != resource.namespace:
                    raise QCRError("invalid_contract", "Capability namespace does not match.")
                self._schemas.check(cap.input_schema)
                self._schemas.check(cap.output_schema)
            if registration.read_capability is not None and not callable(registration.read_capability):
                raise QCRError("invalid_contract", "Invalid read callback.")
            invoker = registration.invoker
            if invoker is not None and not all(callable(getattr(invoker, name, None))
                                               for name in ("requirements", "start", "cancel")):
                raise QCRError("invalid_contract", "Invalid invoker.")
            entries[resource.ref] = (owner, Registration(resource, capabilities, registration.read_properties,
                                                        registration.read_capability, invoker))
        aliases, capabilities, targets = set(), set(), {}
        for ref, (_, registration) in entries.items():
            for alias in (ref.resource_id, *registration.resource.aliases):
                key = (ref.project_id, alias)
                if key in aliases:
                    raise QCRError("duplicate_alias", "Resource alias is ambiguous.")
                aliases.add(key)
            for cap in registration.capabilities:
                identity = (ref.project_id, cap.capability_id)
                if identity in targets and targets[identity] != ref:
                    raise QCRError("duplicate_capability", "Capability versions must refer to one resource.")
                targets[identity] = ref
                key = (ref.project_id, cap.capability_id, cap.version)
                if key in capabilities:
                    raise QCRError("duplicate_capability", "Capability version is already registered.")
                capabilities.add(key)
        self._entries = entries

    def _visible(self, caller, registration):
        resource = registration.resource
        return self._authority.allows(caller, resource.ref, resource.permission)

    def _resource(self, caller, ref):
        self._authority.check_caller(caller)
        if not isinstance(ref, ResourceRef):
            raise QCRError("invalid_contract", "Expected a resource reference.")
        entry = self._entries.get(ref)
        if entry is None or not self._visible(caller, entry[1]):
            # Missing and inaccessible resources do not disclose existence.
            raise QCRError("not_found", "Resource is not available.")
        return entry[1]

    def _describe(self, caller, resource):
        return copy.deepcopy(replace(resource, properties=tuple(
            p for p in resource.properties if self._authority.allows(caller, resource.ref, p.permission))))

    def list_resources(self, caller, project_id, resource_type=None):
        self._authority.check_caller(caller)
        text(project_id, "project id")
        return [self._describe(caller, reg.resource) for _, reg in self._entries.values()
                if reg.resource.ref.project_id == project_id
                and (resource_type is None or reg.resource.ref.resource_type == resource_type)
                and self._visible(caller, reg)]

    def describe_resource(self, caller, project_id, resource_id_or_alias):
        self._authority.check_caller(caller)
        text(project_id, "project id")
        text(resource_id_or_alias, "resource id or alias")
        for ref, (_, reg) in self._entries.items():
            if ref.project_id == project_id and resource_id_or_alias in (ref.resource_id, *reg.resource.aliases):
                return self._describe(caller, self._resource(caller, ref).resource)
        raise QCRError("not_found", "Resource is not available.")

    def list_capabilities(self, caller, project_id, namespace=None):
        self._authority.check_caller(caller)
        text(project_id, "project id")
        return [copy.deepcopy(cap) for _, reg in self._entries.values()
                if reg.resource.ref.project_id == project_id
                and (namespace is None or reg.resource.namespace == namespace)
                and self._visible(caller, reg) for cap in reg.capabilities
                if self._authority.allows(caller, cap.resource, cap.permission)]

    def describe_capability(self, caller, project_id, capability_id, version=None):
        self._authority.check_caller(caller)
        text(capability_id, "capability id")
        if version is not None and (type(version) is not int or version < 1):
            raise QCRError("invalid_contract", "Invalid capability version.")
        candidates = [cap for cap in self.list_capabilities(caller, project_id)
                      if cap.capability_id == capability_id and (version is None or cap.version == version)]
        if not candidates:
            raise QCRError("not_found", "Capability is not available.")
        return max(candidates, key=lambda cap: cap.version)

    def read(self, caller, project_id, capability_id, request, *, version=None):
        """Explicit parameterized read; no retries, jobs, events or mutation invoke.

        Authentication/permissions and input validation precede provider I/O.
        A host must offload blocking remote reads from its UI event loop.
        """
        cap = self.describe_capability(caller, project_id, capability_id, version)
        reg = self._resource(caller, cap.resource)
        if not cap.read_only or reg.read_capability is None:
            raise QCRError("not_readable", "Capability is not exposed as a read.")
        self._schemas.validate(request, cap.input_schema, code="invalid_input")
        try:
            result = reg.read_capability(cap.capability_id, cap.version, copy.deepcopy(request))
        except QCRError:
            # A trusted adapter's own value-free code (out_of_scope, auth, network...).
            raise
        except Exception:
            raise QCRError("read_failed", "Capability read failed.") from None
        self._schemas.validate(result, cap.output_schema, code="invalid_output")
        return copy.deepcopy(result)

    def invocable(self, caller, project_id, capability_id, request, *, version=None):
        """Resolve a non-read capability for invocation: ``(capability, invoker)``.

        Resource and capability permissions are checked, and the request is
        validated against the input schema, before anything is recorded or
        dispatched. A capability without an invoker is ``not_invocable``.
        """
        cap = self.describe_capability(caller, project_id, capability_id, version)
        reg = self._resource(caller, cap.resource)
        if cap.read_only or reg.invoker is None:
            raise QCRError("not_invocable", "Capability is not invocable.")
        self._schemas.validate(request, cap.input_schema, code="invalid_input")
        return cap, reg.invoker

    def validate_input(self, capability, request):
        """Check a request against the capability's input schema (``invalid_input``)."""
        self._schemas.validate(request, capability.input_schema, code="invalid_input")

    def validate_output(self, capability, output):
        """Check an invoker's output against the capability's registered schema."""
        self._schemas.validate(output, capability.output_schema, code="invalid_output")

    def allows(self, caller, ref, permission):
        """Whether ``caller`` holds ``permission`` on ``ref`` - for a closure check."""
        return self._authority.allows(caller, ref, permission)

    def get_properties(self, caller, ref, names=None, *, max_age_seconds=None):
        reg = self._resource(caller, ref)
        if max_age_seconds is not None:
            duration(max_age_seconds)
        properties = {p.name: p for p in reg.resource.properties}
        if names is None:
            names = [p.name for p in properties.values() if self._authority.allows(caller, ref, p.permission)]
        elif not isinstance(names, (list, tuple)) or any(not isinstance(n, str) for n in names):
            raise QCRError("invalid_contract", "Expected property names.")
        names = tuple(dict.fromkeys(names))
        for name in names:
            if name not in properties:
                raise QCRError("not_found", "Property is not available.")
            self._authority.require(caller, ref, properties[name].permission)
        if not names:
            return {}
        try:
            values = reg.read_properties(ref, names)
        except Exception:
            raise QCRError("source_unavailable", "Property source is unavailable.") from None
        if not isinstance(values, dict) or set(values) != set(names):
            raise QCRError("invalid_property", "Property source returned an invalid response.")
        now = self._clock()
        result = {}
        for name, value in values.items():
            if not isinstance(value, PropertySnapshot):
                raise QCRError("invalid_property", "Expected a property snapshot.")
            if value.availability == "available":
                self._schemas.validate(value.value, properties[name].schema)
                limit = value.ttl_seconds if max_age_seconds is None else min(value.ttl_seconds, max_age_seconds)
                if now < value.observed_at or now - value.observed_at > limit:
                    value = replace(value, value=None, availability="stale")
            result[name] = copy.deepcopy(value)
        return result
