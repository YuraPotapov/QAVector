"""``qavector-core --integrations COMMAND``: connections and their operations.

The GUI's only way in. It depends on PySide6 and nothing else, so it cannot
decrypt a token and does not try: it names a connection and an operation, and
this process reads the token, asks the service, and prints the answer.

Every command prints one JSON object and exits - 0 when ``ok``, 1 when the
answer is a refusal or a failure worth showing, 2 when the command line itself
is wrong. A token arrives only in a ``--from`` file, never in argv, and is never
printed: the only thing anybody is told about it is ``token_set``.

    list                          every connection, with token_set
    save --from=F                 add or change one: {"connection", "token",
                                  "clear_token"}
    delete ID                     forget one, and its token
    test ID                       sign in, and remember when and as whom
    call ID OPERATION [--from=F]  one operation of the connection's provider,
                                  F holding its request
    activity                      what was changed in Jira through QAVector,
                                  newest first
    export [ID...] --to=F         connections with their tokens into F, owner-
                                  readable only - for Export; prints the ids
    import --from=F               put exported connections in: {"connections",
                                  "integration_apps", "mode": "merge"|"copy"}
    app                           the project's Atlassian OAuth app, without
                                  its secret
    app-save --from=F             set it: {"client_id", "secret", "port",
                                  "service"} - a developer's settings
    sign-in [--from=F]            sign in through the browser; F may name a
                                  connection to sign in again: {"connection"}
    scope --from=F                what a predefined query comes to, with its
                                  variables put in - asks nothing of Jira:
                                  {"predefined_query", "variables_from"}

Options, anywhere on the line: ``--integrations-file=PATH`` (the project's
connections), ``--cycle-secrets-file=PATH`` (the store their tokens are in) and
``--environments-file=PATH`` (the project's environments, whose variables a
connection's predefined query may use; by default beside the connections).
"""

import json
import os
import sys
import time

USAGE = __doc__[__doc__.index("    list"):__doc__.index("Options, anywhere")].rstrip()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    options, words = {}, []
    for arg in argv:
        if arg in ("-h", "--help"):
            print("qavector-core --integrations COMMAND\n\n" + USAGE)
            return 0
        if arg.startswith("--") and "=" in arg:
            name, _, value = arg.partition("=")
            options[name] = value.strip()
        elif arg.startswith("--"):
            return _usage("Unknown option %r." % arg)
        else:
            words.append(arg)
    unknown = sorted(set(options) - {"--integrations-file", "--cycle-secrets-file", "--from",
                                     "--to", "--environments-file"})
    if unknown:
        return _usage("Unknown option %s." % ", ".join(unknown))
    if not words:
        return _usage("A command is needed.")
    path = _path(options.get("--integrations-file"))
    secrets_path = _path(options.get("--cycle-secrets-file"))
    environments = _path(options.get("--environments-file"))
    command, rest = words[0], words[1:]
    try:
        handler = COMMANDS[command]
    except KeyError:
        return _usage("Unknown command %r." % command)
    try:
        if command == "export":
            payload = _export(rest, options.get("--to") or "", path, secrets_path)
        else:
            payload = handler(rest, options.get("--from") or "", path, secrets_path,
                              environments=environments)
    except _Usage as exc:
        return _usage(str(exc))
    except Exception as exc:  # noqa: BLE001 - the report IS the error report
        payload = {"ok": False, "kind": "error",
                   "error": "%s: %s" % (type(exc).__name__, exc)}
    return _print(payload)


class _Usage(Exception):
    pass


def _path(value):
    return os.path.abspath(os.path.expanduser(value)) if value else None


def _print(payload):
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False, default=str)
    print()
    return 0 if payload.get("ok") else 1


def _usage(message):
    _print({"ok": False, "kind": "usage", "error": message, "usage": USAGE})
    return 2


def _document(source):
    """The JSON object a ``--from`` file holds; ``{}`` when there is none."""
    if not source:
        return {}
    with open(source, encoding="utf-8-sig") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise ValueError("--from has to hold a JSON object")
    return document


def _one(rest, what):
    if len(rest) != 1 or not rest[0].strip():
        raise _Usage("%s needs one connection id." % what)
    return rest[0].strip()


