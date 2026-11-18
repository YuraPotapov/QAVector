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

from cycle import registry
from cycle.registry import CyclePlugin, PluginMetadata, field, output
# The card, the mentions and the request are the Teams integration's (ADR-015):
# one way to post, whoever posts.
from integrations.teams.webhook import (CARD, LOOPBACK, MAX_TEXT, PERSON, TIMEOUT,  # noqa: F401
                                        SendError, address_problem as _address_problem, card, people, send)


class TeamsMessage(CyclePlugin):
    #: Given its channel whole (``_connection``): it posts through the integration.
    TAKES_CONNECTION = True
    metadata = PluginMetadata(
        id="teams.message", name="Teams Message", category=registry.ACTION,
        summary="Post a message to a Teams chat, channel or person through a "
                "Workflows webhook.",
        permissions=("network",),
        inputs=(
            field("connection", "Channel", "connection", options=("teams",),
                  hint="A Teams channel of this project (Integrations -> Teams), by its id. "
                       "Its webhook, footer and names to mention people by come with it, every "
                       "post is in its history, and it posts at most once a minute."),
            field("webhook", "Workflow URL",
                  hint="Only for a cycle not moved to a channel yet: the URL a Teams workflow "
                       "gives, as a secret variable - ${vars.teams_webhook}. Name a channel "
                       "instead."),
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
        # ``_connection`` is the channel the run put in place of ``connection``
        # (cycle/connections.expand), not something a cycle file can say.
        found = super().problems({key: value for key, value in settings.items() if key != "_connection"})
        for key in ("webhook", "text", "text_file", "title"):
            if key in settings and not isinstance(settings[key], str):
                found.append("%s must be text." % key)
        if not str(settings.get("text") or "").strip() and \
                not str(settings.get("text_file") or "").strip():
            found.append("A message needs text, or text_file to read it from.")
        if not str(settings.get("connection") or "").strip() and not settings.get("_connection") and \
                not str(settings.get("webhook") or "").strip():
            found.append("Name the Teams channel to post to (Integrations -> Teams).")
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
        channel = step.settings.get("_connection")
        if channel:
            return self._through(context, step, channel, text)
        webhook = str(self.setting(step.settings, "webhook")).strip()
        problem = _address_problem(webhook)
        if problem:
            return registry.failed(problem)
        mentions, skipped = people(self.setting(step.settings, "mentions"))
        context.cancel.raise_if_set()

        body = json.dumps(card(text, str(self.setting(step.settings, "title", "") or ""),
                               mentions), ensure_ascii=False).encode("utf-8")
        verify = bool(self.setting(step.settings, "verify_tls", True))
        try:
            status = send(webhook, body, verify_tls=verify)
        except SendError as exc:
            return registry.failed(str(exc), sent=False, status=exc.status, mentioned=0)
        from integrations.teams.webhook import host_of
        host = host_of(webhook)
        said = " mentioning %d" % len(mentions) if mentions else ""
        if skipped:
            said += "; not mentioned: " + " ".join(skipped)
        return registry.succeeded("teams: sent to %s (HTTP %d)%s" % (host, status, said),
                                  sent=True, status=status, mentioned=len(mentions))

    def _through(self, context, step, channel, text):
        """Post through the project's channel: its record, its one-a-minute and
        its access for cycles hold, as for anybody else who posts (ADR-015)."""
        from integrations.contracts import WriteError
        from integrations.teams import capabilities
        if ((channel.get("settings") or {}).get("access") or {}).get("cycles") == "off":
            return registry.failed("%s does not let cycles post. Change it in Integrations -> Teams -> "
                                   "Connections." % channel.get("name"), sent=False, status=0, mentioned=0)
        mentions = self.setting(step.settings, "mentions")
        names = mentions if isinstance(mentions, list) else [one for one in str(mentions or "").replace(
            "\n", ",").split(",") if one.strip()]
        run = getattr(context, "run", None)
        cycle = getattr(context, "cycle", None)
        source = {"label": "Cycle %s" % (getattr(cycle, "name", "") or getattr(run, "cycle_id", "") or ""),
                  "detail": "Run %s, step %s" % (getattr(run, "id", ""), step.id)}
        context.cancel.raise_if_set()
        try:
            made = capabilities.post(channel, {"title": str(self.setting(step.settings, "title", "") or ""),
                                               "text": text, "mentions": [str(one) for one in names]}, source)
        except WriteError as exc:
            return registry.failed("Teams: %s" % exc, sent=False, status=0, mentioned=0)
        except SendError as exc:
            return registry.failed(str(exc), sent=False, status=exc.status, mentioned=0)
        post = made["post"]
        return registry.succeeded("teams: posted to %s (HTTP %d)%s" % (
            channel.get("name"), post.get("status") or 0,
            " mentioning %d" % len(post.get("mentions") or []) if post.get("mentions") else ""),
            sent=True, status=post.get("status") or 0, mentioned=len(post.get("mentions") or []))
