"""The Teams provider: what can be done through a connection (QCR ADR-015 D3).

* ``describe`` - the channel as set up, everything but its webhook;
* ``preview`` - the card a post would be, built here; nothing is sent;
* ``history`` / ``post_of`` - what QAVector posted through the connection;
* ``post`` - a write: ``prepare`` shows the card with the text editable before
  the person approves it, ``write`` sends it and writes it down.

``test`` (a real test post) is the person's, from the page, and no contract:
an agent never gets it.

A post cannot be taken back, so ``post`` is not idempotent and never retried;
202 is "accepted", not "delivered"; no answer in time is an unknown outcome.
"""

import json
import math
import socket
import time

from integrations.contracts import OperationContract, Provider, WriteError

from . import connection as connection_mod
from . import history
from .webhook import MAX_TEXT, SendError, card, host_of, people, send

#: At most one post a connection in this many seconds (user, 2026-11-17): a guard
#: against sending the same thing twice, by anybody - page, agent or cycle.
INTERVAL = 60

SHORT = {"type": "string", "maxLength": 4096}
TEXT = {"type": "string", "minLength": 1, "maxLength": MAX_TEXT}
NAMES = {"type": "array", "maxItems": 50, "items": {"type": "string", "minLength": 1, "maxLength": 300}}
POST = {"type": "object"}


def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


POST_INPUT = obj({"title": {"type": "string", "maxLength": 300}, "text": TEXT, "mentions": NAMES}, ["text"])
CONTRACTS = {
    "describe": OperationContract(
        "describe", "This channel as it is set up: what it posts to, the default title and footer, names "
        "it mentions people by, the longest text, how often it posts, and when the next post may go.",
        obj({}), {"type": "object"}),
    "preview": OperationContract(
        "preview", "Build the card a post would be - title, Markdown text, people to mention - without sending.",
        POST_INPUT, obj({"card": {"type": "object"}, "length": {"type": "integer"}, "mentions": NAMES,
                         "problems": NAMES}, ["card", "length", "mentions", "problems"])),
    "history": OperationContract(
        "history", "The posts QAVector sent through this connection, newest first.",
        obj({"size": {"type": "integer", "minimum": 1, "maximum": 200},
             "outcome": {"enum": list(history.OUTCOMES)}}),
        obj({"items": {"type": "array", "items": POST}}, ["items"])),
    "post_of": OperationContract(
        "post_of", "One post QAVector sent, by its id.",
        obj({"id": {"type": "string", "minLength": 1, "maxLength": 64}}, ["id"]),
        obj({"post": POST}, ["post"])),
    "post": OperationContract(
        "post", "Post to this Teams channel or chat. It cannot be taken back; the person approves it first.",
        POST_INPUT, obj({"post": POST, "summary": SHORT}, ["post", "summary"]),
        access="write", risk="high", idempotent=False),
}


def _made(connection, request):
    """The text with the connection's footer, the mentions resolved by its aliases."""
    settings = connection["settings"]
    title = str(request.get("title") or "").strip() or settings["title"]
    text = str(request.get("text") or "").strip()
    if settings["footer"]:
        text = text + "\n\n" + settings["footer"]
    if len(text) > settings["max_text"]:
        text = text[:settings["max_text"]].rstrip() + "\n\n(cut at %d characters)" % settings["max_text"]
    names = connection_mod.mentions_of(connection, request.get("mentions"))
    mentions, problems = people(names)
    return title, text, mentions, problems


def _shown(post):
    """A post as it is told: everything but where it went."""
    return {key: value for key, value in post.items() if key != "webhook"}


def describe(connection):
    """Everything set on the channel but its webhook, and where its posting stands."""
    settings = connection["settings"]
    last = history.last_sent(connection["log"], connection["id"])
    wait = max(0, int(math.ceil(INTERVAL - (time.time() - last)))) if last is not None else 0
    return {"id": connection["id"], "name": connection["name"], "posts_to": connection["target"],
            "about": connection["about"], "default_title": settings["title"], "footer": settings["footer"],
            "mention_names": dict(settings["aliases"]), "max_text": settings["max_text"],
            "access": dict(settings["access"]),
            "posting": {"one_post_every_seconds": INTERVAL, "last_post_at": last, "next_post_in_seconds": wait,
                        "takes_back": False, "outcomes": list(history.OUTCOMES)},
            "posts_kept": len(history.read(connection["log"], connection["id"], size=history.KEEP))}


