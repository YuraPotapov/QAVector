"""``qavector-core --integrations agents COMMAND``: agent connections and sessions.

The GUI's way in, as for every other integration: one JSON object printed, exit
0 when ``ok``, 1 for a refusal or a failure worth showing, 2 when the command
line itself is wrong.

    kinds                     the agent CLIs this build knows, and where each
                              one is on this machine - starts nothing
    list                      every connection
    save --from=F             add or change one: {"connection": {...}}
    delete ID                 forget one
    test ID                   ask the CLI who it is signed in as, and remember
                              the answer on the connection
    test --from=F             the same for one not saved yet:
                              {"connection": {...}} - nothing is remembered
    sign-in ID                the command that signs the CLI in, for the caller
                              to run and watch; --from=F as for test
    models ID                 models offered by that CLI, without sending a
                              prompt; --from=F accepts an unsaved connection

    sessions [--from=F]       the project's conversations, the most recently
                              used first: {"limit", "offset", "query", "ref"} -
                              "ref" is exactly the one a source gave
    session ID [--from=F]     one conversation and what was said in it:
                              {"after": seq, "limit": newest N}
    session-new --from=F      start one: {"connection", "folder", "mode",
                              "model", "effort", "title", "restrictions", "plan",
                              "skills", "others", "source": {"label", "detail", "ref",
                              "record"}} - asks nothing of the agent
    session-set ID --from=F   change its {"title", "mode", "model", "effort",
                              "restrictions", "skills", "plan", "others"}; "folder" only before
                              its first turn
    session-delete ID         remove it and everything kept of it
    send ID --from=F          take one turn: {"message", "mode", "model",
                              "effort", "skills", "attachments": [file paths],
                              "plan": a planning turn - questions or a plan,
                              "qavector": {"socket", "token_file"} - QAVector
                              through MCP for this turn}.
                              Prints one line per event
                              as it happens - {"event": {...}} - and the
                              answer as its last line. SIGTERM stops the agent.
    activity [--from=F]       every turn of every session, newest first:
                              {"limit", "ref"}
    changes ID [--from=F]     changes so far, including a running turn; or one
                              turn with {"turn": N}. Running turns use a live
                              git snapshot; completed turns read saved patches

    roles                     the roles agents may hand work to, and the usual
                              ones as a start (ADR-010)
    role-save --from=F        add or change one: {"role": {...}, "new": bool}
    role-delete ID            remove one

    skills                    the skills library, and what each agent has by
                              itself
    skill-look --from=F       fetch {"url"} or read {"folder"} and say what is
                              there - every file, the source, the commit.
                              Adds nothing; answers with a token
    skill-add --from=F        put in what was looked at: {"token", "picks",
                              "replace"}
    skill-update NAME         look at a library skill's source again and say
                              what changed; skill-add with "replace" takes it
    skill-remove NAME         take one out of the library

    export --from=F --to=T    connections and skills into T, readable by its
                              owner only: {"connections": [ids], "skills":
                              [names]}. Prints what went
    import --from=F           put in what an export holds: {"agent_connections",
                              "agent_skills", "mode": "merge"|"copy"}

Options: ``--agents-file=PATH``, the connections file, ``--agent-sessions-dir=
PATH``, the project's conversations, and ``--agent-skills-dir=PATH``, the
skills library - each beside the user's data by default.

``test`` starts an agent CLI only to ask who it is signed in as. ``send`` is
the one command that asks a model anything. ``skill-look`` and ``skill-update``
are the only ones that reach the network, with git.
"""

import json
import os
import sys
import time
from collections import namedtuple

USAGE = __doc__[__doc__.index("    kinds"):__doc__.index("Options: ``--agents-file")].rstrip()

#: Commands that print a line per event before their answer; the answer of one
#: of these is a single line too, so a reader can take the output line by line.
STREAMS = ("send",)

#: What a command is handed: its words, its ``--from`` file, the three paths -
#: the connections, the project's conversations, the skills library - and
#: ``--to``, the file an export is written into.
Call = namedtuple("Call", "rest source path sessions skills target read_tasks",
                  defaults=("", None))


class Usage(Exception):
    """The command line is wrong; the message says how."""