# -- commands -----------------------------------------------------------------
def _list(rest, source, path, secrets_path, environments=None):
    from integrations import store
    return {"ok": True, "path": store.resolve_path(path),
            "connections": store.describe(path, secrets_path)}


def _save(rest, source, path, secrets_path, environments=None):
    from integrations import store
    if not source:
        raise _Usage("save needs --from=FILE with the connection.")
    document = _document(source)
    entry, problems = store.upsert(document.get("connection") or {}, path, secrets_path,
                                   new_token=str(document.get("token") or ""),
                                   clear_token=bool(document.get("clear_token")),
                                   oauth_pending=str(document.get("oauth_pending") or ""))
    if problems:
        return {"ok": False, "kind": "invalid", "problems": problems,
                "error": " ".join(problems)}
    from integrations import scope
    settings = entry.get("settings") or {}
    # Saved all the same - the variable may be added to the environment next -
    # but said now rather than at the first query.
    warnings = scope.missing(settings.get("predefined_query"), settings.get("variables_from"),
                             environments or scope.default_environments_path(
                                 store.resolve_path(path)))
    return {"ok": True, "warnings": warnings, "connection": dict(
        entry, token_set=store.has_token(entry["id"], secrets_path))}


def _delete(rest, source, path, secrets_path, environments=None):
    from integrations import store
    connection_id = _one(rest, "delete")
    if not store.remove(connection_id, path, secrets_path):
        return {"ok": False, "kind": "not_found",
                "error": "No connection %r." % connection_id}
    return {"ok": True, "id": connection_id}


def _test(rest, source, path, secrets_path, environments=None):
    from integrations import store
    connection_id = _one(rest, "test")
    answer = _call(connection_id, "test", {}, path, secrets_path, environments)
    # Remembered, so the Connections table can say when it last worked -
    # only for a test somebody asked for, never for an ordinary read.
    entry = store.get(connection_id, path)
    if entry is not None:
        entry["last_check"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                               "ok": bool(answer.get("ok")),
                               "account": (answer.get("result") or {}).get("account", ""),
                               "error": answer.get("error", "")}
        store.upsert(entry, path, secrets_path)
    return answer


def _call_command(rest, source, path, secrets_path, environments=None):
    if len(rest) != 2:
        raise _Usage("call needs a connection id and an operation.")
    return _call(rest[0], rest[1], _document(source), path, secrets_path, environments)


def _call(connection_id, operation, request, path, secrets_path, environments=None):
    """One operation, its failure turned into something a page can show."""
    from integrations import store, PROVIDERS
    from integrations.jira import operations
    from integrations.jira.client import JiraError

    try:
        # A sign-in check asks no query, so a predefined query that does not
        # resolve yet must not stop it.
        connection = store.credentials(connection_id, path, secrets_path, environments,
                                       scoped=operation != "test")
    except store.Unavailable as exc:
        return {"ok": False, "kind": "no_token" if exc.kind == "app" else exc.kind,
                "error": str(exc)}
    if connection["provider"] not in PROVIDERS:
        return {"ok": False, "kind": "error",
                "error": "Unknown provider %r." % connection["provider"]}
    token = connection["token"]
    from integrations import activity
    from integrations.jira import writes
    changes = operation in writes.CHANGES
    target = str((request or {}).get("key") or (request or {}).get("project") or "")
    try:
        get = operations.session(connection, token)
        result = operations.call(operation, connection, token, request, get=get)
        if get.switched and connection["auth"] == "basic":
            # A token with scopes: the gateway let it in where the site would
            # not. Kept, so every request after - cycles' too - goes there.
            store.upsert(dict(store.get(connection_id, path), gateway=True,
                              cloud_id=get.cloud_id), path, secrets_path)
    except writes.Refused as exc:
        # Not made, for a reason the page acts on - fields to ask for, an
        # issue changed meanwhile. Nothing was changed, so nothing is logged.
        return dict(exc.details, ok=False, kind=exc.kind, error=str(exc))
    except JiraError as exc:
        error = str(exc).replace(token, "[redacted]")
        if changes:
            activity.record(path, connection_id, operation, target, False, error)
        return {"ok": False, "kind": _kind(exc.status), "status": exc.status,
                "error": error}
    except ValueError as exc:
        return {"ok": False, "kind": "invalid", "error": str(exc)}
    if changes:
        made = (result.get("issue") or {}).get("key") or target
        activity.record(path, connection_id, operation, made, True,
                        result.get("summary") or operation)
    return {"ok": True, "connection": connection_id, "operation": operation,
            "result": result}


