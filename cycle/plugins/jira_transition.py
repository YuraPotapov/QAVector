"""Moving one Jira issue along its workflow.

``jira.issues`` reads and says so in its own first line, which is what lets a
cycle point it at a production instance without thinking. This writes, so it is
a separate plugin: the cycle file says plainly which of the two a step is, the
way ``agent.edit`` is separate from ``agent.review`` and ``git.commit`` from
``git.checkout``.

**The transition is named, never guessed.** A Jira workflow is the project's
own, and the transition out of To Do is called "Start Progress" in one instance
and "In Arbeit nehmen" in the next. So the step names what it wants, this asks
the issue what it can actually do, and a name that is not among them is refused
with **the list that was available** - which is the difference between a
usable error and a 400.

**Already there is not a failure.** A run killed between the transition and the
record re-runs and finds the issue where it wanted it. Jira would refuse the
transition at that point, because a workflow does not usually offer you the
move you have already made - so this looks first, reports ``changed: false``
and succeeds. That is the same reconcile ``git.commit`` does, and it needs no
store either.

**It cannot say who moved it.** Finding an issue in the target status proves
the status, not the author: somebody may have moved it by hand a minute ago.
Where that distinction matters, a cycle records its own intent with
``memory.remember`` before this step and reads it back after - the plugin does
not pretend to know.

**What it does not do.** No create, no assign, no edit of fields outside the
transition screen, no delete. Each of those is a different kind of write and
deserves to be asked for by name.
"""

from cycle import registry
from cycle.plugins.jira import CLOUD, SERVER, JiraError, Session
from cycle.registry import CyclePlugin, PluginMetadata, field, output


class JiraTransition(CyclePlugin):
    metadata = PluginMetadata(
        id="jira.transition", name="Jira Transition", category=registry.ACTION,
        summary="Move one issue along its workflow. Writes - and only this.",
        permissions=("network",),
        inputs=(
            field("site", "Site", required=True,
                  hint="https://yourcompany.atlassian.net, or your Jira "
                       "Server's base URL."),
            field("flavour", "Product", "choice", default=CLOUD,
                  options=(CLOUD, SERVER),
                  hint="Cloud and Server differ in their API path and in how a "
                       "comment's body is written. Nothing about the URL says "
                       "which you have."),
            field("auth", "Authentication", "choice", default="basic",
                  options=("basic", "bearer"),
                  hint="basic is Cloud: your email plus an API token. bearer "
                       "is a Server or Data Center personal access token."),
            field("email", "Account email",
                  hint="Only for basic. The account the API token belongs to."),
            field("token", "API token", required=True,
                  hint="Reference a secret variable - ${vars.jira_token} - so "
                       "the value stays out of this file."),
            field("issue", "Issue", required=True,
                  hint="The key, such as QA-7, or the numeric id."),
            field("to", "Transition", required=True,
                  hint="The transition's name as the workflow spells it - "
                       "'Start Progress', not the status it leads to - or its "
                       "id. Matched without regard to case. A name that is not "
                       "on offer is refused with the ones that are."),
            field("lands_in", "Where it should end up",
                  hint="The status the transition leads to. Given, the step "
                       "recognises an issue already there as work already "
                       "done - which a re-run needs, because a workflow stops "
                       "offering a move once it has been made. Also checked "
                       "afterwards: landing somewhere else fails the step "
                       "rather than being reported as success."),
            field("expect_status", "Only if it is in",
                  hint="Refuse unless the issue is in this status now. The "
                       "same idea as git.commit's verified tree: do not act on "
                       "something that changed under you."),
            field("comment", "Comment to leave", "multiline",
                  hint="Added as part of the transition, so the two are one "
                       "action in the issue's history rather than two."),
            field("fields", "Fields the screen asks for", "env",
                  hint="Only the ones the transition screen requires, such as "
                       "a resolution. Editing anything else belongs in a step "
                       "that says it edits."),
        ),
        outputs=(
            output("key", "text", "The issue that was moved."),
            output("status", "text", "The status it is in now."),
            output("from_status", "text", "The status it was in before."),
            output("changed", "boolean",
                   "False when it was already there and nothing was done."),
            output("transition", "text", "The transition that was used."),
            output("available", "list",
                   "What the workflow offered, which is what a refusal names."),
        ),
    )

    def problems(self, settings):
        settings = settings or {}
        found = super().problems(settings)
        for key in ("site", "email", "token", "issue", "to", "expect_status",
                    "comment"):
            if key in settings and not isinstance(settings[key], str):
                found.append("%s must be text." % key)
        site = str(settings.get("site") or "")
        if site and "${" not in site and not site.startswith(("http://", "https://")):
            found.append("Site must start with http:// or https://.")
        if (settings.get("auth", "basic") == "basic"
                and not str(settings.get("email") or "").strip()):
            found.append("Basic authentication needs the account email.")
        return found

    def execute(self, context, step):
        problems = self.problems(step.settings)
        if problems:
            return registry.failed("; ".join(problems))
        context.cancel.raise_if_set()

        ask = Session(
            site=str(self.setting(step.settings, "site")).rstrip("/"),
            auth=self.setting(step.settings, "auth", "basic"),
            email=str(self.setting(step.settings, "email", "") or ""),
            token=str(self.setting(step.settings, "token")),
            verify=True)
        flavour = self.setting(step.settings, "flavour", CLOUD)
        key = str(self.setting(step.settings, "issue")).strip()
        wanted = str(self.setting(step.settings, "to")).strip()

        try:
            return self._move(context, ask, flavour, key, wanted, step)
        except JiraError as exc:
            return registry.failed(str(exc))

    def _move(self, context, ask, flavour, key, wanted, step):
        version = "3" if flavour == CLOUD else "2"
        issue = ask("/rest/api/%s/issue/%s" % (version, key),
                    {"fields": "status"})
        before = ((issue.get("fields") or {}).get("status") or {}).get("name", "")

        expected = str(self.setting(step.settings, "expect_status", "") or "").strip()
        if expected and expected.lower() != before.lower():
            return registry.failed(
                "%s is in %r, not %r. Nothing was changed: the issue moved "
                "after the run decided what to do with it."
                % (key, before, expected))

        lands_in = str(self.setting(step.settings, "lands_in", "") or "").strip()
        if lands_in and before.strip().lower() == lands_in.lower():
            # Where a re-run lands: the work was done, and the workflow has
            # stopped offering the move that did it.
            return registry.succeeded(
                key=key, status=before, from_status=before, changed=False,
                transition="", available=[],
                message="jira: %s is already in %s" % (key, before))

        context.cancel.raise_if_set()
        offered = _transitions(ask, version, key)
        chosen = _match(offered, wanted)

        if chosen is None and _already(offered, before, wanted):
            # A run killed between the transition and the record finds the
            # issue where it wanted it, and the workflow no longer offering the
            # move is the ordinary shape of that - not a failure.
            return registry.succeeded(
                key=key, status=before, from_status=before, changed=False,
                transition="", available=[one["name"] for one in offered],
                message="jira: %s is already in %s" % (key, before))
        if chosen is None:
            return registry.failed(
                "%s has no transition called %r from %s. It offers: %s"
                % (key, wanted, before or "its current status",
                   ", ".join(one["name"] for one in offered) or "nothing"))

        payload = {"transition": {"id": chosen["id"]}}
        fields = self.setting(step.settings, "fields", {}) or {}
        if fields:
            payload["fields"] = dict(fields)
        comment = str(self.setting(step.settings, "comment", "") or "").strip()
        if comment:
            payload["update"] = {"comment": [{"add": _body(comment, flavour)}]}

        ask("/rest/api/%s/issue/%s/transitions" % (version, key), body=payload)

        # Read back rather than assume: a post-function in the workflow can
        # land the issue somewhere other than where the transition's name
        # suggested, and reporting the name would be reporting an intention.
        after = ask("/rest/api/%s/issue/%s" % (version, key),
                    {"fields": "status"})
        now = ((after.get("fields") or {}).get("status") or {}).get("name", "")
        if lands_in and now.strip().lower() != lands_in.lower():
            # The step said where this should end up and it did not. A
            # post-function moved it on, and reporting success would be
            # claiming something nobody checked.
            return registry.PluginResult(
                "failed",
                outputs={"key": key, "status": now, "from_status": before,
                         "changed": True, "transition": chosen["name"],
                         "available": [one["name"] for one in offered]},
                message="%s went to %r, not the %r the step expected. The "
                        "transition was made; something in the workflow moved "
                        "it on." % (key, now, lands_in))
        return registry.succeeded(
            key=key, status=now, from_status=before, changed=True,
            transition=chosen["name"],
            available=[one["name"] for one in offered],
            message="jira: %s %s -> %s" % (key, before or "?", now or "?"))