def run(words, source="", path=None, sessions_root=None, skills_root=None, target="", read_tasks=None):
    """Answer one command as a payload. Raises :class:`Usage`."""
    if not words:
        raise Usage("agents needs a command.")
    try:
        handler = COMMANDS[words[0]]
    except KeyError:
        raise Usage("Unknown agents command %r." % words[0]) from None
    return handler(Call(words[1:], source, path, sessions_root, skills_root, target or "", read_tasks))


def _document(source):
    """The JSON object a ``--from`` file holds; ``{}`` when there is none."""
    if not source:
        return {}
    with open(source, encoding="utf-8-sig") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise ValueError("--from has to hold a JSON object")
    return document


def _refusal(kind, error, **more):
    return dict(more, ok=False, kind=kind, error=error)


def _one(call, what):
    if len(call.rest) != 1 or not call.rest[0].strip():
        raise Usage("%s needs one id." % what)
    return call.rest[0].strip()


def _named(call, what):
    """``(connection, whether it is a saved one, refusal)`` for a command that
    takes a saved connection's id or, in ``--from``, one not saved yet."""
    from integrations.agents import store
    rest = call.rest
    if len(rest) == 1 and rest[0].strip():
        entry = store.get(rest[0].strip(), call.path)
        if entry is None:
            return None, False, _refusal("not_found", "No connection %r." % rest[0].strip())
        return entry, True, None
    if rest or not call.source:
        raise Usage("%s needs a connection id, or --from=FILE with one not saved yet." % what)
    entry = store.normalize(_document(call.source).get("connection") or {})
    # Not saved, so it has no name to be wrong yet: only what names the program.
    found = store.problems(dict(entry, name=entry["name"] or "-"))
    if found:
        return None, False, _refusal("invalid", " ".join(found), problems=found)
    return entry, False, None


# -- connections ----------------------------------------------------------------
def _kinds(call):
    from integrations.agents import adapters
    return {"ok": True, "kinds": adapters.describe()}


def _list(call):
    from integrations.agents import store
    return {"ok": True, "path": store.resolve_path(call.path),
            "connections": store.load(call.path)}


def _roles(call):
    """The roles agents may hand work to (ADR-010), and the usual ones as a start."""
    from integrations.agents import roles, store
    try:
        found = roles.load(call.path)
    except roles.RoleError as exc:
        return _refusal("unreadable", str(exc))
    connections = store.load(call.path)
    names = {one["id"] for one in connections}
    return {"ok": True, "path": roles.path_for(call.path),
            "roles": [dict(one, broken=one["connection"] not in names) for one in found],
            "templates": roles.templates(connections)}


def _role_save(call):
    """``{"role": {...}, "new": true}``: add a role, or change one."""
    from integrations.agents import roles
    if not call.source:
        raise Usage("role-save needs --from=FILE with the role.")
    document = _document(call.source)
    role, found = roles.upsert(document.get("role") or {}, call.path, new=bool(document.get("new")))
    if found:
        return _refusal("invalid", " ".join(found), problems=found)
    return {"ok": True, "role": role}


def _role_delete(call):
    from integrations.agents import roles
    role_id = _one(call, "role-delete")
    if not roles.delete(role_id, call.path):
        return _refusal("not_found", "No role %r." % role_id)
    return {"ok": True, "id": role_id}


def _models(call):
    from integrations.agents import adapters
    entry, _saved, refusal = _named(call, "models")
    if refusal:
        return refusal
    return adapters.get(entry["kind"]).models(entry["binary"])


def _save(call):
    from integrations.agents import store
    if not call.source:
        raise Usage("save needs --from=FILE with the connection.")
    entry, found = store.upsert(_document(call.source).get("connection") or {}, call.path)
    if found:
        return _refusal("invalid", " ".join(found), problems=found)
    return {"ok": True, "connection": entry}


def _delete(call):
    from integrations.agents import store
    connection_id = _one(call, "delete")
    if not store.remove(connection_id, call.path):
        return _refusal("not_found", "No connection %r." % connection_id)
    return {"ok": True, "id": connection_id}