def _kind(status):
    """What kind of failure a page should show for an HTTP status."""
    if not status:
        return "unreachable"
    return {401: "signin", 403: "forbidden", 404: "not_found",
            410: "not_found", 400: "invalid", 429: "busy"}.get(status, "error")


def _export(rest, target, path, secrets_path):
    """Connections with their tokens into a file only its owner can read - for
    Export. What is printed is the ids, never a token."""
    from integrations import store
    if not target:
        raise _Usage("export needs --to=FILE to write the connections into.")
    found = store.export(rest, path, secrets_path)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(found, handle, ensure_ascii=False)
    os.chmod(target, 0o600)
    return {"ok": True, "ids": [one["id"] for one in found["connections"]]}


def _import(rest, source, path, secrets_path, environments=None):
    from integrations import store
    if not source:
        raise _Usage("import needs --from=FILE with the connections.")
    document = _document(source)
    mode = "copy" if document.get("mode") == "copy" else "merge"
    return {"ok": True, "said": store.merge(document, mode, path, secrets_path)}


def _activity(rest, source, path, secrets_path, environments=None):
    from integrations import activity
    return {"ok": True, "activity": activity.read(path)}


def _app(rest, source, path, secrets_path, environments=None):
    from integrations import oauth
    return {"ok": True, "app": oauth.app(path, secrets_path)}


def _app_save(rest, source, path, secrets_path, environments=None):
    from integrations import oauth
    if not source:
        raise _Usage("app-save needs --from=FILE with the app.")
    document = _document(source)
    problems = oauth.save_app(document.get("client_id"), document.get("secret"),
                              document.get("port"), path, secrets_path,
                              service=document.get("service"))
    if problems:
        return {"ok": False, "kind": "invalid", "problems": problems,
                "error": " ".join(problems)}
    return {"ok": True, "app": oauth.app(path, secrets_path)}


def _sign_in(rest, source, path, secrets_path, environments=None):
    """Sign in through the browser; stopped by SIGTERM, which is how the page's
    Cancel ends this process."""
    import signal
    import threading
    from integrations import oauth, store

    document = _document(source)
    cancel = threading.Event()
    previous = signal.signal(signal.SIGTERM, lambda *_: cancel.set())
    try:
        found = oauth.sign_in(path, secrets_path, cancel=cancel,
                              say=lambda url: print("Opening %s" % url, file=sys.stderr))
    except oauth.OAuthError as exc:
        return {"ok": False, "kind": exc.kind, "error": str(exc)}
    finally:
        signal.signal(signal.SIGTERM, previous)
    again = str(document.get("connection") or "")
    entry = store.get(again, path) if again else None
    if entry is not None:
        # Signing in again: the same site gets the new tokens at once.
        if entry.get("cloud_id") in {one["id"] for one in found["sites"]}:
            saved, problems = store.upsert(entry, path, secrets_path,
                                           oauth_pending=found["pending"])
            if problems:
                return {"ok": False, "kind": "invalid", "problems": problems,
                        "error": " ".join(problems)}
            return {"ok": True, "account": found["account"], "sites": found["sites"],
                    "connection": dict(saved, token_set=True)}
    return {"ok": True, "account": found["account"], "sites": found["sites"],
            "pending": found["pending"]}


def _scope(rest, source, path, secrets_path, environments=None):
    """A connection's predefined query as it would be ANDed: for a page to
    show while it is typed. Reads the environments file only."""
    from integrations import scope, store
    settings = _document(source)
    where = environments or scope.default_environments_path(store.resolve_path(path))
    try:
        return {"ok": True, "scope": scope.resolve(settings, where), "problems": []}
    except scope.ScopeError as exc:
        return {"ok": True, "scope": "", "problems": [str(exc)]}


COMMANDS = {"scope": _scope, "list": _list, "save": _save, "delete": _delete, "test": _test,
            "call": _call_command, "app": _app, "app-save": _app_save,
            "sign-in": _sign_in, "activity": _activity, "export": _export,
            "import": _import}
