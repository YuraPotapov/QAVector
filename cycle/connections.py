"""A cycle step that names a project connection instead of carrying a site and a token.

A Jira step either says where and who itself - ``site``, ``email``, ``token`` and
the rest - or names a connection of the project and says none of that::

    - id: queue
      plugin: jira.issues
      with:
        connection: product-team
        project: QA

An agent step does the same with an agent connection, which is QAVector's
rather than the project's - ``integrations/agents``, Integrations -> Agents::

    - id: review
      plugin: agent.review
      with:
        connection: claude-code      # instead of framework and claude
        task: ...

This is the cycle's side of that, and all of it: the integrations package knows
nothing about cycles, steps or plugins. It offers a connection with its
credentials (``integrations.store.credentials``) and a log of changes
(``integrations.activity``); a cycle reads the one and writes to the other.
For agents the log is the conversations record: every call an agent step makes
goes into it (:func:`record_agent_call`), saying which cycle and step made it.

Just before a step executes - after its ``${...}`` are resolved and after
anything shown for approval, which therefore names the connection and never its
token, and before any sandbox, which cannot read the project's files - the
connection is expanded into exactly the fields the step would otherwise have
carried. A listener does the same before it asks the queue. So the plugins keep
one way of being configured, and every cycle written the old way keeps working.
"""

import os

from cycle import variables

#: What a connection stands for in a step's settings.
#: ``scope`` is the connection's predefined query, resolved: a step that names a
#: connection is bounded by it and cannot set it itself.
FIELDS = ("site", "flavour", "auth", "email", "token", "verify_tls", "api", "scope")

#: Plugins that change something through a connection, and what the
#: integration's log calls that change. Reads are not logged.
CHANGES = {"jira.transition": "transition"}

#: Steps that run an agent's program, and may name an agent connection.
AGENT_PLUGINS = frozenset(("agent.review", "agent.edit", "agent.implement", "agent.merge"))

#: What an agent connection stands for in a step's settings: which program runs
#: it. A step that names a connection says none of these itself.
AGENT_FIELDS = ("claude", "python", "model_client", "api_key_env")

#: The only agent programs a cycle step can run so far.
AGENT_KINDS = ("claude",)

#: Where, in what :func:`load` returns, a run keeps what it knows of agents:
#: where conversations are recorded. Not a connection id - those never start
#: with "@" - so no step can name it.
AGENTS = "@agents"


def _agent_key(name):
    """Where an agent connection is kept in what :func:`load` returns: apart
    from the project's own, so the two can share an id without one hiding the
    other."""
    return "agents/%s" % name


class BindingError(Exception):
    """The connection a step names cannot be used for it."""


def problems(settings):
    """``(names a connection, problems)`` for a step's unresolved settings.

    A step gives a connection or the fields it stands for, never both: two
    sources for the same site would leave a reader guessing which one won. And
    the connection may read only the cycle's variables, because which account a
    step acts as is decided before the run starts.
    """
    return _problems(settings, FIELDS, "the connection already says where and who.",
                     lambda key: key in settings)


def agent_problems(settings):
    """:func:`problems` for an agent step: a connection, or the program the
    step names itself - ``claude``, a worker framework and its interpreter -
    never both. A field left empty says nothing, so it is no second answer."""
    settings = settings or {}
    connected, found = _problems(
        settings, AGENT_FIELDS, "the connection already says which program runs it.",
        lambda key: settings.get(key) not in (None, ""))
    framework = settings.get("framework")
    if connected and framework not in (None, "", "claude_cli"):
        found.append("A connection runs Claude Code; framework %s is not one." % framework)
    return connected, found


def _problems(settings, fields, why, given):
    settings = settings or {}
    value = settings.get("connection")
    if value in (None, ""):
        return False, []
    found = []
    if not isinstance(value, str):
        return True, ["connection must be text."]
    both = [key for key in fields if given(key)]
    if both:
        found.append("Give Connection or %s, not both: %s" % (", ".join(both), why))
    if any(not ref.startswith("vars.") for ref in variables.REFERENCE.findall(value)):
        found.append("connection may read only ${vars...}: which account a step "
                     "uses is decided before the run starts.")
    return True, found


