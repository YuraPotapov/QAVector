"""One turn of a conversation: say something to the agent, and keep what it did.

A turn is one run of the agent's CLI. The conversation carries over between
runs because the CLI keeps it and is told which one to continue; nothing here
holds a process open between turns, so there is nothing to lose when the
window, or QAVector, is closed while nobody is speaking.

What a turn does, in order: check everything that can be checked without
starting anything, write down what is about to be said, start the CLI where the
session says - inside the folder's restrictions when it has any - hand it the
message, and read its stream, recording each event before anybody is told of
it. Whatever happens to the process, the turn ends with one line in the
session saying how.

**A turn is never sent twice.** One that ended without an answer may still
have been received and acted on; it is recorded as failed and left to the
person, who can see what it did and say what to do next.
"""

import os
import signal
import subprocess
import threading
import time
import uuid

import runtime_paths
from integrations.agents import adapters, sessions, skills as skills_mod, store
from integrations.agents import attachments as attachment_files

#: How long a stopped CLI is given to end by itself before it is killed.
STOP_GRACE = 5.0

#: How much of what the CLI wrote to stderr is quoted when a turn fails.
ERROR_CHARS = 400


class Refused(Exception):
    """A turn that was not started; ``kind`` is what a page acts on.

    Nothing was said to the agent and nothing was recorded.
    """

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def effective_restrictions(session):
    """What the session's folder is held to, resolved - or None for nothing.

    Raises :class:`Refused` for restrictions no turn can run under.
    """
    raw = session.get("restrictions") or {}
    if not raw:
        return None
    from execution_runtime import restrictions
    from execution_runtime.errors import RuntimePolicyError
    try:
        effective = restrictions.resolve([("session", raw)], session["folder"])
    except RuntimePolicyError as exc:
        raise Refused("restrictions", str(exc)) from None
    if restrictions.unrestricted(effective):
        return None
    if effective["network"] == "off":
        raise Refused("restrictions", "The network is off for this session's folder, and an "
                                      "agent cannot reach its model without it.")
    if effective["network"] == "isolated":
        raise Refused("restrictions", "An isolated network is for services for now; use on "
                                      "for a session's folder.")
    return effective


def _start(argv, cwd, env, effective, folders=(), **kwargs):
    """Start the CLI: inside the folder's restrictions when it has any.

    ``folders`` are what the turn itself needs the CLI to see besides the
    session's folder - the skills laid out for it - each read-only.
    """
    if effective is None:
        return subprocess.Popen(argv, cwd=cwd, env=env, **kwargs)
    from execution_runtime.backends.linux import restricted
    prepared = restricted.prepare_command(effective, argv, cwd, env, folders)
    try:
        return subprocess.Popen(prepared.argv, cwd=prepared.cwd, env=prepared.env,
                                pass_fds=prepared.pass_fds, **kwargs)
    finally:
        prepared.close()


class Reader:
    """An agent program's stream, read into a conversation's events, line by line.

    What the adapter says of each line, and one thing no single line can say:
    whether a file was written. A tool that writes says what it would write
    when it is called; the file is recorded as written only once the program
    answers that call as done - a write the turn's mode refuses wrote nothing.

    Whoever records an agent's work uses this, a turn taken here or a call
    made by anything else, so the two records read the same.
    """

    def __init__(self, adapter):
        self.adapter = adapter
        self._writing = {}              # tool call id -> the path it asked to write

    def events(self, line):
        found = []
        for event in self.adapter.events(line):
            if event["type"] == "tool.started" and event.get("writes"):
                self._writing[event.get("id")] = event["writes"]
            found.append(event)
            if event["type"] == "tool.completed":
                path = self._writing.pop(event.get("id"), None)
                if path and event.get("ok"):
                    found.append({"type": "file.written", "path": path})
        return found


def _signal(process, hard):
    """Stop the CLI and anything it started."""
    try:
        if os.name == "nt":
            process.kill() if hard else process.terminate()
            return
        os.killpg(os.getpgid(process.pid), signal.SIGKILL if hard else signal.SIGTERM)
    except (OSError, ProcessLookupError, PermissionError):
        try:
            process.kill() if hard else process.terminate()
        except OSError:
            pass


def _tail(path, limit=ERROR_CHARS):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - limit * 4))
            text = handle.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""
    return text[-limit:]