# -- the workflow -------------------------------------------------------------
def _transitions(ask, version, key):
    """What this issue can be moved by right now, as ``[{id, name, to}]``."""
    found = ask("/rest/api/%s/issue/%s/transitions" % (version, key),
                {"expand": "transitions.fields"})
    offered = []
    for one in found.get("transitions") or []:
        if not isinstance(one, dict):
            continue
        offered.append({"id": str(one.get("id", "")),
                        "name": str(one.get("name", "")),
                        "to": _status_name(one.get("to"))})
    return offered


def _status_name(value):
    """The status a transition leads to.

    Jira sends an object; the check is here because this reads somebody else's
    API and a variant that sends the name as a string should cost a blank field
    rather than an exception out of a step.
    """
    if isinstance(value, dict):
        return str(value.get("name") or "")
    return str(value or "")


def _match(offered, wanted):
    """The transition the step asked for, by name or by id. Case is ignored.

    Names are what a person writes and ids are what a script that read the
    workflow has; both are accepted because refusing one of them would make
    somebody look up the other for no reason.
    """
    wanted = wanted.strip().lower()
    for one in offered:
        if one["name"].strip().lower() == wanted or one["id"] == wanted:
            return one
    return None


def _already(offered, status, wanted):
    """Whether being where we are is the answer the step was asking for.

    True when the current status is what the step named, or what the named
    transition would have led to - the second case being the one a re-run hits,
    since a workflow does not usually keep offering a move already made.
    """
    if status and status.strip().lower() == wanted.strip().lower():
        return True
    return any(one["to"] and one["to"].strip().lower() == status.strip().lower()
               and one["name"].strip().lower() == wanted.strip().lower()
               for one in offered)


def _body(text, flavour):
    """A comment, in the shape the flavour wants.

    Server takes a string. Cloud's v3 takes Atlassian Document Format, so a
    plain paragraph is built rather than sent as text and rejected.
    """
    if flavour != CLOUD:
        return {"body": text}
    return {"body": {"type": "doc", "version": 1,
                     "content": [{"type": "paragraph",
                                  "content": [{"type": "text",
                                               "text": text}]}]}}