def named_by(cycle, run_variables=None, env=None, agents=False):
    """``(connection ids the cycle's steps name, problems)``.

    A step's ``connection`` is a name or a ``${vars...}`` reference: which
    connection a step uses is decided before the run starts, so a reference
    to another step's output is a problem here, said before anything runs.

    ``agents`` says which: the project's connections (the default, what every
    caller from before agent steps means), or the agent connections.
    """
    values = dict(getattr(cycle, "variables", {}) or {})
    values.update(run_variables or {})
    root = {"vars": values, "env": dict(os.environ if env is None else env),
            "run": {}, "steps": {}}
    ids, found = [], []
    for step in getattr(cycle, "steps", ()) or ():
        if (step.plugin in AGENT_PLUGINS) != bool(agents):
            continue
        raw = (step.settings or {}).get("connection")
        if raw in (None, ""):
            continue
        try:
            name = str(variables.resolve(raw, root) or "").strip()
        except variables.ResolveError as exc:
            found.append("%s: connection %s" % (step.id, exc))
            continue
        if name and name not in ids:
            ids.append(name)
    return ids, found


def load(cycle, run_variables=None, path=None, secrets_path=None, env=None,
         environments_path=None, agents_path=None, agent_sessions=None,
         include_agents=True):
    """``({id: connection with its credentials}, problems)`` for one run.

    Only the connections the cycle's steps name are read, and each one must be
    usable - a run that would fail on its third step for a missing connection
    is refused before its first.

    ``agents_path`` is the agent connections file and ``agent_sessions`` where
    this project's conversations with agents are kept; given, every call an
    agent step makes is recorded there. ``include_agents`` False reads the
    project's connections alone - for a listener, which runs no agent.
    """
    ids, found = named_by(cycle, run_variables, env)
    connections = {}
    if ids:
        from integrations import store
        for connection_id in ids:
            try:
                connections[connection_id] = store.credentials(
                    connection_id, path, secrets_path, environments_path)
            except (store.Unavailable, store.StoreError) as exc:
                found.append(str(exc))
    if not include_agents:
        return connections, found
    agent_ids, wrong = named_by(cycle, run_variables, env, agents=True)
    found.extend(wrong)
    if agent_ids:
        from integrations.agents import store as agents
        try:
            known = {one["id"]: one for one in agents.load(agents_path)}
        except agents.StoreError as exc:
            known = {}
            found.append(str(exc))
        for connection_id in agent_ids:
            one = known.get(connection_id)
            if one is None:
                found.append("No agent connection %r. Add it in Integrations -> Agents "
                             "-> Connections." % connection_id)
            elif one.get("kind") not in AGENT_KINDS:
                found.append("%s runs %s; a cycle step can run %s only so far."
                             % (one.get("name") or connection_id, one.get("kind"),
                                ", ".join(AGENT_KINDS)))
            else:
                connections[_agent_key(connection_id)] = dict(one, provider="agents")
    if agent_sessions:
        connections[AGENTS] = {"sessions": agent_sessions}
    return connections, found


def expand(plugin, settings, connections):
    """``settings`` with the named connection's fields in place of its name.

    ``connections`` is what :func:`load` read. Settings that name no connection
    come back as they are. Raises :class:`BindingError`.
    """
    name = (settings or {}).get("connection")
    if name in (None, ""):
        return settings
    metadata = plugin.metadata
    name = str(name).strip()
    if metadata.id in AGENT_PLUGINS:
        return _expand_agent(metadata, settings, name, connections)
    connection = (connections or {}).get(name)
    if connection is None:
        raise BindingError("No connection %r in this project. Add it in "
                           "Integrations -> Jira -> Connections." % name)
    provider = metadata.id.split(".", 1)[0]
    if connection.get("provider", "jira") != provider:
        raise BindingError("%s is a %s connection; %s needs a %s one."
                           % (connection.get("name") or name,
                              connection.get("provider"), metadata.id, provider))
    renew = connection.get("renew")
    try:
        # A run can outlast an access token: each step asks for one good now.
        token = renew() if callable(renew) else connection.get("token")
    except Exception as exc:                # noqa: BLE001 - said as the step's failure
        raise BindingError(str(exc))
    if not token:
        raise BindingError("Connection %r has no token. Set it in Integrations "
                           "-> %s -> Connections." % (connection.get("name") or name, provider.capitalize()))
    values = dict(connection, token=token, api=connection.get("api") or "")
    if connection.get("auth") == "oauth":
        # Signed in through Atlassian: a bearer token, for the API gateway.
        values.update(auth="bearer", email="")
    accepted = {one.key for one in metadata.inputs}
    expanded = {key: value for key, value in settings.items() if key != "connection"}
    for key in FIELDS:
        if key in accepted:
            expanded[key] = values.get(key)
    # The whole connection, for a step that says it posts through its integration
    # and so keeps its record, limits and access (Teams, ADR-015).
    if getattr(plugin, "TAKES_CONNECTION", False):
        expanded["_connection"] = values
    return expanded


