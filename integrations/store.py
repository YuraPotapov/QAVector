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

FLAVOURS = ("cloud", "server")
#: basic is an email and an API token, bearer a personal access token, oauth a
#: sign-in through the browser with the project's own Atlassian app (Cloud only).
AUTHS = ("basic", "bearer", "oauth")

#: What a connection's own settings are, with their defaults.
#: ``refresh_minutes`` 0 means the page never refreshes on its own.
SETTINGS = {"default_project": "", "refresh_minutes": 0}

#: The fields that say where and as whom; changing one makes a past check moot.
ACCOUNT = ("site", "flavour", "auth", "email", "verify_tls", "cloud_id", "gateway")

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
    """``entry`` with every field present and of the right type.

    Keys this version does not know are kept, so a file written by a newer
    build survives being saved by this one.
    """
    entry = dict(entry or {})
    settings = dict(SETTINGS)
    settings.update(entry.get("settings") or {})
    try:
        settings["refresh_minutes"] = max(0, int(settings.get("refresh_minutes") or 0))
    except (TypeError, ValueError):
        settings["refresh_minutes"] = 0
    # Written by an earlier build of this page: a setting for one kind of user
    # of the connection, which the connection has no business knowing about.
    settings.pop("cycle_writes", None)
    settings["default_project"] = str(settings.get("default_project") or "").strip()
    entry.update({
        "id": str(entry.get("id") or "").strip(),
        "provider": str(entry.get("provider") or "jira").strip(),
        "name": str(entry.get("name") or "").strip(),
        "site": str(entry.get("site") or "").strip().rstrip("/"),
        "flavour": str(entry.get("flavour") or "cloud").strip(),
        "auth": str(entry.get("auth") or "basic").strip(),
        "email": str(entry.get("email") or "").strip(),
        "verify_tls": bool(entry.get("verify_tls", True)),
        "settings": settings,
    })
    if entry["auth"] == "oauth":
        # Which Jira site the sign-in reaches, as Atlassian names it: requests
        # go to the API gateway with this id, links to the site itself.
        entry["cloud_id"] = str(entry.get("cloud_id") or "").strip()
        entry["email"] = ""
        entry["verify_tls"] = True
        entry.pop("gateway", None)
    elif entry["auth"] == "basic" and entry["flavour"] == "cloud" and entry.get("gateway"):
        # An API token with scopes: Jira Cloud answers it only through the
        # gateway, under the site's cloud id - found on first use, see cli.
        entry["gateway"] = True
        entry["cloud_id"] = str(entry.get("cloud_id") or "").strip()
    else:
        entry.pop("cloud_id", None)
        entry.pop("gateway", None)
    entry.pop("token", None)               # never stored here, whatever was sent
    entry.pop("token_set", None)           # an answer about the store, not a field
    for runtime in ("renew", "log", "api"):  # what credentials() adds for a caller
        entry.pop(runtime, None)
    return entry


def problems(entry):
    """Everything wrong with one connection, as messages."""
    from integrations import PROVIDERS

    found = []
    if entry["provider"] not in PROVIDERS:
        found.append("Unknown provider %r." % entry["provider"])
    if not entry["name"]:
        found.append("Name is required.")
    if not entry["site"]:
        found.append("Site is required.")
    elif not entry["site"].startswith(("http://", "https://")):
        found.append("Site must start with http:// or https://.")
    if entry["flavour"] not in FLAVOURS:
        found.append("Product must be one of %s." % ", ".join(FLAVOURS))
    if entry["auth"] not in AUTHS:
        found.append("Authentication must be one of %s." % ", ".join(AUTHS))
    if entry["auth"] == "oauth":
        if entry["flavour"] != "cloud":
            found.append("Signing in with Atlassian is for Jira Cloud only.")
        if not entry["cloud_id"]:
            found.append("Sign in with Atlassian and choose a site first.")
    if entry["auth"] == "basic" and not entry["email"]:
        # Basic auth against Jira is the email, not a username, and an empty
        # one fails with a 401 that says nothing about which half is missing.
        found.append("Basic authentication needs the account email.")
    if entry["id"] and not ID_PATTERN.match(entry["id"]):
        found.append("Id %r may hold only letters, digits, - and _." % entry["id"])
    return found


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
        if new_token or clear_token or oauth_pending or any(
                previous.get(key) != entry.get(key) for key in ACCOUNT
                if key not in ("gateway", "cloud_id")):
            # Where or who changed: the last successful sign-in says nothing
            # about the new account, and a new token may be the other kind.
            entry.pop("last_check", None)
            if entry["auth"] != "oauth":
                entry.pop("gateway", None)
                entry.pop("cloud_id", None)
        elif "last_check" not in entry and "last_check" in previous:
            entry["last_check"] = previous["last_check"]
        connections = [entry if one["id"] == entry["id"] else one for one in connections]
    save(connections, path)
    if oauth_pending:
        from integrations import oauth
        oauth.adopt(oauth_pending, entry["id"], secrets_path)
    elif clear_token:
        set_token(entry["id"], "", secrets_path)
    elif new_token and entry["auth"] != "oauth":
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
    return [dict(one, token_set=one["id"] in held) for one in load(path)]


# -- for a run ----------------------------------------------------------------
class Unavailable(Exception):
    """A connection that cannot be used now. ``kind``: ``not_found``,
    ``no_token``, or what a sign-in failure says (``signin``, ``app``...)."""

    def __init__(self, message, kind="not_found"):
        super().__init__(message)
        self.kind = kind


def credentials(connection_id, path=None, secrets_path=None):
    """A connection with what a client needs to reach its service now.

    The connection's own fields, plus ``token`` (good now - refreshed first if
    it was signed in through Atlassian and about to run out), ``api`` (where
    requests go, when that is not the site itself), ``renew`` (a call that gives
    a token good at that later moment, for whoever holds on to this past an
    hour) and ``log`` (the connections file, for :mod:`integrations.activity`).
    Raises :class:`Unavailable`.
    """
    entry = get(connection_id, path)
    if entry is None:
        raise Unavailable("No connection %r in this project. Add it in Integrations "
                          "-> Jira -> Connections." % connection_id)
    found = dict(entry, log=resolve_path(path))
    if entry["auth"] == "oauth":
        from integrations import oauth

        def renew():
            try:
                return oauth.fresh_token(connection_id, path, secrets_path)
            except oauth.OAuthError as exc:
                raise Unavailable("Connection %r: %s" % (entry["name"], exc), exc.kind)

        return dict(found, token=renew(), api=oauth.api_base(entry["cloud_id"]), renew=renew)
    value = token(connection_id, secrets_path)
    if not value:
        raise Unavailable("Connection %r has no token. Set it in Integrations -> Jira "
                          "-> Connections." % entry["name"], "no_token")
    return dict(found, token=value, api=gateway_of(entry) if entry.get("gateway") else "",
                renew=lambda: value)


def gateway_of(entry):
    from integrations.jira import client
    return client.gateway(entry.get("cloud_id", ""))