def _test(call):
    from integrations.agents import adapters, store
    entry, saved, refusal = _named(call, "test")
    if refusal:
        return refusal
    adapter = adapters.get(entry["kind"])
    found = adapter.status(entry["binary"])
    account = found["email"] or found["organisation"]
    result = {"binary": found["binary"], "account": account,
              "organisation": found["organisation"], "plan": found["plan"],
              "method": found["method"], "summary": adapter.describe(found)}
    ok = bool(found["logged_in"])
    # Three different things to do about a failure: install it, sign in, or
    # read what it said.
    kind = "" if ok else ("not_installed" if not found["installed"]
                          else "signin" if found["answered"] else "error")
    if saved:
        # Remembered, so the table can say when it last worked - only for a
        # check somebody asked for.
        entry["last_check"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "ok": ok,
                               "kind": kind, "account": account, "plan": found["plan"],
                               "binary": found["binary"], "error": found["problem"]}
        store.upsert(entry, call.path)
    answer = {"ok": ok, "connection": entry["id"], "result": result}
    if saved:
        answer["last_check"] = entry["last_check"]
    if not ok:
        answer.update(error=found["problem"], kind=kind)
    return answer


def _sign_in(call):
    from integrations.agents import adapters
    entry, _saved, refusal = _named(call, "sign-in")
    if refusal:
        return refusal
    adapter = adapters.get(entry["kind"])
    if not adapter.resolve(entry["binary"]):
        return _refusal("not_installed", "%s is not installed, or %r is not on PATH."
                        % (adapter.NAME, entry["binary"] or adapter.DEFAULT_BINARY))
    return {"ok": True, "connection": entry["id"],
            "argv": adapter.login_argv(entry["binary"]),
            "summary": "Sign in through the browser",
            "detail": "%s opens your browser and keeps the credentials itself. "
                      "Nothing is stored in this application." % adapter.NAME}


# -- sessions -------------------------------------------------------------------
def _settings(document, adapter, current=None, skills_root=None):
    """``(what a session is set to, problems)`` from what a form sent.

    Only the keys that were sent are answered for, so changing a title does
    not have to repeat the rest.
    """
    current = current or {}
    found, changed = [], {}
    if "skills" in document:
        from integrations.agents import skills
        names = document.get("skills") or []
        if not isinstance(names, list) or not all(isinstance(one, str) for one in names):
            found.append("Skills is a list of names from the library.")
        else:
            try:
                skills.resolve(skills_root, names)
                changed["skills"] = list(dict.fromkeys(names))
            except skills.SkillError as exc:
                found.append(str(exc))
    if "title" in document:
        changed["title"] = str(document.get("title") or "").strip()
    if "mode" in document or not current:
        mode = str(document.get("mode") or adapter.MODES[0]).strip()
        if mode not in adapter.MODES:
            found.append("Mode must be one of %s." % ", ".join(adapter.MODES))
        changed["mode"] = mode
    for key in ("model", "effort"):
        if key in document:
            changed[key] = str(document.get(key) or "").strip()
    if "wake" in document:
        from integrations.agents import store
        if document["wake"] not in store.WAKES:
            found.append("When tasks finish must be one of %s." % ", ".join(store.WAKES))
        else:
            changed["wake"] = document["wake"]
    if "others" in document:
        from integrations.agents import store
        if document["others"] not in store.OTHERS:
            found.append("Other agents must be one of %s." % ", ".join(store.OTHERS))
        else:
            changed["others"] = document["others"]
    if "plan" in document:
        if not isinstance(document["plan"], bool):
            found.append("Plan is true or false.")
        elif document["plan"] and not getattr(adapter, "PLANS", False):
            found.append("%s cannot plan before it works." % adapter.NAME)
        else:
            changed["plan"] = document["plan"]
    if changed.get("effort") and changed["effort"] not in adapter.EFFORTS:
        found.append("Effort must be one of %s, or blank for the connection's."
                     % ", ".join(adapter.EFFORTS))
    if "restrictions" in document:
        raw = document.get("restrictions") or {}
        from execution_runtime import restrictions
        from execution_runtime.errors import RuntimePolicyError
        try:
            restrictions.parse(raw, "restrictions")
            changed["restrictions"] = dict(raw)
        except RuntimePolicyError as exc:
            found.append(str(exc))
    return changed, found


def _source(document):
    from integrations.agents import sessions
    source = document.get("source")
    if not isinstance(source, dict):
        return None
    return sessions.normal_source(source)


def _sessions(call):
    from integrations.agents import sessions
    document = _document(call.source)
    answer = sessions.listing(call.sessions, document.get("limit") or 0,
                              document.get("offset") or 0, document.get("query") or "",
                              ref=str(document.get("ref") or ""))
    return dict(answer, ok=True, path=sessions.resolve_root(call.sessions))


