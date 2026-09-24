"""An agent that changes files, as a step of its own.

**Why this is not a flag on ``agent.review``.** A review's whole contract is
that it reads: the tools it is given are ``Read``, ``Glob`` and ``Grep``, and
that is what lets somebody point one at a real checkout without thinking about
it. Adding "may also edit" as a setting would mean every ``agent.review`` step
in every cycle file quietly changes meaning depending on one line further down,
and two steps that do very different things would read the same at a glance.
A separate plugin makes the cycle file say which it is, in the one place
somebody looks.

**Edits are applied, not proposed.** The CLI runs with ``--permission-mode
acceptEdits``: there is no human at a terminal during a run, so a step that
waited to be asked would simply hang. That is the bargain this step makes and
the reason its name says ``edit``. Point it at a checkout you can throw away or
one you can diff afterwards - a `git` step before it and a `command.shell`
running `git diff` after it is the arrangement this is built for.

**What it may not do.** ``Bash`` is not among its tools. A step that needs to
run something has ``command.shell``, which says so in the cycle file and is
covered by the same timeout and cancel machinery. Handing an editing agent a
shell would make "what did this step do" unanswerable from the file.

What it changed is read off the CLI's own event stream rather than by diffing a
directory afterwards: every write announces its path as it happens, so the
answer is exact and costs nothing.
"""

import os

from cycle import registry
from cycle.plugins import agent_cli, agent_run
from cycle.plugins.agent import AgentReview, read_files
from cycle.registry import CyclePlugin, PluginMetadata, field, output

#: Read, and write. Deliberately without ``Bash`` - see the module docstring.
#: Kept as names here because the cycle file and the tests read them, while the
#: values live with the machinery that uses them.
EDIT_TOOLS = agent_run.EDIT_TOOLS
PERMISSION_MODE = agent_run.ACCEPT_EDITS