def read(name, connection, token, request, get=None):
    if name == "describe":
        return describe(connection)
    if name == "preview":
        title, text, mentions, problems = _made(connection, request)
        return {"card": card(text, title, mentions), "length": len(text),
                "mentions": ["%s <%s>" % one for one in mentions], "problems": problems}
    if name == "history":
        posts = history.read(connection["log"], connection["id"], request.get("size") or 50,
                             request.get("outcome") or "")
        return {"items": [_shown(one) for one in posts]}
    if name == "post_of":
        found = history.get(connection["log"], request["id"])
        if found is None or found.get("connection") != connection["id"]:
            raise SendError("No such post.", 404)
        return {"post": _shown(found)}
    raise ValueError("Unknown Teams operation %r." % name)


def prepare(name, connection, token, request, get=None):
    """What the person approves: the card, the text editable, said it cannot be taken back."""
    if name != "post":
        raise WriteError("unsupported")
    title, text, mentions, problems = _made(connection, request)
    if problems:
        raise WriteError("refused", "; ".join(problems))
    notes = ["A post cannot be taken back. It goes to %s (%s)." % (connection["name"], connection["target"])]
    if mentions:
        notes.append("Mentions: " + ", ".join(name for name, _upn in mentions))
    return {"request": dict(request), "noop": None, "card": {
        # The post's own words name it in a list; the channel is said beside it.
        "operation": "post", "key": (title or text.splitlines()[0])[:120], "summary": connection["about"],
        "title": "Post to %s" % connection["name"], "changes": [], "notes": notes,
        "edit": {"title": {"label": "Title", "value": request.get("title") or "", "was": "", "multiline": False},
                 "text": {"label": "Text", "value": request.get("text") or "", "was": "", "multiline": True,
                          "required": True}}}}


def write(name, connection, token, request, get=None):
    if name != "post":
        raise WriteError("unsupported")
    caller = dict(connection.get("caller") or {})
    # The reader writes this one's Activity line, as for every write through it.
    return post(connection, request, caller, caller.pop("approved_by", ""), logged=False)


def post(connection, request, source=None, approved_by="", again_of="", logged=True):
    """Send one post and write it down - made, failed or unknown - in the
    history, and, unless ``logged`` is False, as a line in Activity without its
    words (D4). Returns ``{"post", "summary"}``; raises :class:`WriteError`
    when it was not taken."""
    title, text, mentions, problems = _made(connection, request)
    if problems:
        raise WriteError("refused", "; ".join(problems))
    with history.held(connection["log"], connection["id"]):
        last = history.last_sent(connection["log"], connection["id"])
        left = INTERVAL - (time.time() - last) if last is not None else 0
        if left > 0:
            wait = int(math.ceil(left))
            # Not sent, so not written down: there is no post to tell of.
            raise WriteError("refused", "One post a minute to %s: the last one went %d s ago. Try again in %d s."
                             % (connection["name"], int(time.time() - last), wait),
                             {"retry_after": wait})
        return _send(connection, title, text, mentions, source, approved_by, again_of, logged)


def _said(connection, outcome, detail=""):
    """A post's outcome as Activity and the caller are told it - never its words."""
    words = {"accepted": "accepted", "failed": "not taken", "unknown": "outcome unknown"}[outcome]
    return "Post to %s: %s%s" % (connection["name"], words, " (%s)" % detail if detail else "")


def _log(connection, outcome, detail, source):
    from integrations import activity
    shown = {key: value for key, value in (source or {}).items() if key in ("label", "detail")} or None
    activity.record(connection["log"], connection["id"], "post", connection["name"], outcome != "failed",
                    _said(connection, outcome, detail), shown, "teams")


def _send(connection, title, text, mentions, source, approved_by, again_of, logged=True):
    body = json.dumps(card(text, title, mentions), ensure_ascii=False).encode("utf-8")
    host = host_of(connection["webhook"])
    names = ["%s <%s>" % one for one in mentions]
    try:
        status = send(connection["webhook"], body, verify_tls=connection.get("verify_tls", True))
    except SendError as exc:
        timed_out = isinstance(exc.__context__, (socket.timeout, TimeoutError)) or "timed out" in str(exc)
        outcome = "unknown" if timed_out else "failed"
        detail = "no answer in time" if timed_out else ("HTTP %d" % exc.status if exc.status else
                                                        "could not reach it")
        history.record(connection["log"], connection["id"], title, text, names, outcome, exc.status, host,
                       detail, source, approved_by, again_of)
        if logged:
            _log(connection, outcome, detail, source)
        if timed_out:
            raise WriteError("unknown_outcome") from None
        raise
    made = history.record(connection["log"], connection["id"], title, text, names, "accepted", status, host,
                          "", source, approved_by, again_of)
    if logged:
        _log(connection, "accepted", "HTTP %d" % status, source)
    return {"post": _shown(made), "summary": _said(connection, "accepted", "HTTP %d" % status)}


def provider():
    return Provider("teams", CONTRACTS, {"describe": read, "preview": read, "history": read, "post_of": read,
                                         "post": write},
                    read, prepare=prepare, write=write)
