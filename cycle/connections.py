"""A cycle step that names a project connection instead of carrying a site and a token.

A Jira step either says where and who itself - ``site``, ``email``, ``token`` and
the rest - or names a connection of the project and says none of that::

    - id: queue
      plugin: jira.issues
      with:
        connection: product-team
        project: QA

This is the cycle's side of that, and all of it: the integrations package knows
nothing about cycles, steps or plugins. It offers a connection with its
credentials (``integrations.store.credentials``) and a log of changes
(``integrations.activity``); a cycle reads the one and writes to the other.

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


class BindingError(Exception):
    """The connection a step names cannot be used for it."""


def problems(settings):
    """``(names a connection, problems)`` for a step's unresolved settings.

    A step gives a connection or the fields it stands for, never both: two
    sources for the same site would leave a reader guessing which one won. And
    the connection may read only the cycle's variables, because which account a
    step acts as is decided before the run starts.
    """
    settings = settings or {}
    value = settings.get("connection")
    if value in (None, ""):
        return False, []
    found = []
    if not isinstance(value, str):
        return True, ["connection must be text."]
    both = [key for key in FIELDS if key in settings]
    if both:
        found.append("Give Connection or %s, not both: the connection already "
                     "says where and who." % ", ".join(both))
    if any(not ref.startswith("vars.") for ref in variables.REFERENCE.findall(value)):
        found.append("connection may read only ${vars...}: which account a step "
                     "uses is decided before the run starts.")
    return True, found


def named_by(cycle, run_variables=None, env=None):
    """``(connection ids the cycle's steps name, problems)``.

    A step's ``connection`` is a name or a ``${vars...}`` reference: which
    connection a step uses is decided before the run starts, so a reference
    to another step's output is a problem here, said before anything runs.
    """
    values = dict(getattr(cycle, "variables", {}) or {})
    values.update(run_variables or {})
    root = {"vars": values, "env": dict(os.environ if env is None else env),
            "run": {}, "steps": {}}
    ids, found = [], []
    for step in getattr(cycle, "steps", ()) or ():
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
         environments_path=None):
    """``({id: connection with its credentials}, problems)`` for one run.

    Only the connections the cycle's steps name are read, and each one must be
    usable - a run that would fail on its third step for a missing connection
    is refused before its first.
    """
    ids, found = named_by(cycle, run_variables, env)
    if not ids:
        return {}, found
    from integrations import store
    connections = {}
    for connection_id in ids:
        try:
            connections[connection_id] = store.credentials(
                connection_id, path, secrets_path, environments_path)
        except (store.Unavailable, store.StoreError) as exc:
            found.append(str(exc))
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
                           "-> Jira -> Connections." % (connection.get("name") or name))
    values = dict(connection, token=token, api=connection.get("api") or "")
    if connection.get("auth") == "oauth":
        # Signed in through Atlassian: a bearer token, for the API gateway.
        values.update(auth="bearer", email="")
    accepted = {one.key for one in metadata.inputs}
    expanded = {key: value for key, value in settings.items() if key != "connection"}
    for key in FIELDS:
        if key in accepted:
            expanded[key] = values.get(key)
    return expanded


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
