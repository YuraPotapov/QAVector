"""A project's connections, and where each one's token is kept.

Two files, for the reason ``cycle/secrets.py`` gives for its own split::

    <project>/integrations.json     who and where: name, site, product, account
    <project>/cyclesecrets.json     the token, encrypted, under "@integrations"

The first is plain JSON a person can read; it holds nothing that would hurt if
it were shown on a screen share. The token goes into the project's existing
secrets store, under an entry none of that store's other users can be called
- their ids are ``[A-Za-z0-9_-]+`` and this starts with ``@`` - so it shares the
key, the permissions and the path that store already has.

A connection's ``id`` is made once, from its first name, and never changes:
whatever uses a connection names it by its id, and renaming "Product team" must
not break any of them.
"""

import json
import os
import re

import runtime_paths
from cycle import secrets as secret_store

FILE_NAME = "integrations.json"
VERSION = 1

#: The entry of the secrets store that holds connection tokens, by connection id.
CREDENTIALS = "@integrations"

#: What a connection id may be: the same alphabet a cycle id uses, so it is
#: safe in a YAML scalar, a settings key and a file name alike.
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

def provider_of(entry):
    """A connection's provider. One saved before there were others is the first one."""
    from integrations import PROVIDERS
    return str((entry or {}).get("provider") or PROVIDERS[0]).strip()


def _provider(name):
    """A provider's handling of its connections - ``integrations.<name>.connection`` -
    or None for a provider this build does not know."""
    import importlib
    from integrations import PROVIDERS
    return importlib.import_module("integrations.%s.connection" % name) if name in PROVIDERS else None


#: Owner read/write only, as for the secrets store: the file names an account.
PRIVATE = 0o600


class StoreError(Exception):
    """The connections file could not be read or written, or an entry is wrong."""


def default_path():
    """Where the file goes when nobody has said otherwise: beside the user's data."""
    return os.path.join(runtime_paths.user_data_root(), FILE_NAME)


def resolve_path(configured=""):
    """The path in force: what the caller was given, or the default."""
    configured = (configured or "").strip()
    return os.path.expanduser(configured) if configured else default_path()


# -- one entry ----------------------------------------------------------------
def normalize(entry):
    """``entry`` with every field its provider has, of the right type. Keys this
    version does not know are kept, so a newer build's file survives this one."""
    entry = dict(entry or {})
    entry["provider"] = provider_of(entry)
    module = _provider(entry["provider"])
    if module is None:
        return dict(entry, id=str(entry.get("id") or "").strip(), name=str(entry.get("name") or "").strip())
    return module.normalize(entry)


def problems(entry):
    """Everything wrong with one connection, as messages."""
    module = _provider(entry["provider"])
    if module is None:
        return ["Unknown provider %r." % entry["provider"]]
    return module.problems(entry)


def slug(name, taken=()):
    """A new id from a name: lower case, dashes, unique among ``taken``."""
    base = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-") or "connection"
    base = base[:48].strip("-") or "connection"
    candidate, suffix = base, 2
    while candidate in taken:
        candidate = "%s-%d" % (base, suffix)
        suffix += 1
    return candidate


# -- the file -----------------------------------------------------------------
def _document(path):
    path = resolve_path(path)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8-sig") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as exc:
        raise StoreError("cannot read %s: %s" % (path, exc))
    if not isinstance(document, dict) or not isinstance(document.get("connections", []), list):
        raise StoreError("%s has to contain an object with a connections list" % path)
    return document


def load(path=None):
    """Every connection, normalized, in the order they were added.

    A missing file is no connections, the ordinary state of a new project.
    """
    return [normalize(one) for one in _document(path).get("connections") or []
            if isinstance(one, dict) and one.get("id")]


def apps(path=None):
    """The project's own OAuth apps, by provider: ``{"atlassian": {client_id, port}}``.
    Their secrets are in the secrets store, never here."""
    found = _document(path).get("apps")
    return dict(found) if isinstance(found, dict) else {}


def save_app(name, settings, path=None):
    known = apps(path)
    known[name] = {key: value for key, value in dict(settings).items() if key != "secret"}
    save(load(path), path, known)


