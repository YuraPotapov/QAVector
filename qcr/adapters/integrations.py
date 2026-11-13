"""One adapter for provider-declared reads; no Jira/Teams HTTP knowledge."""

import copy
import re
import time

from integrations.contracts import READ_ERRORS, Provider, ReadError

from ..contracts import (CapabilityDescriptor, PropertyDescriptor, PropertySnapshot,
                         ResourceDescriptor, ResourceRef, text)
from ..errors import QCRError
from ..registry import Registration


class IntegrationAdapter:
    def __init__(self, registry, project_id, source, *, clock=time.time):
        text(project_id, "project id")
        self.registry, self.project_id = registry, project_id
        self._source, self._clock, self._owner = source, clock, object()
        self._providers = copy.deepcopy(source.providers)
        for name, provider in self._providers.items():
            if not isinstance(provider, Provider) or name != provider.name:
                raise QCRError("invalid_contract", "Invalid integration provider.")
            provider.__post_init__()

    def _connections(self):
        try:
            rows = self._source.connections()
            seen = set()
            for row in rows:
                if set(row) != {"id", "provider", "name"}:
                    raise ValueError("Invalid metadata.")
                for key in row:
                    text(row[key], "connection metadata")
                if not re.fullmatch(r"[A-Za-z0-9_-]+", row["id"]) or row["id"] in seen:
                    raise ValueError("Invalid or duplicate connection identity.")
                seen.add(row["id"])
            return rows
        except Exception:
            raise QCRError("source_unavailable", "Connection inventory is unavailable.") from None

    def refresh(self):
        """Metadata-only refresh. Unknown/unimplemented providers are not exposed."""
        registrations = []
        for row in self._connections():
            provider = self._providers.get(row["provider"])
            if provider is None:
                continue
            connection_id = row["id"]
            prefix = "integrations.%s.%s" % (provider.name, connection_id)
            permission = "integrations.%s.read" % provider.name
            ref = ResourceRef(self.project_id, prefix, "integration")
            resource = ResourceDescriptor(ref, "integrations", row["name"], permission, (connection_id,), tuple(
                PropertyDescriptor(key, {"type": "string"}, permission) for key in ("id", "provider", "name")))
            caps = tuple(CapabilityDescriptor(prefix + "." + op.name, ref, op.version, op.description,
                op.input_schema, op.output_schema, "integrations.%s.%s" % (provider.name, op.name),
                risk_level=op.risk, read_only=True)
                for op in provider.contracts.values() if op.access == "read")
            registrations.append(Registration(resource, caps,
                self._properties(connection_id, provider.name), self._read(connection_id, provider, prefix)))
        self.registry.replace(self._owner, registrations)

    def _properties(self, connection_id, provider_name):
        def read(ref, names):
            row = next((row for row in self._connections()
                        if row["id"] == connection_id and row["provider"] == provider_name), None)
            now = self._clock()
            return {name: PropertySnapshot(row[name] if row else None, "integrations.configuration",
                    now if row else None, 5 if row else 0, "available" if row else "unavailable") for name in names}
        return read

    def _read(self, connection_id, provider, prefix):
        allowed = {prefix + "." + op.name: op for op in provider.contracts.values() if op.access == "read"}
        def read(capability_id, version, request):
            operation = allowed[capability_id]
            if operation.version != version:
                raise QCRError("not_found", "Operation version is unavailable.")
            try:
                return self._source.read(connection_id, provider.name, operation.name, request)
            except ReadError as exc:
                # The code and its fixed sentence only; never the cause.
                raise QCRError(exc.code, READ_ERRORS[exc.code]) from None
        return read
