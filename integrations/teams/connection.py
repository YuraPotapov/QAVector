"""A Teams connection as the project keeps it (QCR ADR-015 D1, D2, D5).

One channel or chat. Its webhook URL is the connection's secret, kept where a
Jira token is (``store.set_token``) and never in this record.

Settings:

* ``title`` - the title a post gets when none is given; ``footer`` - a line
  under every post ("- via QAVector");
* ``aliases`` - ``{name: sign-in address}``: Teams mentions by the UPN, which
  is not always the address people know;
* ``max_text`` - the longest text, at most the card's ``MAX_TEXT``;
* ``access`` - what agents and cycles may do by default. The connection sets
  the ceiling; an agent connection's own switch only narrows it.
"""

import re

from .webhook import MAX_TEXT, PERSON

TARGETS = ("channel", "chat", "person")
#: What agents may do: nothing, post once the person approves, or post.
AGENTS = ("off", "approve", "post")
#: What cycle steps may do: nothing, or post.
CYCLES = ("off", "post")
ACCESS = {"agents": "approve", "cycles": "post"}


def normalize(entry):
    entry = dict(entry or {})
    settings = dict(entry.get("settings") or {})
    access = dict(ACCESS)
    access.update({key: value for key, value in (settings.get("access") or {}).items() if key in ACCESS})
    try:
        longest = int(settings.get("max_text") or MAX_TEXT)
    except (TypeError, ValueError):
        longest = MAX_TEXT
    aliases = settings.get("aliases") or {}
    entry.update({
        "id": str(entry.get("id") or "").strip(),
        "provider": "teams",
        "name": str(entry.get("name") or "").strip(),
        "target": str(entry.get("target") or "channel").strip(),
        "about": str(entry.get("about") or "").strip()[:500],
        "verify_tls": bool(entry.get("verify_tls", True)),
        "settings": {
            "title": str(settings.get("title") or "").strip()[:300],
            "footer": str(settings.get("footer") or "").strip()[:300],
            "aliases": {str(name).strip(): str(upn).strip() for name, upn in aliases.items()
                        if str(name).strip() and str(upn).strip()} if isinstance(aliases, dict) else {},
            "max_text": max(100, min(longest, MAX_TEXT)),
            "access": access,
        },
    })
    for runtime in ("token", "token_set", "webhook", "log"):
        entry.pop(runtime, None)              # never stored here, whatever was sent
    return entry


def problems(entry):
    found = []
    if not entry["name"]:
        found.append("Name is required.")
    if entry["target"] not in TARGETS:
        found.append("It posts to a %s." % " / ".join(TARGETS))
    access = entry["settings"]["access"]
    if access["agents"] not in AGENTS:
        found.append("Agents' access must be one of %s." % ", ".join(AGENTS))
    if access["cycles"] not in CYCLES:
        found.append("Cycles' access must be one of %s." % ", ".join(CYCLES))
    for name, upn in entry["settings"]["aliases"].items():
        if not PERSON.match(upn):
            found.append("The alias %r needs a sign-in address, not %r." % (name, upn))
    if entry["id"] and not re.match(r"^[A-Za-z0-9_-]+$", entry["id"]):
        found.append("Id %r may hold only letters, digits, - and _." % entry["id"])
    return found


def access_of(entry, kind):
    """What ``kind`` of caller ("agents", "cycles") may do through this channel:
    a level of :data:`AGENTS` or :data:`CYCLES`. QCR holds every caller of
    that kind to it (D5); a kind the connection does not name gets "off"."""
    return normalize(entry)["settings"]["access"].get(kind, "off")


def mentions_of(entry, names):
    """``names`` as ``name <upn>`` lines: an alias where there is one, else as given."""
    aliases = {name.lower(): (name, upn) for name, upn in entry["settings"]["aliases"].items()}
    made = []
    for one in names or ():
        one = str(one or "").strip()
        if not one:
            continue
        hit = aliases.get(one.lstrip("@").lower())
        made.append("%s <%s>" % hit if hit else one)
    return made


def secret_problem(webhook):
    """What is wrong with a webhook given, or "" - never the address itself."""
    from .webhook import address_problem
    return address_problem(webhook)


def keeps_token(entry):
    return True


def account_changed(previous, entry, token_changed):
    """A new webhook: a past test post says nothing about it."""
    if token_changed:
        entry.pop("last_check", None)
    elif "last_check" not in entry and "last_check" in previous:
        entry["last_check"] = previous["last_check"]
    return entry


def credentials(entry, path=None, secrets_path=None, environments_path=None, scoped=True):
    """A channel ready to post to: its webhook, which is its whole permission (D1)."""
    from integrations import store
    value = store.token(entry["id"], secrets_path)
    if not value:
        raise store.Unavailable("Connection %r has no webhook. Set it in Integrations -> Teams -> Connections."
                                % entry["name"], "no_token")
    return dict(entry, log=store.resolve_path(path), token=value, webhook=value)


def shown(entry, secrets_path=None):
    """Where the webhook goes, as a page may show it: the host, never the address."""
    from integrations import store
    from .webhook import host_of
    return dict(entry, webhook_host=host_of(store.token(entry["id"], secrets_path)))
