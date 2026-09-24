"""Stopping to ask somebody before the run goes on.

Everything else a cycle does, it decides for itself. This is the one step whose
answer is a person's, and it exists for the places where that is the honest
arrangement: before an agent edits a repository somebody else also works in,
before work moves on a board other people read, before anything that would be
awkward to take back.

**Not answered is not approved.** No application attached, nobody at the
screen, the window closed, the timeout passed - every one of those fails the
step. A gate that approves when it cannot ask is not a gate, and it would stop
being one precisely where somebody believed they had one. There is deliberately
no setting to change that: a cycle that should not ask deletes the step.

**What it costs to leave one in.** A run that reaches this and nobody answers
waits out its timeout and then fails, so a cycle meant to run unattended at
three in the morning should not have one. That is a real cost and the reason
the step is not on by default anywhere.

**How the answer reaches it.** ``cycle/ask.py`` - the same pipe the service
steps use, asking a different question. With no GUI attached the step says so
in its first millisecond rather than waiting out the timeout, the way a service
step does.
"""

from cycle import ask as ask_mod
from cycle import registry
from cycle.registry import CyclePlugin, PluginMetadata, field, output


class ApprovalGate(CyclePlugin):
    metadata = PluginMetadata(
        id="approval.gate", name="Approval", category=registry.ACTION,
        asks_when_overdue=False,
        summary="Stop and ask a person. Anything but an answer fails the step.",
        # A person's agreement is a fact about *this* run. Running part of a
        # cycle used to take this step's result from the last run like any
        # other, so a gate somebody had answered an hour ago - or, worse, one
        # that had been skipped and was recorded as reused anyway - passed
        # silently in every run after it. Nobody was asked, and the cycle went
        # on to edit a repository on the strength of it.
        reusable=False,
        inputs=(
            field("question", "What to ask", "multiline", required=True,
                  hint="Written as the decision somebody is making, not as a "
                       "status. They are agreeing to what this says."),
            field("detail", "What they should see", "multiline",
                  hint="The diff, the plan, the findings - whatever the "
                       "decision rests on. Usually a ${...} reference to an "
                       "earlier step's output."),
            field("revision_step", "Plan step to revise", "text",
                  hint="Optional agent.review step to revisit with the person's feedback. "
                       "Its review chain runs again before this approval is shown again."),
            field("max_revisions", "Maximum plan revisions", "number", default=3,
                  hint="Maximum feedback rounds in this run. Each may call paid agents."),
            field("options", "The answers it takes", "args",
                  hint="Buttons, in order. The first is the one that means "
                       "yes. Blank offers Approve and Reject."),
            field("approve", "Which answers mean yes", "args",
                  hint="Blank means the first option. Name more than one when "
                       "several answers should let the run continue."),
        ),
        outputs=(
            output("approved", "boolean",
                   "True only when somebody chose an answer that means yes."),
            output("answer", "text", "What they chose."),
            output("who", "text", "Who answered, when the application says."),
        ),
    )

    def problems(self, settings):
        settings = settings or {}
        found = super().problems(settings)
        for key in ("question", "detail", "revision_step"):
            if key in settings and not isinstance(settings[key], str):
                found.append("%s must be text." % key)
        for key in ("options", "approve"):
            value = settings.get(key)
            if value is not None and not isinstance(value, (list, str)):
                found.append("%s must be a list of answers." % key)

        limit = settings.get("max_revisions", 3)
        if not isinstance(limit, (int, float)) or isinstance(limit, bool) or not 1 <= limit <= 20 or int(limit) != limit:
            found.append("max_revisions must be a whole number between 1 and 20.")
        options = _names(settings.get("options")) or list(ask_mod.DEFAULT_OPTIONS)
        approve = _names(settings.get("approve"))
        unknown = [one for one in approve
                   if one.strip().lower() not in
                   [other.strip().lower() for other in options]]
        if unknown and "${" not in str(settings.get("approve")):
            # Caught here rather than at run time, where it would read as a
            # rejection: an answer that means yes and is not on offer means
            # nobody can ever approve, and the step would fail looking as
            # though somebody had said no.
            found.append("%s cannot be chosen: the answers on offer are %s."
                         % (", ".join(unknown), ", ".join(options)))
        return found

    def execute(self, context, step):
        problems = self.problems(step.settings)
        if problems:
            return registry.failed("; ".join(problems))
        context.cancel.raise_if_set()

        options = _names(self.setting(step.settings, "options")) or \
            list(ask_mod.DEFAULT_OPTIONS)
        approve = _names(self.setting(step.settings, "approve")) or options[:1]
        question = str(self.setting(step.settings, "question") or "")

        revision = {}
        revision_step = str(self.setting(step.settings, "revision_step", "") or "")
        if revision_step:
            from cycle import revisions
            try:
                revisions.targets(context.cycle, step.id, revision_step)
            except ValueError as exc:
                return registry.failed(str(exc))
            used = sum(one.get("gate") == step.id for one in context.run.revisions)
            remaining = int(self.setting(step.settings, "max_revisions", 3)) - used
            revision = {"enabled": remaining > 0, "remaining": max(0, remaining),
                        "target": context.cycle.step(revision_step).title, "approvals": approve}

        context.stage(step.id, {"kind": "start", "title": "Waiting for an answer",
                                "detail": question, "status": "running"})
        answer = ask_mod.ask(
            question, detail=str(self.setting(step.settings, "detail", "") or ""),
            options=options,
            # The step's own timeout, so the deadline is written where every
            # other deadline in the file is rather than in a field of its own.
            timeout_ms=int(step.timeout * 1000) if step.timeout else None,
            step=step.id, cancel=context.cancel, revision=revision)

        if answer.get("revision_requested"):
            if not revision.get("enabled"):
                return registry.failed("The plan revision limit has been reached.")
            return registry.PluginResult(
                "waiting", message="Plan revision requested",
                revision={"target": revision_step, "feedback": answer["feedback"],
                          "who": answer.get("who", "")})

        chosen = answer.get("answer", "")
        approved = bool(answer.get("answered")) and chosen.strip().lower() in \
            [one.strip().lower() for one in approve]
        context.stage(step.id, {
            "kind": "review", "title": chosen or "No answer",
            "detail": answer.get("message", ""),
            "status": "done" if approved else "failed"})

        outputs = {"approved": approved, "answer": chosen,
                   "who": answer.get("who", "")}
        if approved:
            return registry.succeeded(message="approved: %s" % chosen, **outputs)
        # A refusal and a silence are different things and the message says
        # which, but neither lets the run go on.
        return registry.PluginResult(
            "failed", outputs=outputs,
            message=("not approved: %s" % chosen if answer.get("answered")
                     else answer.get("message", "nobody answered")))


def _names(value):
    """Answers from a list, or from the line somebody wrote instead."""
    if isinstance(value, str):
        return [one.strip() for one in value.split(",") if one.strip()] \
            if "," in value else value.split()
    return [str(one) for one in (value or [])]