def save(connections, path=None, apps_=None):
    """Write every connection back, atomically and owner-only. Returns the path.

    The apps are kept as they are unless ``apps_`` replaces them.
    """
    kept = apps(path) if apps_ is None else apps_
    path = resolve_path(path)
    payload = {"version": VERSION, "connections": [normalize(one) for one in connections]}
    if kept:
        payload["apps"] = kept
    return write_private(path, payload)


def write_private(path, payload):
    """Write ``payload`` to ``path`` as JSON, atomically and owner-only.

    Returns the path. Raises :class:`StoreError`. Shared by every file of
    connections there is, so none of them is ever half written or readable by
    another account.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    temp = os.path.join(directory, ".%s.tmp" % os.path.basename(path))
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, PRIVATE)
    except OSError as exc:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise StoreError("cannot write %s: %s" % (path, exc))
    return path


def get(connection_id, path=None):
    """One connection, or None."""
    for one in load(path):
        if one["id"] == connection_id:
            return one
    return None


# -- the token ----------------------------------------------------------------
def token(connection_id, secrets_path=None):
    """The connection's token, or "" when none is set or the store is unreadable."""
    return secret_store.get(CREDENTIALS, connection_id, secrets_path)


def has_token(connection_id, secrets_path=None):
    return connection_id in secret_store.names(CREDENTIALS, secrets_path)


def set_token(connection_id, value, secrets_path=None):
    """Store a token. An empty value removes it."""
    secret_store.set_secret(CREDENTIALS, connection_id, value, secrets_path)


# -- changing it --------------------------------------------------------------
def upsert(entry, path=None, secrets_path=None, new_token=None, clear_token=False,
           oauth_pending=""):
    """Add a connection, or change one. Returns ``(entry, problems)``.

    An entry without an ``id`` is new and gets one from its name. One with an
    id replaces the connection of that id and keeps it - renaming changes the
    name, never what cycles call it. ``new_token`` None or "" leaves the token
    as it is (a form that never shows the token cannot send it back);
    ``clear_token`` removes it. ``oauth_pending`` gives the connection the
    tokens a browser sign-in left waiting (see ``integrations.oauth``).
    """
    entry = normalize(entry)
    connections = load(path)
    taken = {one["id"] for one in connections}
    if entry["id"] and entry["id"] not in taken:
        return entry, ["No connection %r to change." % entry["id"]]
    found = problems(entry)
    module = _provider(entry["provider"])
    said = getattr(module, "secret_problem", None)
    if new_token and said is not None and said(new_token):
        found.append(said(new_token))         # what is wrong with it, never the secret
    others = [one for one in connections if one["id"] != entry["id"]]
    if any(one["name"].lower() == entry["name"].lower() for one in others):
        found.append("A connection called %r already exists." % entry["name"])
    if found:
        return entry, found
    if not entry["id"]:
        entry["id"] = slug(entry["name"], taken)
        entry.pop("last_check", None)
        connections.append(entry)
    else:
        previous = next(one for one in connections if one["id"] == entry["id"])
        # Where or who changed: the last successful check says nothing about the new one.
        entry = module.account_changed(previous, entry, bool(new_token or clear_token or oauth_pending))
        connections = [entry if one["id"] == entry["id"] else one for one in connections]
    save(connections, path)
    if oauth_pending:
        from integrations import oauth
        oauth.adopt(oauth_pending, entry["id"], secrets_path)
    elif clear_token:
        set_token(entry["id"], "", secrets_path)
    elif new_token and getattr(module, "keeps_token", lambda one: True)(entry):
        set_token(entry["id"], new_token, secrets_path)
    return entry, []


def export(ids=(), path=None, secrets_path=None):
    """Connections with their tokens, for a file somebody moves to another
    machine or project - and the project's own OAuth app, secret included,
    when it has one. ``ids`` empty is every connection."""
    chosen = [one for one in load(path) if not ids or one["id"] in set(ids)]
    found = {"connections": [], "integration_apps": []}
    for one in chosen:
        entry = dict(one)
        entry.pop("last_check", None)
        entry["token"] = token(one["id"], secrets_path)
        found["connections"].append(entry)
    if chosen:
        for name, app in apps(path).items():
            secret = secret_store.get(CREDENTIALS, "app:" + name, secrets_path)
            found["integration_apps"].append(dict(app, provider=name, secret=secret))
    return found