def send(root, session_id, message, agents_path=None, mode=None, model=None,
         effort=None, skills=None, skills_root=None, emit=None, stop=None, attachments=None):
    """Take one turn. Returns ``{"session", "turn"}``; raises :class:`Refused`.

    ``mode``, ``model``, ``effort`` and ``skills`` (names in the library at
    ``skills_root``), when given, are what the session uses from this turn on.
    ``emit`` is told each event once it is on disk. ``stop`` is an Event: set,
    it ends the CLI and the turn is recorded as stopped.

    A turn that was started always returns, however it ended - ``turn["state"]``
    says how. Only a turn that could not be started at all is refused.
    """
    message = str(message or "")
    if not message.strip() and not attachments:
        raise Refused("invalid", "There is nothing to send.")
    emit = emit or (lambda event: None)
    stop = stop or threading.Event()
    chosen = {"mode": mode, "model": model, "effort": effort, "skills": skills,
              "skills_root": skills_root, "attachments": attachments if attachments is not None else []}
    try:
        sessions.load(root, session_id)             # not there: said before the lock
        with sessions.lock(root, session_id):
            # Read under the lock: what a turn before this one left is in it.
            session = sessions.load(root, session_id)
            turn = _turn(root, session, message, agents_path, chosen, emit, stop)
        # Described once the turn is let go of, or it would say it is still running.
        return {"session": sessions.described(root, session), "turn": turn}
    except sessions.SessionError as exc:
        raise Refused(exc.kind, str(exc)) from None


def _turn(root, session, message, agents_path, chosen, emit, stop):
    mode, model, effort = chosen["mode"], chosen["model"], chosen["effort"]
    connection = store.get(session["connection"], agents_path)
    if connection is None:
        raise Refused("no_connection", "The agent connection %r this session was started "
                                       "with is gone." % (session.get("connection_name")
                                                          or session["connection"]))
    try:
        adapter = adapters.get(connection["kind"])
    except adapters.UnknownKind as exc:
        raise Refused("invalid", str(exc)) from None
    mode = str(mode or session.get("mode") or adapter.MODES[0])
    if mode not in adapter.MODES:
        raise Refused("invalid", "%s has no %r mode; it has %s."
                      % (adapter.NAME, mode, ", ".join(adapter.MODES)))
    # What the session says, and under it what the connection says; blank at
    # both leaves the choice to the CLI's own configuration.
    session_model = session.get("model", "") if model is None else str(model).strip()
    session_effort = session.get("effort", "") if effort is None else str(effort).strip()
    used_model = session_model or connection.get("model", "")
    used_effort = session_effort or connection.get("effort", "")
    if used_effort and used_effort not in adapter.EFFORTS:
        raise Refused("invalid", "Effort must be one of %s, or blank for the CLI's own."
                      % ", ".join(adapter.EFFORTS))
    binary = adapter.resolve(connection.get("binary", ""))
    if not binary:
        raise Refused("not_installed", "%s is not installed, or %r is not on PATH."
                      % (adapter.NAME, connection.get("binary") or adapter.DEFAULT_BINARY))
    folder = session["folder"]
    if not os.path.isdir(folder):
        raise Refused("invalid", "This session's folder is gone: %s" % folder)
    effective = effective_restrictions(session)
    # The skills this turn takes: laid out afresh from the library each time,
    # so a skill updated or removed since the last turn is what the agent gets.
    names = (list(session.get("skills") or []) if chosen["skills"] is None
             else [str(one) for one in chosen["skills"]])
    try:
        staged = adapter.stage_skills(
            os.path.join(sessions.directory(root, session["id"]), "skills"),
            skills_mod.resolve(chosen["skills_root"], names))
    except skills_mod.SkillError as exc:
        raise Refused("skills", str(exc)) from None
    except OSError as exc:
        raise Refused("skills", "The skills could not be laid out for the agent: %s" % exc)
    seen = ([{"path": staged, "access": "read_only",
              "reason": "the skills chosen for this conversation"}] if staged else [])

    session_dir = sessions.directory(root, session["id"])
    try:
        attached = attachment_files.stage(session_dir, chosen["attachments"])
    except attachment_files.AttachmentError as exc:
        raise Refused("attachments", str(exc)) from None
    attachments_dir = os.path.join(session_dir, "attachments")
    if os.path.isdir(attachments_dir):
        # Previous messages' files remain available on later turns too.
        seen.append({"path": attachments_dir, "access": "read_only",
                     "reason": "files attached to this conversation"})
    else:
        attachments_dir = ""
    prompt = attachment_files.prompt(message, session_dir, attached)

    # From here on something is recorded whatever happens.
    number = len(session.get("turns") or []) + 1
    where = sessions.make_turn_directory(root, session["id"], number)
    _keep(os.path.join(where, "prompt.md"), prompt)
    resume = bool(session.get("provider_started") and session.get("provider_session"))
    provider = session["provider_session"] if resume else str(uuid.uuid4())
    turn = {"n": number, "started": time.time(), "ended": None, "state": sessions.RUNNING,
            "mode": mode, "model": used_model, "effort": used_effort, "skills": names,
            "cost_usd": None, "steps": None, "error": ""}
    if attached:
        turn["attachments"] = attached
    session.update(mode=mode, model=session_model, effort=session_effort, skills=names,
                   provider_session=provider, updated=turn["started"],
                   title=session.get("title") or sessions.title_from(
                       message or ", ".join(one["name"] for one in attached)))
    session.setdefault("turns", []).append(turn)
    sessions.save(root, session)

    record = sessions.Record(root, session["id"], number)
    try:
        said = {"type": "user", "text": message, "mode": mode}
        if names:
            said["skills"] = names
        if attached:
            said["attachments"] = attached
        emit(record.add(said))
        outcome = _run(adapter, adapter.turn_argv(binary, mode, used_model, used_effort,
                                                  session=provider, resume=resume,
                                                  skills_dir=staged,
                                                  attachments_dir=attachments_dir),
                       folder, effective, seen, prompt, where, record, emit, stop)
        if outcome["state"] != sessions.COMPLETED and not outcome["reported"]:
            # The CLI said nothing of how it ended, so the record does.
            emit(record.add({"type": outcome["state"], "error": outcome["error"]}))
    finally:
        record.close()

    turn.update(ended=time.time(), state=outcome["state"], error=outcome["error"],
                cost_usd=outcome["cost_usd"], steps=outcome["steps"])
    if outcome["provider_session"]:
        # The CLI has the conversation now, under the id it says it has.
        session.update(provider_session=outcome["provider_session"], provider_started=True)
    session.update(updated=turn["ended"], state=turn["state"],
                   cost_usd=round(sum(one.get("cost_usd") or 0 for one in session["turns"]), 6))
    sessions.save(root, session)
    return dict(turn)