class AgentEdit(CyclePlugin):
    metadata = PluginMetadata(
        recoverable=True,
        id="agent.edit", name="Agent Edit", category=registry.AGENT,
        summary="Let an agent change files in a directory. Edits are applied "
                "as they are made, without asking.",
        permissions=("process.spawn", "filesystem.read", "filesystem.write",
                     "network"),
        inputs=(
            field("claude", "Claude Code binary", "file",
                  hint="Blank finds `claude` on PATH. It signs in through the "
                       "browser and keeps the credentials itself - there is no "
                       "API key here."),
            field("model", "Model", required=True,
                  hint="An alias such as opus or sonnet."),
            field("effort", "Effort", "choice", default="",
                  options=agent_run.EFFORT_LEVELS,
                  hint="How hard the model is asked to think. Blank leaves it "
                       "to whatever the Claude Code CLI is configured to use."),
            field("task", "Editing task", "multiline", required=True,
                  hint="What to change, and what to leave alone. Be specific: "
                       "this step applies what it decides."),
            field("directory", "Directory to edit", "dir", required=True,
                  hint="Relative paths are read against the run's own "
                       "workspace, so a checkout step's output can be handed "
                       "straight in. An absolute path edits that path itself."),
            field("inputs", "Structured inputs", "env",
                  hint="A mapping of results or context for the work."),
            field("files", "Logs and artifacts", "args",
                  hint="Text files within the run workspace to include."),
            field("max_iterations", "Maximum tool iterations", "number",
                  hint="Empty for no limit: the step's timeout bounds it."),
        ),
        outputs=(
            output("files_changed", "list",
                   "Every path the agent wrote, in the order it wrote them."),
            output("changed", "number", "How many distinct files it changed."),
            output("summary", "text", "What the agent said it did."),
            output("cost_usd", "number", "What the run cost, when the CLI says."),
            output("effort", "text",
                   "The effort level it ran at; empty for the CLI's own default."),
            output("stdout_path", "text", "The whole event stream, on disk."),
        ),
        # The same two the review offers, and for the same reason: signing in
        # belongs to the plugin that needs it, not to a row in Settings.
        actions=AgentReview.metadata.actions,
    )

    def run_action(self, key, settings):
        """Delegated whole: it is the same CLI and the same sign-in."""
        return AgentReview().run_action(key, settings)

    def problems(self, settings):
        settings = settings or {}
        literal = dict(settings)
        for key in ("effort", "max_iterations", "inputs"):
            if isinstance(literal.get(key), str) and "${" in literal[key]:
                literal.pop(key)
        found = super().problems(literal)
        for key in ("model", "task", "directory", "claude"):
            if key in settings and not isinstance(settings[key], str):
                found.append("%s must be text." % key)
        files = settings.get("files", [])
        if not (isinstance(files, str) and "${" in files):
            if not isinstance(files, list) or any(not isinstance(one, str)
                                                  for one in files):
                found.append("Files must be a list of paths.")
        count = settings.get("max_iterations")
        if count not in (None, "") and not (isinstance(count, str) and "${" in count):
            try:
                if (isinstance(count, bool) or not 1 <= int(count) <= 200
                        or float(count) != int(count)):
                    raise ValueError()
            except (TypeError, ValueError, OverflowError):
                found.append("Maximum tool iterations must be an integer from "
                             "1 to 200.")
        return found

    def execute(self, context, step):
        problems = self.problems(step.settings)
        if problems:
            return registry.failed("; ".join(problems))
        if context.cancel.is_set():
            return registry.failed("cancelled before starting the agent")

        binary = self.setting(step.settings, "claude", "")
        status = agent_cli.auth_status(binary)
        if status["problem"]:
            return registry.failed(status["problem"])

        where = os.path.realpath(os.path.join(
            context.workspace,
            os.path.expanduser(str(self.setting(step.settings, "directory")))))
        if not os.path.isdir(where):
            # Refused rather than created: an agent let loose on a directory
            # that was meant to be a checkout and is in fact empty would work
            # very hard on nothing.
            return registry.failed("Directory to edit does not exist: %s" % where)
        try:
            files = read_files(context, self.setting(step.settings, "files", []))
        except (OSError, ValueError) as exc:
            return registry.failed("Cannot prepare agent files: %s" % exc)

        prompt = _prompt(str(self.setting(step.settings, "task") or ""),
                         self.setting(step.settings, "inputs", {}), files, where)
        reply = agent_run.run_claude(
            context, step, prompt, where, EDIT_TOOLS, status["binary"],
            mode=PERMISSION_MODE, add_dir=where,
            model=self.setting(step.settings, "model"),
            effort=self.setting(step.settings, "effort", ""),
            max_turns=self.setting(step.settings, "max_iterations"))

        # What it changed is reported whether or not the run went well. A step
        # that failed halfway has still edited whatever it edited, and leaving
        # that out of the record would be the worst possible answer.
        result = reply.result
        result.outputs = dict(result.outputs or {},
                              files_changed=reply.written,
                              changed=len(reply.written))
        if not reply.ok:
            result.status = "failed"
            result.message = reply.problem
            return result

        result.outputs["summary"] = reply.text
        if reply.cost is not None:
            result.outputs["cost_usd"] = reply.cost
        effort = self.setting(step.settings, "effort", "") or ""
        result.outputs["effort"] = effort
        result.message = agent_run.with_effort("agent.edit: %d file%s changed" % (
            len(reply.written), "" if len(reply.written) == 1 else "s"), effort)
        return result


def _prompt(task, inputs, files, where):
    """The whole request, as one prompt.

    No answer schema, unlike the review's: the work here is the edit, and what
    it says afterwards is a note for a person rather than a value a later step
    branches on. Asking an editing agent for JSON would put the useful half of
    its answer inside a string.
    """
    parts = ["You are making changes for an automated pipeline.", "",
             "# Task", task.strip(), "",
             "# Where",
             "The files are at %s. Edit them in place." % where,
             "Change only what the task asks for. Do not reformat, rename or "
             "tidy anything you were not asked about - this runs unattended "
             "and every change you make is applied.",
             "You cannot run commands, so do not plan to test what you write."]
    if inputs:
        import json
        parts += ["", "# Results so far",
                  json.dumps(inputs, indent=2, ensure_ascii=False, default=str)]
    for entry in files:
        parts += ["", "# File: %s%s" % (entry["path"],
                                        " (truncated)" if entry["truncated"]
                                        else ""),
                  "```", entry["content"].rstrip(), "```"]
    parts += ["", "# When you are done",
              "Say in a short paragraph what you changed and why, and name any "
              "file you decided to leave alone. Plain prose, not JSON."]
    return "\n".join(parts)