def merge(document, mode, path=None, secrets_path=None):
    """Put exported connections in. Returns what was done, said.

    A connection keeps its id, so whatever named it there names it here. One
    whose id is taken either **adds to the existing one** (``merge``: only a
    token it lacks) or is **added as a copy** (``copy``: a new id and a name
    with "(copy)"). An app is set only where the project has none.
    """
    said = []
    connections = load(path)
    taken = {one["id"]: one for one in connections}
    names = {one["name"].casefold() for one in connections}
    for raw in document.get("connections") or []:
        if not isinstance(raw, dict):
            continue
        value = str(raw.get("token") or "")
        entry = normalize(raw)
        existing = taken.get(entry["id"])
        if existing is not None and mode == "merge":
            if value and not has_token(entry["id"], secrets_path):
                set_token(entry["id"], value, secrets_path)
                said.append("Gave connection %s the token it lacked." % existing["name"])
            else:
                said.append("Kept connection %s as it was." % existing["name"])
            continue
        if existing is not None or not ID_PATTERN.match(entry["id"] or "-"):
            base = entry["name"] + " (copy)"
            name, number = base, 2
            while name.casefold() in names:
                name, number = "%s %d" % (base, number), number + 1
            entry["name"] = name
            entry["id"] = slug(name, taken)
        elif entry["name"].casefold() in names:
            entry["name"] = "%s (%s)" % (entry["name"], entry["id"])
        problems_found = problems(entry)
        if problems_found:
            said.append("Connection %s was not imported: %s"
                        % (entry["name"], " ".join(problems_found)))
            continue
        connections.append(entry)
        taken[entry["id"]] = entry
        names.add(entry["name"].casefold())
        save(connections, path)
        if value:
            set_token(entry["id"], value, secrets_path)
        said.append("Added connection %s." % entry["name"])
    known = apps(path)
    for app in document.get("integration_apps") or []:
        name = str((app or {}).get("provider") or "")
        if not name or known.get(name, {}).get("client_id"):
            continue
        secret = str(app.get("secret") or "")
        save_app(name, {key: value for key, value in app.items()
                        if key in ("client_id", "port", "service")}, path)
        if secret:
            secret_store.set_secret(CREDENTIALS, "app:" + name, secret, secrets_path)
        said.append("Set this project's own %s OAuth app." % name.title())
    return said


def remove(connection_id, path=None, secrets_path=None):
    """Forget a connection and its token. Returns whether there was one."""
    connections = load(path)
    kept = [one for one in connections if one["id"] != connection_id]
    if len(kept) == len(connections):
        return False
    save(kept, path)
    set_token(connection_id, "", secrets_path)
    return True


def describe(path=None, secrets_path=None):
    """Every connection as a front-end shows it: ``token_set`` instead of a token."""
    try:
        held = set(secret_store.names(CREDENTIALS, secrets_path))
    except Exception:                       # noqa: BLE001 - shown as "not set"
        held = set()
    shown = []
    for one in load(path):
        one = dict(one, token_set=one["id"] in held)
        # What a provider adds for a page to show - never a secret itself.
        extra = getattr(_provider(one["provider"]), "shown", None)
        shown.append(extra(one, secrets_path) if extra is not None and one["token_set"] else one)
    return shown


# -- for a run ----------------------------------------------------------------
class Unavailable(Exception):
    """A connection that cannot be used now. ``kind``: ``not_found``,
    ``no_token``, or what a sign-in failure says (``signin``, ``app``...)."""

    def __init__(self, message, kind="not_found"):
        super().__init__(message)
        self.kind = kind


def credentials(connection_id, path=None, secrets_path=None, environments_path=None,
                scoped=True):
    """A connection with what a client needs to reach its service now - its
    provider says what that is. Raises :class:`Unavailable`; ``scoped`` False is
    for a caller that makes no query - a sign-in check."""
    entry = get(connection_id, path)
    if entry is None:
        raise Unavailable("No connection %r in this project. Add it in Integrations." % connection_id)
    module = _provider(entry["provider"])
    if module is None:
        raise Unavailable("Connection %r has a provider this build does not know." % entry["name"])
    return module.credentials(entry, path, secrets_path, environments_path, scoped)