def _expand_agent(metadata, settings, name, connections):
    """An agent step's settings with its connection's program in place of its
    name: ``claude`` (blank is found as :func:`_program` says), the CLI backend, and the
    connection's model and effort wherever the step left those blank - the step
    may still ask for another."""
    connection = (connections or {}).get(_agent_key(name))
    if connection is None:
        raise BindingError("No agent connection %r. Add it in Integrations -> Agents "
                           "-> Connections." % name)
    accepted = {one.key for one in metadata.inputs}
    expanded = {key: value for key, value in settings.items() if key != "connection"}
    if "claude" in accepted:
        expanded["claude"] = _program(connection)
    if "framework" in accepted:
        expanded["framework"] = "claude_cli"
    for key in ("model", "effort"):
        if key in accepted and expanded.get(key) in (None, "") and connection.get(key):
            expanded[key] = connection[key]
    return expanded


def _program(connection):
    """The program an agent connection runs: the path it names, or - blank -
    wherever its CLI is found now, PATH first and then where its installers put
    it (the same lookup as the Agents integration's). Still blank when it is
    nowhere, so the step's own check says it cannot be found."""
    binary = connection.get("binary") or ""
    if binary:
        return binary
    from integrations.agents import adapters
    try:
        return adapters.get(connection.get("kind") or "claude").resolve("")
    except adapters.UnknownKind:
        return ""


#: The file in a step's folder that says which conversation its agent calls
#: are recorded in, so a resumed run goes on in the same one.
AGENT_SESSION_FILE = "agent-session.json"


def _mode(tools):
    """What a call's tools let the agent do, in the words a conversation uses."""
    names = [str(one).split("(", 1)[0] for one in tools or ()]
    if "Bash" in names:
        return "full"
    if any(one in ("Edit", "Write", "MultiEdit", "NotebookEdit") for one in names):
        return "edit"
    return "read"


def record_agent_call(context, step, call):
    """Put one call an agent step made into the agents' conversations record.

    Every call, the steps that name a connection and the ones that name their
    program themselves alike: the Agents integration keeps an account of
    everything any agent was asked and did in the project. A step's calls are
    one conversation, its source the cycle and the step - in the integration's
    words, which it shows and never reads.

    ``call`` says what the call was: ``prompt``, ``cwd``, ``tools``, ``model``,
    ``effort``, ``started``, ``ended``, ``state``, ``error``, ``cost_usd``,
    ``steps``, ``provider_session``, ``stdout`` and ``prompt_file`` (the run's
    own files, which are linked to, not copied).

    Never raises, and records nothing when the run was not told where the
    record is: the step's own outcome is what counts.
    """
    try:
        _record_agent_call(context, step, call)
    except Exception as exc:                # noqa: BLE001 - a record, never the step
        import logging
        logging.getLogger("cycle.connections").warning(
            "The agent call of %s was not recorded: %s", getattr(step, "id", "?"), exc)