def _keep(path, text):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, sessions.PRIVATE_FILE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text if text.endswith("\n") else text + "\n")


def _run(adapter, argv, folder, effective, seen, message, where, record, emit, stop):
    """Run the CLI once and read it to the end. Never raises for the CLI's
    sake: how it went is the answer. ``seen`` is what a restricted CLI is
    shown besides the session's folder."""
    outcome = {"state": sessions.FAILED, "error": "", "cost_usd": None, "steps": None,
               "provider_session": "", "reported": False}
    out_path, err_path = os.path.join(where, "stdout.log"), os.path.join(where, "stderr.log")
    environment = adapter.turn_env(runtime_paths.clean_subprocess_env())
    out_file = os.fdopen(os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                                 sessions.PRIVATE_FILE), "wb")
    err_file = os.fdopen(os.open(err_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                                 sessions.PRIVATE_FILE), "wb")
    with out_file, err_file:
        try:
            process = _start(argv, folder, environment, effective, seen,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err_file,
                             start_new_session=(os.name != "nt"))
        except Exception as exc:        # noqa: BLE001 - not started is an answer too
            outcome["error"] = "Cannot start %s: %s" % (adapter.NAME, exc)
            return outcome

        finished = threading.Event()

        def feed():
            # On a thread of its own: a long message fills the pipe, and the
            # CLI may write before it has read all of it.
            try:
                process.stdin.write(message.encode("utf-8"))
                process.stdin.close()
            except (OSError, ValueError):
                pass                    # it ended without reading; the stream says why

        def watch():
            while not finished.wait(0.1):
                if stop.is_set():
                    _signal(process, hard=False)
                    if not finished.wait(STOP_GRACE):
                        _signal(process, hard=True)
                    return

        threads = [threading.Thread(target=feed, daemon=True),
                   threading.Thread(target=watch, daemon=True)]
        for thread in threads:
            thread.start()
        reader = Reader(adapter)
        try:
            for raw in process.stdout:
                out_file.write(raw)
                out_file.flush()
                for event in reader.events(raw.decode("utf-8", "replace")):
                    if event.get("provider_session"):
                        outcome["provider_session"] = event["provider_session"]
                    if event["type"] in (sessions.COMPLETED, sessions.FAILED):
                        outcome.update(state=event["type"], reported=True,
                                       error=event.get("error", ""),
                                       cost_usd=event.get("cost_usd"),
                                       steps=event.get("steps"))
                    emit(record.add(event))
            code = process.wait()
        finally:
            finished.set()
            if process.poll() is None:
                _signal(process, hard=True)
                process.wait()
            for thread in threads:
                thread.join(STOP_GRACE + 1)
    if outcome["reported"]:
        return outcome
    if stop.is_set():
        outcome.update(state=sessions.STOPPED, error="")
        return outcome
    said = _tail(err_path)
    outcome["error"] = ("%s ended without an answer (exit %s)%s"
                        % (adapter.NAME, code, ": %s" % said if said else "."))
    return outcome
