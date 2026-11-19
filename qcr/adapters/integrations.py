"""One adapter for provider-declared reads and writes; no Jira/Teams HTTP knowledge.

Reads are explicit registry reads on ``integrations.<provider>.<connection>``.
Writes (ADR-007) are executions on ``integrations.<provider>.<connection>~writes``
that wait for a person: the provider prepares what the person decides on, the
person approves - possibly with the editable fields changed - or denies, and
only the approved version is written. One unfinished write per connection.
"""

import copy
import logging
import os
import re
import threading
import time

from integrations.contracts import READ_ERRORS, WRITE_ERRORS, Provider, ReadError, WriteError

from ..contracts import (CapabilityDescriptor, PropertyDescriptor, PropertySnapshot,
                         ResourceDescriptor, ResourceRef, text)
from ..errors import QCRError
from ..registry import Registration


log = logging.getLogger("qcr.integrations")

#: A write's resource: apart from the connection, so the write switch does not
#: open reading, and "one unfinished write" is about writes only.
WRITES = "%s~writes"
#: How long a write may wait for the person, and then be made.
WRITE_SECONDS = 3600


#: The kinds of caller a connection may limit (ADR-015 D5), by the start of the
#: actor's id. The person (the GUI's own actor) is no kind: nothing limits them.
KINDS = (("agent:", "agents"), ("cycle:", "cycles"), ("run:", "cycles"))


def kind_of(actor_id):
    """``"agents"``, ``"cycles"``, or None for the person."""
    return next((kind for start, kind in KINDS if str(actor_id or "").startswith(start)), None)


def _thread(work):
    threading.Thread(target=work, name="qcr-integration-write", daemon=True).start()


class IntegrationAdapter:
    def __init__(self, registry, project_id, source, *, clock=time.time, notify=None, label=None, folder=None,
                 spawn=_thread):
        """``notify(message)`` tells the GUI a write waits for the person;
        ``label(actor_id)`` names who asked, for the activity log;
        ``folder(actor_id)`` is where that actor may take a file from, or None."""
        text(project_id, "project id")
        self.registry, self.project_id = registry, project_id
        #: Called with the connections' rows after each refresh: what registers beside them.
        self.refreshed = []
        self._source, self._clock, self._owner = source, clock, object()
        self.writer = WriteInvoker(source, notify or (lambda message: None), label or (lambda actor: actor),
                                   spawn, folder or (lambda actor: None))
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
        registrations, targets = [], {}
        rows = self._connections()
        for row in rows:
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
            writes = [op for op in provider.contracts.values() if op.access == "write"]
            if writes:
                wref = ResourceRef(self.project_id, WRITES % prefix, "integration_writes")
                wcaps = tuple(CapabilityDescriptor(prefix + "." + op.name, wref, op.version, op.description,
                    op.input_schema, op.output_schema, "integrations.%s.%s" % (provider.name, op.name),
                    risk_level=op.risk, mode="long_running", timeout_seconds=WRITE_SECONDS,
                    supports_cancel=True, supports_idempotency=True) for op in writes)
                for op in writes:
                    targets[prefix + "." + op.name] = (connection_id, provider.name, op.name, row["name"],
                                                       op.local_paths)
                registrations.append(Registration(
                    ResourceDescriptor(wref, "integrations", "%s changes" % row["name"],
                                       "integrations.%s.write" % provider.name),
                    wcaps, lambda ref, names: {}, invoker=self.writer))
        self.registry.replace(self._owner, registrations)
        self.writer.targets = targets
        for hook in self.refreshed:
            hook(rows)

    def narrow(self, actor_id, grants):
        """``grants`` held to what each connection allows ``actor_id``'s kind of
        caller (ADR-015 D5): for a provider whose connections set such a limit,
        a grant over all its connections becomes one per connection that is not
        Off - so the caller does not even see the others - and a grant on one
        connection that is Off is dropped. Other grants are as given."""
        kind = kind_of(actor_id)
        ceilings = getattr(self._source, "ceilings", None)
        if kind is None or ceilings is None:
            return list(grants)
        try:
            levels = ceilings(kind)
        except Exception:
            levels = None
        limited = {provider for provider, _connection in (levels or {})}
        made = []
        for grant in grants:
            parts = grant.permission.split(".")
            provider = parts[1] if len(parts) > 2 and parts[0] == "integrations" else None
            if levels is None and provider in self._providers:
                continue                          # cannot tell what is allowed: nothing is
            if provider not in limited:
                made.append(grant)
                continue
            open_ = ["integrations.%s.%s" % (provider, connection) for (name, connection), level
                     in sorted(levels.items()) if name == provider and level != "off"]
            if grant.resource_id is None:
                made.extend(type(grant)(grant.project_id, grant.permission, resource)
                            for prefix in open_ for resource in (prefix, WRITES % prefix))
            elif grant.resource_id.split("~", 1)[0] in open_:
                made.append(grant)
        return made

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
                return text_only(self._source.read(connection_id, provider.name, operation.name, request))
            except ReadError as exc:
                # The code and its fixed sentence only; never the cause.
                raise QCRError(exc.code, READ_ERRORS[exc.code]) from None
        return read


#: What a page needs to draw and edit an issue, not what an agent reads: through
#: QCR they come empty - the text beside them is whole (ADR-014). The schema
#: keeps the fields; a description three times over did not fit an agent's read.
_PAGE_ONLY = {"description_html": "", "description_doc": None, "description_lost": [],
              "html": "", "doc": None, "lost": []}


def text_only(found):
    """An issue or comments page with the page's HTML and editor documents emptied."""
    def slim(value):
        if isinstance(value, dict):
            return {key: (_PAGE_ONLY[key] if key in _PAGE_ONLY else slim(one)) for key, one in value.items()}
        if isinstance(value, list):
            return [slim(one) for one in value]
        return value
    return slim(found)


