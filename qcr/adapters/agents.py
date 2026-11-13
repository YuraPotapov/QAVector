"""Agent tasks on the existing QCR Engine, using the existing conversation runner.

An owner registers explicit profiles. This does not grant agents permission to
launch other agents. No QAVector token is inherited or created here. Phase 5
will add delegation and its permission narrowing separately.
"""

import copy
from dataclasses import asdict
import os
import threading
import uuid

from integrations.agents import adapters, runner, sessions, store
from integrations.agents.contracts import AgentProfile, AgentTask, normalized_event
from qcr.contracts import CapabilityDescriptor, ResourceDescriptor, ResourceRef, text
from qcr.errors import QCRError
from qcr.registry import Registration

READ, EXECUTE = "agents.read", "agents.execute"
_ID = {"type": "string", "minLength": 1, "maxLength": 200}
INPUT = {"type": "object", "additionalProperties": False,
         "required": ["task_id", "message"], "properties": {
             "task_id": _ID, "message": {"type": "string", "minLength": 1, "maxLength": 100000},
             "session_id": _ID,
             "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 21600}}}
OUTPUT = {"type": "object", "additionalProperties": False,
          "required": ["task_id", "session_id", "provider", "provider_session_id", "turn", "artifacts"],
          "properties": {"task_id": _ID, "session_id": _ID, "provider": _ID,
                         "provider_session_id": {"type": "string"},
                         "turn": {"type": "object"}, "artifacts": {"type": "array", "items": {"type": "string"}}}}


class AgentRuntime:
    """Facade bound to an owner's Engine/Registry; it creates neither of them.

    Task identity is stored in execution.input, session identity in progress and
    output, raw provider streams in the existing session turn directory. Status
    is read from the Engine; there is no competing task state machine.
    """

    def __init__(self, registry, engine, project_id, sessions_root, agents_path):
        self.registry, self.engine = registry, engine
        self.project_id, self.root, self.agents_path = project_id, sessions_root, agents_path
        self._owner = object()
        self._profiles = {}
        self._jobs = {}
        self._lock = threading.RLock()
        self._closed = False

    def configure(self, profiles):
        """Trusted owner configuration only. Profiles cannot change during work."""
        with self._lock:
            if self._jobs or self._closed:
                raise QCRError("busy", "Agent profiles cannot change now.")
            configured, registrations = {}, []
            for profile in profiles:
                if not isinstance(profile, AgentProfile) or profile.project_id != self.project_id:
                    raise QCRError("invalid_contract", "Invalid agent profile scope.")
                text(profile.id, "agent id")
                if (profile.id in configured or not isinstance(profile.folder, str)
                        or not os.path.isabs(profile.folder) or not os.path.isdir(profile.folder)):
                    raise QCRError("invalid_contract", "Invalid agent profile.")
                connection = store.get(profile.connection_id, self.agents_path)
                if connection is None:
                    raise QCRError("not_found", "Agent connection is unavailable.")
                provider = adapters.get(connection["kind"])
                if profile.mode not in provider.MODES:
                    raise QCRError("unsupported", "The provider does not support this mode.")
                if profile.effort and profile.effort not in provider.EFFORTS:
                    raise QCRError("unsupported", "The provider does not support this effort.")
                if profile.restrictions is not None and not isinstance(profile.restrictions, dict):
                    raise QCRError("invalid_contract", "Agent restrictions must be an object.")
                configured[profile.id] = (copy.deepcopy(profile), connection["kind"])
                ref = ResourceRef(self.project_id, "agent~" + profile.id, "agent")
                resource = ResourceDescriptor(ref, "agents", profile.name, READ)
                caps = (CapabilityDescriptor("agents.%s.describe" % profile.id, ref, 1,
                            "Describe this agent's connection and supported features.",
                            {"type": "object", "additionalProperties": False, "properties": {}},
                            {"type": "object"}, READ, read_only=True),
                        CapabilityDescriptor("agents.%s.execute" % profile.id, ref, 1,
                            "Run one task using this agent's fixed workspace, mode and connection.",
                            INPUT, OUTPUT, EXECUTE, risk_level="high", mode="long_running",
                            supports_cancel=True, supports_idempotency=True, timeout_seconds=21600))
                registrations.append(Registration(resource, caps, lambda ref, names: {},
                                                   read_capability=self._describe, invoker=self))
            self.registry.replace(self._owner, registrations)
            self._profiles = configured

    def _profile(self, capability_id):
        return self._profiles[capability_id.split(".")[1]]

    def _describe(self, capability_id, version, request):
        profile, provider = self._profile(capability_id)
        return {"id": profile.id, "connection_id": profile.connection_id, "provider": provider,
                "mode": profile.mode, "model": profile.model, "effort": profile.effort,
                "capabilities": adapters.get(provider).capabilities().document()}

    def requirements(self, capability, request):
        profile, provider = self._profile(capability.capability_id)
        if self._closed:
            raise QCRError("source_unavailable", "The agent runtime is closed.")
        connection = store.get(profile.connection_id, self.agents_path)
        if connection is None or connection["kind"] != provider:
            raise QCRError("source_unavailable", "The agent connection has changed.")
        if not request["message"].strip():
            raise QCRError("invalid_input", "A task needs a message.")
        adapter = adapters.get(provider)
        if not adapter.resolve(connection.get("binary")):
            raise QCRError("not_installed", "The agent program is unavailable.")
        if request.get("session_id"):
            self._session(profile, provider, request["session_id"])
        # Resolve restrictions before any execution or new session is written.
        try:
            runner.effective_restrictions({"folder": profile.folder, "restrictions": profile.restrictions or {}})
        except runner.Refused as exc:
            raise QCRError(exc.kind, str(exc)) from None
        return ()

    def _session(self, profile, provider, session_id):
        try:
            session = sessions.load(self.root, session_id)
        except sessions.SessionError:
            raise QCRError("not_found", "Session is unavailable to this agent.") from None
        source = session.get("source") or {}
        if (session["connection"] != profile.connection_id or session["kind"] != provider
                or os.path.realpath(session["folder"]) != os.path.realpath(profile.folder)
                or source.get("ref") != "qcr:%s:%s" % (self.project_id, profile.id)
                or session.get("restrictions", {}) != (profile.restrictions or {})):
            raise QCRError("not_found", "Session is unavailable to this agent.")
        return session

    def timeout(self, capability, request):
        return request.get("timeout_seconds", 21600)

    def submit(self, caller, agent_id, message, *, session_id=None, idempotency_key=None, timeout_seconds=None):
        # Stable task identity on an idempotent retry, independently of execution
        # and provider IDs. An altered message still conflicts in ExecutionStore.
        identity = "\0".join((self.project_id, caller.actor_id, agent_id, idempotency_key)) if idempotency_key else None
        task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, identity) if identity else uuid.uuid4())
        request = {"task_id": task_id, "message": message}
        if session_id:
            request["session_id"] = session_id
        if timeout_seconds is not None:
            request["timeout_seconds"] = timeout_seconds
        return self.engine.invoke(caller, self.project_id, "agents.%s.execute" % agent_id, request,
                                  idempotency_key=idempotency_key)

    def task(self, caller, execution_id):
        execution = self.engine.get(caller, execution_id)
        if execution["project_id"] != self.project_id or not execution["capability_id"].startswith("agents."):
            raise QCRError("not_found", "Agent task is unavailable.")
        output = execution.get("output") or {}
        task = AgentTask(execution["input"]["task_id"], execution["capability_id"].split(".")[1],
                         execution["project_id"], execution_id, execution["state"], output.get("session_id", ""))
        return dict(asdict(task), execution=execution)

    def start(self, job):
        stop = threading.Event()
        with self._lock:
            if self._closed:
                job.fail("source_unavailable", "The agent runtime is closed.")
                return
            thread = threading.Thread(target=self._run, args=(job, stop), daemon=True)
            self._jobs[job.execution_id] = (stop, thread)
            thread.start()

    def _run(self, job, stop):
        try:
            profile, provider = self._profile(job.capability.capability_id)
            if job.finished or stop.is_set():
                return
            if job.request.get("session_id"):
                session = self._session(profile, provider, job.request["session_id"])
            else:
                connection = store.get(profile.connection_id, self.agents_path)
                if connection is None or connection["kind"] != provider:
                    raise QCRError("source_unavailable", "The agent connection has changed.")
                session = sessions.create(self.root, connection, profile.folder, profile.mode,
                    model=profile.model, effort=profile.effort, restrictions=profile.restrictions,
                    source={"ref": "qcr:%s:%s" % (self.project_id, profile.id), "kind": "qcr"})
            task_id = job.request["task_id"]
            metadata = {"task_id": task_id, "session_id": session["id"], "provider": provider,
                        "agent_id": profile.id, "connection_id": profile.connection_id}
            if not job.dispatched(metadata) or not job.running(metadata) or stop.is_set():
                return
            answer, _text = run_turn(job, self.root, self.agents_path, session, provider, task_id,
                                     metadata, job.request["message"], profile.mode,
                                     profile.model, profile.effort, stop)
            turn = answer["turn"]
            output = {"task_id": task_id, "session_id": session["id"], "provider": provider,
                      "provider_session_id": answer["session"].get("provider_session", ""),
                      "turn": turn, "artifacts": turn_artifacts(self.root, session["id"], turn)}
            if turn["state"] == sessions.COMPLETED:
                job.succeed(output)
            else:
                job.fail("agent_failed", "The agent task did not complete.", output=output)
        except (runner.Refused, QCRError, sessions.SessionError) as exc:
            job.fail(getattr(exc, "code", getattr(exc, "kind", "agent_failed")), str(exc))
        except Exception:
            job.fail("agent_failed", "The agent task could not finish; inspect its session logs.")
        finally:
            with self._lock:
                self._jobs.pop(job.execution_id, None)

    def cancel(self, job):
        with self._lock:
            slot = self._jobs.get(job.execution_id)
            if slot:
                slot[0].set()

    def close(self):
        with self._lock:
            self._closed = True
            jobs = list(self._jobs.values())
        for stop, _thread in jobs:
            stop.set()
        for _stop, thread in jobs:
            thread.join(runner.STOP_GRACE + 2)


