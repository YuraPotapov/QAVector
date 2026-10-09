"""Sending a message to Microsoft Teams - a group chat, a channel, or a person.

**Through a Workflows webhook.** Teams' own *Workflows* app makes the address:
"Post to a chat when a webhook request is received" (or "... to a channel"),
pointed at the chat it should post in, gives a URL. This step sends a card to
that URL, and the workflow posts it - in a group, a channel, or a one-to-one
chat with the Flow bot, whichever the workflow was made for. No app
registration, no admin consent and no sign-in: the URL is the whole of the
permission, which is also why it is treated as a password.

**The URL is a secret.** It carries its own signature, and anybody holding it
can post as the workflow. So it belongs in a secret variable -
``${vars.teams_webhook}`` - and it is never repeated: not in the step's
message, not in its outputs, not in an error. A failure names the host and the
HTTP status, which is enough to tell a dead workflow from a typo.

**Text, laid out for a card.** The text is Markdown the way a release note is:
lines, ``- `` bullets and ``**bold**``. Teams draws Markdown lists inside one
card block unreliably, so each line becomes a block of its own and a bullet is
drawn as one; bold and italic are left to Teams, which draws those well.

**Or from a file.** ``text_file`` reads the message from a file - in the run
workspace, or given whole - for one longer than the forty lines a step keeps of
what it printed: a list of every test that failed, say.

**Mentioning people.** ``mentions`` names who Teams should notify -
``name <email>`` or just the email, their Teams sign-in. Writing "@name" in
the text notifies nobody: a card mentions somebody only when its text holds
``<at>name</at>`` *and* the card lists that tag with the person's address. So
the step does both. Where the text already says ``<at>name</at>`` - beside the
test that person's change broke, say - that is where the mention goes; anybody
the text does not tag is tagged on a line of their own at the end.

**Sending twice sends twice.** A message is not something that can be taken
back or recognised later, so there is nothing to reconcile: running the step
again posts again. Resuming a run does not re-run a step that finished.
"""

import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

from cycle import registry
from cycle.registry import CyclePlugin, PluginMetadata, field, output

#: Where a plain http:// address is allowed: a stand-in on this machine, for
#: trying a cycle out. Anything else must be https - the URL is a password.
LOOPBACK = ("127.0.0.1", "localhost", "::1")
TIMEOUT = 30
CARD = "application/vnd.microsoft.card.adaptive"
#: The most a message from a file carries. A Teams card stops at about 28 KB;
#: this leaves room for the card around the text.
MAX_TEXT = 20000
#: One person to mention: ``name <email>``, or the email alone.
PERSON = re.compile(r"^\s*(?:(?P<name>[^<>]+?)\s*<\s*(?P<email>[^<>\s@]+@[^<>\s@]+)\s*>"
                    r"|(?P<bare>[^<>\s@,]+@[^<>\s@,]+))\s*$")