class WriteInvoker:
    """Carries out integration writes for the Engine (ADR-007 D3)."""

    def __init__(self, source, notify, label, spawn, folder):
        self._source, self._notify, self._label, self._spawn = source, notify, label, spawn
        self._folder = folder
        self._lock = threading.Lock()
        self._pending = {}       # execution id -> (job, prepared, target)
        self._closed = False
        #: capability id -> (connection id, provider, operation, connection name, local path fields)
        self.targets = {}

    def requirements(self, capability, request):
        if self._closed:
            raise QCRError("source_unavailable", "QAVector is closing.")
        return ()

    def start(self, job):
        target = self.targets.get(job.capability.capability_id)
        if target is None:
            job.fail("not_found", "Capability is not available.")
            return
        level = self._level(job, target)
        if level == "off":
            job.fail("authorization_denied", "This connection does not let %s do this." % kind_of(job.actor_id))
            return
        request = dict(job.request)
        for field in target[4]:
            found = self._local(job.actor_id, request.get(field))
            if found is None:
                job.fail("out_of_folder", "A file is taken only from the asking conversation's own folder.")
                return
            request[field] = found
        # A read of the service: never on the host's lock.
        self._spawn(lambda: self._prepare(job, target, request))

    def _level(self, job, target):
        """What the connection allows this caller's kind now (ADR-015 D5), read
        fresh: a level changed during a turn holds for its next write. None:
        no limit set, so the person approves, as for every write before."""
        kind, access = kind_of(job.actor_id), getattr(self._source, "access", None)
        if kind is None or access is None:
            return None
        try:
            return access(target[0], target[1], kind)
        except Exception:
            return "off"

    def _local(self, actor, given):
        """``given`` as a real path inside the actor's folder, or None."""
        folder = self._folder(actor)
        if not folder or not isinstance(given, str) or not given.strip():
            return None
        root = os.path.realpath(folder)
        found = os.path.realpath(os.path.join(root, os.path.expanduser(given)))
        try:
            inside = os.path.commonpath([root, found]) == root and found != root
        except ValueError:
            return None
        return found if inside else None

    def _prepare(self, job, target, request):
        connection_id, provider, operation, name, _paths = target
        try:
            prepared = self._source.prepare(connection_id, provider, operation, request)
        except WriteError as exc:
            job.fail(exc.code, str(exc))
            return
        except Exception:
            log.exception("preparing %s failed", job.capability.capability_id)
            job.fail("failed", WRITE_ERRORS["failed"])
            return
        if prepared["noop"] is not None:
            job.succeed(text_only(prepared["noop"]))
            return
        if self._level(job, target) == "post":
            # The connection lets this kind of caller write without asking (D5).
            if job.dispatched({"approval": "not_needed"}) and job.running():
                self._write(job, target, prepared["request"])
            return
        card = dict(prepared["card"], connection=connection_id, connection_name=name)
        with self._lock:
            if self._closed:
                job.fail("source_unavailable", "QAVector closed before the person answered.")
                return
            self._pending[job.execution_id] = (job, prepared, target)
        if not job.dispatched({"approval": "requested"}) or not job.progress("approval.requested", card):
            with self._lock:
                self._pending.pop(job.execution_id, None)
            return
        self._notify({"type": "integration_approval", "execution_id": job.execution_id,
                      "actor": job.actor_id, "capability": job.capability.capability_id,
                      "card": card, "editable": list(prepared["editable"])})

    def pending(self):
        with self._lock:
            return list(self._pending)

    def answer(self, execution_id, approved, by="", changes=None):
        """The person's word on a waiting write; ``changes`` may set its
        editable fields. False when no write waits for it."""
        with self._lock:
            slot = self._pending.pop(execution_id, None)
        if slot is None:
            return False
        job, prepared, target = slot
        by = str(by or "the person")[:200]
        if not approved:
            job.fail("denied", "Denied by %s." % by)
            return True
        request = copy.deepcopy(prepared["request"])
        edited = False
        for key, value in (changes or {}).items():
            if key in prepared["editable"] and isinstance(value, str):
                *path, last = key.split(".")
                place = request
                for part in path:
                    place = place.setdefault(part, {})
                if place.get(last) != value:
                    place[last], edited = value, True
        if not job.progress("approval.answered", {"approved": True, "by": by, "edited": edited}):
            return True
        job.running()
        self._spawn(lambda: self._write(job, target, request, by))
        return True

    def _write(self, job, target, request, by=""):
        connection_id, provider, operation, _name, _paths = target
        source = {"label": str(self._label(job.actor_id))[:200], "detail": job.execution_id,
                  "actor": job.actor_id}
        if by:
            source["approved_by"] = str(by)[:200]
        try:
            result = self._source.write(connection_id, provider, operation, request, source)
        except WriteError as exc:
            job.fail(exc.code, str(exc))
            return
        except Exception:
            log.exception("writing %s failed", job.capability.capability_id)
            job.fail("failed", WRITE_ERRORS["failed"])
            return
        # The issue after the change, as a read gives it (ADR-014): with its
        # HTML and editor documents a large one was over the journal's limit.
        job.succeed(text_only(result))

    def cancel(self, job):
        """Canceled or out of time: a write still waiting is dropped. One
        already approved is in flight and is not stopped (ADR-007, known limits)."""
        with self._lock:
            self._pending.pop(job.execution_id, None)

    def close(self):
        with self._lock:
            self._closed = True
            pending, self._pending = list(self._pending.values()), {}
        for job, _prepared, _target in pending:
            job.fail("source_unavailable", "QAVector closed before the person answered.")