def run_turn(job, root, agents_path, session, provider, task_id, metadata, message, mode,
             model, effort, stop):
    """One turn of ``session`` for ``job``: its events become the job's progress
    (normalized, plus ``usage.reported``). Returns ``(runner answer, the turn's
    final text)``. No skills, no planning and no QAVector access: a task is
    given exactly its folder and mode."""
    said = {"text": ""}

    def emit(event):
        normalized = normalized_event(event, provider, session["id"], task_id)
        job.progress(normalized["type"], normalized)
        if event["type"] in ("completed", "failed"):
            said["text"] = str(event.get("text") or "")
            if "tokens" in event:
                job.progress("usage.reported", dict(metadata, tokens=event["tokens"],
                             cost_usd=event.get("cost_usd")))
    answer = runner.send(root, session["id"], message, agents_path=agents_path, mode=mode,
                         model=model, effort=effort, plan=False, skills=[], emit=emit, stop=stop)
    return answer, said["text"]


def turn_artifacts(root, session_id, turn):
    """The files a turn left for diagnosis: its prompt and the program's streams."""
    where = sessions.turn_directory(root, session_id, turn["n"])
    return [os.path.join(where, name) for name in ("prompt.md", "stdout.log", "stderr.log")
            if os.path.isfile(os.path.join(where, name))]