def _usage(call):
    """What agent work used and cost (QCR ADR-008): ``{"ref": ...}`` for an
    issue's conversations with what they handed on, ``{"session": ...}`` for one
    conversation, else totals with filters ``since``, ``until``, ``connection``,
    ``model``, ``kind``, ``cycle``, ``run`` and breakdowns ``by``."""
    from integrations.agents import sessions, usage
    document = _document(call.source)
    try:
        if document.get("ref"):
            return dict(usage.for_ref(call.sessions, str(document["ref"])), ok=True)
        if document.get("session"):
            return dict(usage.conversation(call.sessions, str(document["session"])), ok=True)
        filters = {key: document[key] for key in ("since", "until", "connection", "model", "kind", "cycle", "run",
                                                  "role") if document.get(key) not in (None, "")}
        by = tuple(one for one in document.get("by") or ("model", "connection", "kind", "cycle")
                   if one in ("model", "connection", "kind", "cycle", "role"))
        return dict(usage.summary(call.sessions, by=by, **filters), ok=True)
    except sessions.SessionError as exc:
        return _refusal(exc.kind, str(exc))


def _session_link(call):
    """Link a conversation to an issue, or undo a link (QCR ADR-014):
    ``{"link": "jira:<connection>:<KEY>"}`` or ``{"unlink": ...}``."""
    from integrations.agents import sessions
    session_id = _one(call, "session")
    document = _document(call.source)
    try:
        if document.get("link"):
            sessions.link(call.sessions, session_id, str(document["link"]))
        if document.get("unlink"):
            sessions.unlink(call.sessions, session_id, str(document["unlink"]))
        return {"ok": True, "session": sessions.described(call.sessions, sessions.load(call.sessions, session_id))}
    except sessions.SessionError as exc:
        return _refusal(exc.kind, str(exc))


def _budget(call):
    """A conversation's budget and how far it is spent (QCR ADR-013): read it, or
    with ``{"budget": {"usd", "tokens"}}`` set its own (``{}``: none)."""
    from integrations.agents import budgets, sessions, store
    session_id = _one(call, "session")
    document = _document(call.source)
    try:
        session = sessions.load(call.sessions, session_id)
        connection = store.get(session.get("connection") or "", call.path)
        if "budget" in document:
            budgets.save(call.sessions, session_id, document.get("budget") or {})
        task = (session.get("source") or {}).get("label") == "Delegated"
        found = budgets.of_task(call.sessions, session_id, connection) if task \
            else budgets.of_conversation(call.sessions, session_id, connection)
        return dict(found, ok=True, session=session_id, scope="task" if task else "conversation")
    except sessions.SessionError as exc:
        return _refusal(exc.kind, str(exc))


def _session(call):
    from integrations.agents import sessions
    session_id = _one(call, "session")
    document = _document(call.source)
    try:
        session = sessions.load(call.sessions, session_id)
    except sessions.SessionError as exc:
        return _refusal(exc.kind, str(exc))
    return {"ok": True, "session": sessions.described(call.sessions, session),
            "events": sessions.events(call.sessions, session_id,
                                      document.get("after") or 0, document.get("limit") or 0)}


def _session_new(call):
    from integrations.agents import adapters, sessions, store
    if not call.source:
        raise Usage("session-new needs --from=FILE with the session.")
    document = _document(call.source)
    name = str(document.get("connection") or "").strip()
    connection = store.get(name, call.path)
    if connection is None:
        return _refusal("no_connection", "No agent connection %r." % name)
    adapter = adapters.get(connection["kind"])
    settings, found = _settings(document, adapter, skills_root=call.skills)
    if found:
        return _refusal("invalid", " ".join(found), problems=found)
    try:
        session = sessions.create(
            call.sessions, connection, document.get("folder") or "", settings["mode"],
            settings.get("model", ""), settings.get("effort", ""), settings.get("title", ""),
            _source(document), settings.get("restrictions"), settings.get("skills"),
            plan=settings.get("plan", False), others=settings.get("others"), wake=settings.get("wake"))
    except sessions.SessionError as exc:
        return _refusal(exc.kind, str(exc))
    return {"ok": True, "session": sessions.described(call.sessions, session)}


