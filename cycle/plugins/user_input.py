"""Stopping to ask somebody to write something, and handing it to later steps.

``approval.gate`` asks for a decision; this asks for words. The task an agent
is to do, a branch name, a note for a report - anything a person types once
and a later step reads as ``${steps.<id>.outputs.text}``.

It asks over the same pipe as the gate (``cycle/ask.py``) and keeps the gate's
rule about silence: **not answered is not an answer.** No application attached,
nobody at the screen, the window closed, Cancel pressed, the timeout passed -
every one fails the step, so nothing after it runs on text nobody wrote. With
``required`` on, which is the default, sending an empty box is no answer
either.

Unlike the gate, what it collects may be reused. Running part of a cycle
(``--cycle-from``) or resuming a run takes the text written last time rather
than asking again: words somebody wrote are still what they wrote, where an
agreement given to a different run is not agreement to this one.
"""

from cycle import ask as ask_mod
from cycle import registry
from cycle.registry import CyclePlugin, PluginMetadata, field, output

#: The two answers. The first sends the text on; the other is a refusal.
SEND, CANCEL = "Send", "Cancel"


class UserInput(CyclePlugin):
    metadata = PluginMetadata(
        id="user.input", name="User Input", category=registry.ACTION,
        asks_when_overdue=False,
        summary="Stop and ask a person to write something for later steps.",
        inputs=(
            field("question", "What to ask", "multiline", required=True,
                  hint="What the person is asked to write, said as a request: "
                       "\"What should the agent do?\""),
            field("detail", "What they should see", "multiline",
                  hint="Anything that helps them write it - often a ${...} "
                       "reference to an earlier step's output."),
            field("placeholder", "Hint in the empty box", "text",
                  hint="Grey text shown until they start typing."),
            field("default", "Text to start from", "multiline",
                  hint="Put in the box already, for them to keep or change."),
            field("required", "Text is required", "check", default=True,
                  hint="Off lets them send an empty box; the output is then "
                       "empty text."),
        ),
        outputs=(
            output("text", "text", "What they wrote."),
            output("who", "text", "Who answered, when the application says."),
        ),
    )

    def problems(self, settings):
        settings = settings or {}
        found = super().problems(settings)
        for key in ("question", "detail", "placeholder", "default"):
            if key in settings and settings[key] is not None \
                    and not isinstance(settings[key], str):
                found.append("%s must be text." % key)
        return found

    def execute(self, context, step):
        problems = self.problems(step.settings)
        if problems:
            return registry.failed("; ".join(problems))
        context.cancel.raise_if_set()

        question = str(self.setting(step.settings, "question") or "")
        required = bool(self.setting(step.settings, "required", True))
        context.stage(step.id, {"kind": "start", "title": "Waiting for input",
                                "detail": question, "status": "running"})
        timeout_ms = int(step.timeout * 1000) if step.timeout else None
        answer = ask_mod.ask(
            question, detail=str(self.setting(step.settings, "detail", "") or ""),
            options=[SEND, CANCEL], timeout_ms=timeout_ms, step=step.id,
            cancel=context.cancel,
            text={"placeholder": str(self.setting(step.settings, "placeholder", "") or ""),
                  "default": str(self.setting(step.settings, "default", "") or ""),
                  "required": required})

        text = str(answer.get("text") or "")
        who = answer.get("who", "")
        sent = bool(answer.get("answered")) and answer.get("answer") == SEND
        if sent and required and not text:
            sent, message = False, "nothing was written"
        elif sent and len(text) > ask_mod.MAX_TEXT:
            sent, message = False, ("what was written is longer than %d characters"
                                    % ask_mod.MAX_TEXT)
        elif sent:
            message = "written: %d character(s)" % len(text)
        elif answer.get("answered"):
            message = "cancelled"
        else:
            message = answer.get("message", "nobody answered")

        context.stage(step.id, {
            "kind": "review", "title": "Written" if sent else "No input",
            "detail": text if sent else message,
            "status": "done" if sent else "failed"})
        if sent:
            return registry.succeeded(message=message, text=text, who=who)
        return registry.PluginResult("failed", message=message,
                                     outputs={"text": "", "who": who})