class TeamsMessage(CyclePlugin):
    metadata = PluginMetadata(
        id="teams.message", name="Teams Message", category=registry.ACTION,
        summary="Post a message to a Teams chat, channel or person through a "
                "Workflows webhook.",
        permissions=("network",),
        inputs=(
            field("webhook", "Workflow URL", required=True,
                  hint="The URL a Teams workflow \"Post to a chat when a webhook "
                       "request is received\" gives. A secret: reference a "
                       "secret variable - ${vars.teams_webhook} - so the value "
                       "stays out of this file."),
            field("text", "Message", "multiline",
                  hint="Markdown as a note is written: lines, \"- \" bullets, "
                       "**bold**. Usually an earlier step's output, such as "
                       "${steps.release_note.outputs.summary}. Or use text_file."),
            field("text_file", "Message from a file", "file",
                  hint="A file holding the message, in the run workspace or "
                       "given whole - for a message longer than the lines a "
                       "step keeps of what it printed. Used when text is blank."),
            field("title", "Title",
                  hint="A heading above the message. Blank for none."),
            field("mentions", "People to mention", "args",
                  hint="Who Teams should notify, one per line or comma: "
                       "name <email>, or the email alone - their Teams sign-in. "
                       "Tagged where the text says <at>name</at>, otherwise on "
                       "a line at the end. Blank for nobody."),
            field("verify_tls", "Verify TLS", "check", default=True,
                  hint="Off only for a stand-in with a certificate of its own."),
        ),
        outputs=(
            output("sent", "boolean", "True when Teams accepted the message."),
            output("status", "number", "The HTTP status Teams answered with."),
            output("mentioned", "number", "How many people the message mentions."),
        ),
    )

    def problems(self, settings):
        settings = settings or {}
        found = super().problems(settings)
        for key in ("webhook", "text", "text_file", "title"):
            if key in settings and not isinstance(settings[key], str):
                found.append("%s must be text." % key)
        if not str(settings.get("text") or "").strip() and \
                not str(settings.get("text_file") or "").strip():
            found.append("A message needs text, or text_file to read it from.")
        webhook = str(settings.get("webhook") or "").strip()
        if webhook and "${" not in webhook:
            problem = _address_problem(webhook)
            if problem:
                found.append(problem)
        mentions = settings.get("mentions")
        if mentions is not None and not isinstance(mentions, (list, str)):
            found.append("mentions must be a list of people.")
        elif "${" not in str(mentions or ""):
            found.extend(people(mentions)[1])
        return found

    def execute(self, context, step):
        # A list another step made is checked here rather than refused: one
        # address it got wrong must not cost everybody the message.
        problems = self.problems(dict(step.settings, mentions=None))
        if problems:
            return registry.failed("; ".join(problems))
        text = str(self.setting(step.settings, "text", "") or "").strip()
        source = str(self.setting(step.settings, "text_file", "") or "").strip()
        if not text and source:
            path = os.path.join(context.workspace or "", os.path.expanduser(source))
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    text = handle.read(MAX_TEXT + 1).strip()
            except OSError as exc:
                return registry.failed("Cannot read the message from %s: %s"
                                       % (source, exc.strerror or exc))
            if len(text) > MAX_TEXT:
                text = text[:MAX_TEXT].rstrip() + "\n\n(cut at %d characters)" % MAX_TEXT
        if not text:
            return registry.failed("There is no message to send: the text is empty.")
        webhook = str(self.setting(step.settings, "webhook")).strip()
        problem = _address_problem(webhook)
        if problem:
            return registry.failed(problem)
        mentions, skipped = people(self.setting(step.settings, "mentions"))
        context.cancel.raise_if_set()

        body = json.dumps(card(text, str(self.setting(step.settings, "title", "") or ""),
                               mentions), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            webhook, data=body, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"})
        host = urllib.parse.urlsplit(webhook).hostname or "the workflow"
        verify = bool(self.setting(step.settings, "verify_tls", True))
        tls = None if verify else ssl._create_unverified_context()
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT, context=tls) as answer:
                status = answer.status
        except urllib.error.HTTPError as exc:
            return registry.failed(
                "Teams did not take the message: %s answered HTTP %d. A 404 is "
                "usually a workflow that was deleted or turned off." % (host, exc.code),
                sent=False, status=exc.code, mentioned=0)
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            return registry.failed("Could not reach %s: %s" % (host, reason),
                                   sent=False, status=0, mentioned=0)
        if not 200 <= status < 300:
            return registry.failed("Teams did not take the message: %s answered "
                                   "HTTP %d." % (host, status), sent=False, status=status,
                                   mentioned=0)
        said = " mentioning %d" % len(mentions) if mentions else ""
        if skipped:
            said += "; not mentioned: " + " ".join(skipped)
        return registry.succeeded("teams: sent to %s (HTTP %d)%s" % (host, status, said),
                                  sent=True, status=status, mentioned=len(mentions))


def _address_problem(webhook):
    """Why ``webhook`` cannot be posted to, or "". Never quotes the address."""
    parts = urllib.parse.urlsplit(webhook)
    if parts.scheme == "https" and parts.hostname:
        return ""
    if parts.scheme == "http" and parts.hostname in LOOPBACK:
        return ""
    return ("The workflow URL must start with https:// (http:// only for a "
            "stand-in on this machine).")


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