def _session_set(call):
    from integrations.agents import adapters, sessions
    session_id = _one(call, "session-set")
    if not call.source:
        raise Usage("session-set needs --from=FILE with what to change.")
    document = _document(call.source)
    try:
        session = sessions.load(call.sessions, session_id)
        adapter = adapters.get(session["kind"])
        settings, found = _settings(document, adapter, current=session,
                                    skills_root=call.skills)
        if "folder" in document:
            folder = os.path.abspath(os.path.expanduser(str(document.get("folder") or "")))
            if session.get("turns"):
                # The CLI keeps a conversation under the folder it began in.
                found.append("The folder cannot change once the conversation has begun.")
            elif not os.path.isdir(folder):
                found.append("%s is not a folder." % folder)
            settings["folder"] = folder
        if found:
            return _refusal("invalid", " ".join(found), problems=found)
        # Under the lock: a turn being taken is writing this same record.
        with sessions.lock(call.sessions, session_id):
            session = sessions.load(call.sessions, session_id)
            session.update(settings)
            sessions.save(call.sessions, session)
    except sessions.SessionError as exc:
        return _refusal(exc.kind, str(exc))
    except adapters.UnknownKind as exc:
        return _refusal("invalid", str(exc))
    return {"ok": True, "session": sessions.described(call.sessions, session)}


def _session_delete(call):
    from integrations.agents import sessions
    session_id = _one(call, "session-delete")
    try:
        sessions.delete(call.sessions, session_id)
    except sessions.SessionError as exc:
        return _refusal(exc.kind, str(exc))
    return {"ok": True, "id": session_id}