def _record_agent_call(context, step, call):
    connections = getattr(context, "connections", None) or {}
    root = (connections.get(AGENTS) or {}).get("sessions")
    if not root:
        return
    import json
    from integrations.agents import adapters, runner, sessions
    name = str(getattr(step, "connection", "") or "")
    connection = connections.get(_agent_key(name)) if name else None
    owner = {"id": name, "kind": "claude",
             "name": (connection or {}).get("name") or "Claude Code"}
    mode = _mode(call.get("tools"))
    where = os.path.join(context.step_dir(step.id), AGENT_SESSION_FILE)
    session_id = ""
    try:
        with open(where, encoding="utf-8") as handle:
            session_id = str(json.load(handle).get("session") or "")
        sessions.load(root, session_id)
    except (OSError, ValueError, AttributeError, sessions.SessionError):
        session_id = ""
    if not session_id:
        cycle = getattr(context, "cycle", None)
        run = getattr(context, "run", None)
        cycle_name = getattr(cycle, "name", "") or getattr(cycle, "id", "")
        label = getattr(step, "label", "") or step.id
        made = sessions.create(
            root, owner, call.get("cwd") or context.workspace, mode,
            model=call.get("model") or "", effort=call.get("effort") or "",
            title="%s · %s" % (cycle_name, label),
            # The ref is for summing by cycle, run and step (ADR-008 D5); the
            # label and detail are for a person.
            source={"label": "Cycle %s · step %s" % (cycle_name, label),
                    "detail": "Run %s" % getattr(run, "id", ""), "record": True,
                    "ref": "cycle:%s:%s:%s" % (getattr(cycle, "id", "") or cycle_name,
                                               getattr(run, "id", ""), step.id)})
        session_id = made["id"]
        with open(where, "w", encoding="utf-8") as handle:
            json.dump({"session": session_id}, handle)
    reader = runner.Reader(adapters.get("claude"))
    events = [{"type": "user", "text": call.get("prompt") or "", "mode": mode,
               "at": call.get("started")}]
    ended = False
    try:
        with open(call.get("stdout") or "", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                for event in reader.events(line):
                    ended = ended or event["type"] in ("completed", "failed")
                    events.append(event)
    except OSError:
        pass
    state = call.get("state") or "failed"
    if not ended:
        # The program said nothing of how it ended, so the record does.
        events.append({"type": state, "error": call.get("error") or ""})
    # Recorded after the fact: the end is when the call ended, not now.
    events[-1] = dict(events[-1], at=call.get("ended"))
    turn = {
        "started": call.get("started"), "ended": call.get("ended"), "state": state,
        "mode": mode, "model": call.get("model") or "", "effort": call.get("effort") or "",
        "skills": [], "cost_usd": call.get("cost_usd"), "steps": call.get("steps"),
        # Claude's own figure for the call, priced as the API would be (ADR-008 D3).
        "cost_kind": "estimate" if isinstance(call.get("cost_usd"), (int, float)) else "unknown",
        "error": call.get("error") or "", "provider_session": call.get("provider_session") or "",
        "files": {"prompt": call.get("prompt_file") or "", "stdout": call.get("stdout") or ""},
    }
    # Each call is a conversation of its own to the program, so what it
    # reports is the call's own; the tokens and context come from the stream.
    end = next((one for one in reversed(events) if one["type"] in ("completed", "failed")), {})
    for key in ("tokens", "context"):
        if end.get(key):
            turn[key] = end[key]
    sessions.append_turn(root, session_id, turn, events)


def expand_for_action(plugin, settings, agents_path=None):
    """An agent step's settings, as asked about outside a run - "is it signed
    in" - with its connection's program in place of its name, as a run would
    have them. Settings that name no agent connection come back as they are.
    Raises :class:`BindingError`."""
    name = str((settings or {}).get("connection") or "").strip()
    if not name or plugin.metadata.id not in AGENT_PLUGINS or "${" in name:
        return settings
    from integrations.agents import store as agents
    try:
        found = agents.get(name, agents_path)
    except agents.StoreError as exc:
        raise BindingError(str(exc))
    connections = {_agent_key(name): dict(found, provider="agents")} if found else {}
    return expand(plugin, settings, connections)


def record(connections, name, step, step_run, cycle_id, run_id):
    """Write a change a step made through a connection into the integration's
    log, saying which cycle made it. Never raises: the step's own outcome is
    what counts."""
    connection = (connections or {}).get(name) or {}
    operation = CHANGES.get(step.plugin)
    if not connection.get("log") or not operation:
        return
    from integrations import activity
    outputs = getattr(step_run, "outputs", None) or {}
    target = outputs.get("key") or ", ".join(outputs.get("keys") or [])
    try:
        activity.record(connection["log"], name, operation, target,
                        step_run.status == "success", step_run.message,
                        source={"label": "Cycle %s · step %s" % (cycle_id, step.id),
                                "detail": "Run %s" % run_id})
    except Exception:                       # noqa: BLE001 - a log line, never the step
        pass


def redactions(connections):
    """The values a run must never show, keyed so no variable can be called that."""
    return {"@connection.%s" % name: one.get("token", "")
            for name, one in (connections or {}).items() if one.get("token")}
