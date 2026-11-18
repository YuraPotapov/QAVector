"""Posting to Microsoft Teams through a Workflows webhook: the card, the people
it mentions, and the request (QCR ADR-015).

Moved here from the cycle step ``teams.message``, which now uses it, so that a
person on the Integrations page, an agent through QCR and a cycle step post the
same way.

**Through a Workflows webhook.** Teams' own *Workflows* app makes the address:
"Post to a chat when a webhook request is received" (or "... to a channel"),
pointed at the chat it should post in, gives a URL. The card goes to that URL
and the workflow posts it. No app registration and no sign-in: the URL is the
whole of the permission, which is why it is treated as a password - never
repeated in an answer, an error or a record. A failure names the host and the
HTTP status, which is enough to tell a dead workflow from a typo.

**Text, laid out for a card.** Markdown the way a release note is: lines,
``- `` bullets and ``**bold**``. Teams draws Markdown lists inside one card block
unreliably, so each line becomes a block of its own and a bullet is drawn as
one; bold and italic are left to Teams.

**Mentioning people.** A card mentions somebody only when its text holds
``<at>name</at>`` *and* the card lists that tag with the person's sign-in
address (UPN). Where the text already tags someone, that is where the mention
goes; anybody it does not is tagged on a line of their own at the end.

**Sending twice sends twice, and 202 is not "delivered".** The workflow takes
the request and posts later; nothing can be taken back or recognised after.
"""

import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

#: Where a plain http:// address is allowed: a stand-in on this machine, for
#: trying it out. Anything else must be https - the URL is a password.
LOOPBACK = ("127.0.0.1", "localhost", "::1")
TIMEOUT = 30
CARD = "application/vnd.microsoft.card.adaptive"
#: The most a post carries. A Teams card stops at about 28 KB; this leaves room
#: for the card around the text.
MAX_TEXT = 20000
#: One person to mention: ``name <email>``, or the email alone.
PERSON = re.compile(r"^\s*(?:(?P<name>[^<>]+?)\s*<\s*(?P<email>[^<>\s@]+@[^<>\s@]+)\s*>"
                    r"|(?P<bare>[^<>\s@,]+@[^<>\s@,]+))\s*$")


class SendError(Exception):
    """A post not taken: ``status`` is the HTTP status, 0 when nothing answered."""

    def __init__(self, message, status=0, host=""):
        super().__init__(message)
        self.status, self.host = status, host


def address_problem(webhook):
    """Why ``webhook`` cannot be posted to, or "". Never quotes the address."""
    parts = urllib.parse.urlsplit(str(webhook or ""))
    if parts.scheme == "https" and parts.hostname:
        return ""
    if parts.scheme == "http" and parts.hostname in LOOPBACK:
        return ""
    return ("The workflow URL must start with https:// (http:// only for a "
            "stand-in on this machine).")


def host_of(webhook):
    """Where a webhook goes, as can be shown: the host, never the address."""
    return urllib.parse.urlsplit(str(webhook or "")).hostname or "the workflow"


def people(value):
    """``([(name, email), ...], problems)`` from a list, or from lines or commas.

    One person once, however often named: the first spelling of their name
    wins. Somebody given by email alone is tagged by that email.
    """
    entries = value if isinstance(value, list) else re.split(r"[\n,]", str(value or ""))
    found, problems, seen = [], [], set()
    for entry in entries:
        entry = str(entry or "").strip()
        if not entry:
            continue
        hit = PERSON.match(entry)
        if not hit:
            problems.append("mentions: %r is not an email or \"name <email>\"." % entry)
            continue
        email = hit.group("email") or hit.group("bare")
        if email.lower() in seen:
            continue
        seen.add(email.lower())
        found.append(((hit.group("name") or email).strip(), email))
    return found, problems


def card(text, title="", mentions=()):
    """The Adaptive Card a Teams workflow posts: one block per line of ``text``.

    ``mentions`` - ``(name, email)`` pairs - are listed for Teams to notify;
    whoever the text does not already tag as ``<at>name</at>`` is tagged on a
    last line of its own.
    """
    tags = [("<at>%s</at>" % name, name, email) for name, email in mentions]
    rest = [tag for tag, _name, _email in tags if tag not in text]
    if rest:
        text = text.rstrip() + "\n\n" + ", ".join(rest)
    body = []
    if title.strip():
        body.append({"type": "TextBlock", "text": title.strip(), "weight": "Bolder",
                     "size": "Medium", "wrap": True})
    gap = False
    for line in text.splitlines():
        if not line.strip():
            gap = True
            continue
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            block = {"type": "TextBlock", "text": "• " + stripped[2:].strip(),
                     "wrap": True, "spacing": "Small"}
        else:
            block = {"type": "TextBlock", "text": stripped, "wrap": True}
        if gap and body:
            block["spacing"] = "Medium"
        gap = False
        body.append(block)
    content = {"type": "AdaptiveCard", "version": "1.4", "body": body}
    if tags:
        content["msteams"] = {"entities": [
            {"type": "mention", "text": tag, "mentioned": {"id": email, "name": name}}
            for tag, name, email in tags]}
    return {"type": "message", "attachments": [{
        "contentType": CARD, "contentUrl": None, "content": content}]}


def send(webhook, payload, verify_tls=True, timeout=TIMEOUT):
    """Post ``payload`` (bytes) to ``webhook``. Returns the HTTP status (2xx);
    raises :class:`SendError` naming the host and status, never the address."""
    problem = address_problem(webhook)
    if problem:
        raise SendError(problem)
    host = host_of(webhook)
    request = urllib.request.Request(webhook, data=payload, method="POST",
                                     headers={"Content-Type": "application/json; charset=utf-8"})
    tls = None if verify_tls else ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=tls) as answer:
            status = answer.status
    except urllib.error.HTTPError as exc:
        raise SendError("Teams did not take the message: %s answered HTTP %d. A 404 is usually a "
                        "workflow that was deleted or turned off." % (host, exc.code), exc.code, host) from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise SendError("Could not reach %s: %s" % (host, reason), 0, host) from None
    if not 200 <= status < 300:
        raise SendError("Teams did not take the message: %s answered HTTP %d." % (host, status), status, host)
    return status