def _send(call):
    """One turn, printed as it happens. SIGTERM - how a page's Stop ends this
    process - stops the agent, and the turn is still recorded and answered."""
    import signal
    import threading
    from integrations.agents import runner
    session_id = _one(call, "send")
    if not call.source:
        raise Usage("send needs --from=FILE with the message.")
    document = _document(call.source)
    stop = threading.Event()
    listening = [True]

    def emit(event):
        # A reader that went away must not end the turn: what the agent does
        # is still recorded, and the next reader finds it there.
        if not listening[0]:
            return
        try:
            sys.stdout.write(json.dumps({"event": event}, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except (OSError, ValueError):
            listening[0] = False

    handlers = {}
    for name in ("SIGTERM", "SIGINT"):
        number = getattr(signal, name, None)
        if number is not None:
            try:
                handlers[number] = signal.signal(number, lambda *_: stop.set())
            except ValueError:          # not the main thread: nobody can signal us
                pass
    try:
        answer = runner.send(call.sessions, session_id, document.get("message"),
                             agents_path=call.path, mode=document.get("mode"),
                             model=document.get("model"), effort=document.get("effort"),
                             skills=document.get("skills"), skills_root=call.skills,
                             emit=emit, stop=stop, attachments=document.get("attachments"),
                             plan=document.get("plan"), qavector=document.get("qavector"),
                             trigger=document.get("trigger"), read_tasks=call.read_tasks)
    except runner.Refused as exc:
        return _refusal(exc.kind, str(exc))
    finally:
        for number, previous in handlers.items():
            signal.signal(number, previous)
    turn = answer["turn"]
    ok = turn["state"] == "completed"
    answer = dict(answer, ok=ok)
    if not ok:
        answer.update(kind=turn["state"], error=turn["error"] or "The turn was stopped.")
    return answer


def _activity(call):
    from integrations.agents import sessions
    document = _document(call.source)
    return {"ok": True, "activity": sessions.activity(
        call.sessions, 500 if document.get("limit") is None else document.get("limit"),
        ref=str(document.get("ref") or ""))}


def _changes(call):
    from integrations.agents import changes, sessions
    session_id = _one(call, "changes")
    document = _document(call.source)
    try:
        session = sessions.load(call.sessions, session_id)
    except sessions.SessionError as exc:
        return _refusal(exc.kind, str(exc))
    where = sessions.directory(call.sessions, session_id)
    turn = document.get("turn")
    turn_dir = None
    if turn is not None:
        if not isinstance(turn, int) or isinstance(turn, bool) or not (
                1 <= turn <= len(session.get("turns") or [])):
            return _refusal("invalid", "This conversation has no turn %r." % (turn,))
        turn_dir = sessions.turn_directory(call.sessions, session_id, turn)
    turns = session.get("turns") or []
    active = turns[-1] if turns else {}
    if (active.get("state") == sessions.RUNNING and turn in (None, active.get("n"))
            and sessions.held(call.sessions, session_id)):
        try:
            found = changes.preview(where, sessions.turn_directory(
                call.sessions, session_id, active["n"]), session["folder"], whole=turn is None)
        except (changes.ChangesError, OSError, ValueError) as exc:
            return _refusal("changes_unavailable", str(exc))
    else:
        found = changes.read(where, turn_dir)
    return dict(found, ok=True, session=session_id, turn=turn)


# -- skills ---------------------------------------------------------------------
def _skill_call(work):
    """Answer a skills command, its refusal turned into something a page shows."""
    from integrations.agents import skills
    try:
        return dict(work(skills), ok=True)
    except skills.SkillError as exc:
        return _refusal(exc.kind, str(exc))


def _skills(call):
    """The library, and - for information - what each agent has by itself."""
    from integrations.agents import adapters

    def work(skills):
        return {"path": skills.resolve_root(call.skills), "skills": skills.listing(call.skills),
                "own": {kind: adapters.get(kind).own_skills() for kind in adapters.KINDS}}

    return _skill_call(work)


def _skill_look(call):
    if not call.source:
        raise Usage("skill-look needs --from=FILE with where to look.")
    document = _document(call.source)
    return _skill_call(lambda skills: skills.look(call.skills, document))


def _skill_add(call):
    if not call.source:
        raise Usage("skill-add needs --from=FILE with what was looked at.")
    document = _document(call.source)
    return _skill_call(lambda skills: {"skills": skills.add(
        call.skills, document.get("token"), document.get("picks") or [],
        replace=bool(document.get("replace")))})


def _skill_update(call):
    name = _one(call, "skill-update")
    return _skill_call(lambda skills: skills.look_again(call.skills, name))


def _skill_remove(call):
    name = _one(call, "skill-remove")

    def work(skills):
        if not skills.remove(call.skills, name):
            raise skills.SkillError("No skill %r in the library." % name, "not_found")
        return {"name": name}

    return _skill_call(work)


# -- export and import ---------------------------------------------------------
def _export(call):
    """Connections and skills into ``--to``, readable by its owner only. Prints
    what went, by name; never a file's contents."""
    from integrations.agents import skills, store
    if not call.source or not call.target:
        raise Usage("export needs --from=FILE with what to export and --to=FILE.")
    document = _document(call.source)
    try:
        found = {"agent_connections": store.export(document.get("connections") or [],
                                                   call.path),
                 "agent_skills": skills.export(call.skills, document.get("skills") or [])}
    except skills.SkillError as exc:
        return _refusal(exc.kind, str(exc))
    descriptor = os.open(call.target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(found, handle, ensure_ascii=False)
    os.chmod(call.target, 0o600)
    return {"ok": True, "connections": [one["id"] for one in found["agent_connections"]],
            "skills": [one["name"] for one in found["agent_skills"]]}


def _import(call):
    """Put in what an export file holds: ``{"agent_connections", "agent_skills",
    "mode": "merge"|"copy"}``. Answers with what was done, said."""
    from integrations.agents import skills, store
    if not call.source:
        raise Usage("import needs --from=FILE with what to import.")
    document = _document(call.source)
    mode = "copy" if document.get("mode") == "copy" else "merge"
    said = []
    if document.get("agent_connections"):
        said.extend(store.import_entries(document["agent_connections"], mode, call.path))
    if document.get("agent_skills"):
        said.extend(skills.import_skills(call.skills, document["agent_skills"], mode))
    return {"ok": True, "said": said}


COMMANDS = {"kinds": _kinds, "models": _models, "list": _list, "save": _save, "delete": _delete,
            "roles": _roles, "role-save": _role_save, "role-delete": _role_delete,
            "test": _test, "sign-in": _sign_in,
            "sessions": _sessions, "session": _session, "usage": _usage, "budget": _budget, "session-link": _session_link, "session-new": _session_new,
            "session-set": _session_set, "session-delete": _session_delete,
            "send": _send, "activity": _activity, "changes": _changes,
            "skills": _skills, "skill-look": _skill_look, "skill-add": _skill_add,
            "skill-update": _skill_update, "skill-remove": _skill_remove,
            "export": _export, "import": _import}
