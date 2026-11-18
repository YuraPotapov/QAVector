"""A Jira connection as the project keeps it: its fields, what is wrong with
one, and what a client needs to reach the site. :mod:`integrations.store`
asks this module for every connection whose provider is ``jira``."""

FLAVOURS = ("cloud", "server")
#: basic is an email and an API token, bearer a personal access token, oauth a
#: sign-in through the browser with the project's own Atlassian app (Cloud only).
AUTHS = ("basic", "bearer", "oauth")

#: What a connection's own settings are, with their defaults.
#: ``refresh_minutes`` 0 means the page never refreshes on its own.
#: (A ``default_project`` of earlier builds is dropped: it narrowed the list
#: unseen beside the predefined query, which says the same thing in the open.)
#: ``predefined_query`` is ANDed with every query the connection makes, its
#: ``{{variables}}`` read from the environment ``variables_from`` names (see
#: :mod:`integrations.scope`).
SETTINGS = {"refresh_minutes": 0, "predefined_query": "", "variables_from": ""}

#: The fields that say where and as whom; changing one makes a past check moot.
ACCOUNT = ("site", "flavour", "auth", "email", "verify_tls", "cloud_id", "gateway")


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
    settings.pop("default_project", None)
    for key in ("predefined_query", "variables_from"):
        settings[key] = str(settings.get(key) or "").strip()
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
    for runtime in ("renew", "log", "api", "scope"):  # what credentials() adds
        entry.pop(runtime, None)
    return entry


def problems(entry):
    """Everything wrong with one connection, as messages."""
    from integrations import store
    found = []
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
    if entry["id"] and not store.ID_PATTERN.match(entry["id"]):
        found.append("Id %r may hold only letters, digits, - and _." % entry["id"])
    return found


def keeps_token(entry):
    """Whether a token given with the connection is kept as its secret: a
    sign-in through Atlassian keeps its own."""
    return entry.get("auth") != "oauth"


def account_changed(previous, entry, token_changed):
    """``entry`` after a change: where or who changed, a past check says nothing."""
    if token_changed or any(previous.get(key) != entry.get(key) for key in ACCOUNT
                            if key not in ("gateway", "cloud_id")):
        entry.pop("last_check", None)
        if entry["auth"] != "oauth":
            entry.pop("gateway", None)
            entry.pop("cloud_id", None)
    elif "last_check" not in entry and "last_check" in previous:
        entry["last_check"] = previous["last_check"]
    return entry


def credentials(entry, path=None, secrets_path=None, environments_path=None, scoped=True):
    """A Jira connection ready to use: its token good now (refreshed first when it
    was signed in through Atlassian), ``api``, ``renew``, ``log`` and ``scope``
    (see :func:`integrations.store.credentials`). Raises ``store.Unavailable``."""
    from integrations import store
    from integrations import scope as scope_mod
    Unavailable, resolve_path, token = store.Unavailable, store.resolve_path, store.token
    connection_id = entry["id"]
    environments = environments_path or scope_mod.default_environments_path(
        resolve_path(path))
    try:
        scope = scope_mod.resolve(entry.get("settings"), environments) if scoped else ""
    except scope_mod.ScopeError as exc:
        # Refused rather than asked without it: a scope that is not there must
        # not read as "every project".
        raise Unavailable("Connection %r: %s" % (entry["name"], exc), "scope")
    found = dict(entry, log=resolve_path(path), scope=scope)
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
