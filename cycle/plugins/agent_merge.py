"""An agent that merges branches in a local repository, and can push nowhere.

``agent.edit`` changes files and runs nothing, on purpose. Merging is the one
job where that is not enough: the work *is* ``git merge``, and a conflict is
resolved by editing files and then ``git add`` and ``git commit``. So this is
its own plugin, and the cycle file says plainly which kind of step it is.

**Local git from a list, and reading.** The agent gets the editing tools and a
fixed set of git commands - merge, status, diff, log, show, add, commit, and
the few others a merge needs. The Claude Code CLI also lets through on its own
the commands it judges read-only - grep, head, ls, echo and the like - and a
merge agent uses them to look around; they cannot write or reach anything.
Whatever else the shell can do is refused, and the commands that reach
another repository - push, fetch, pull, remote, clone, config - are refused by
name as well, which the CLI applies before any allowance.

**And a repository with no remote.** The step refuses to start in a
repository that has a remote, and checks again afterwards. A list of refused
commands is a promise about the CLI; a repository with nowhere to push to is a
fact about the disk. The intended arrangement is a scratch clone made by a
``command.shell`` step that removes its remote, with the result taken back by
another step, deliberately, after somebody has looked.

**What it ran is reported, not described.** Every command the agent ran is
read off its event stream and published as ``commands``, so a review after it
can check what was done against what was asked - the order of the merges, a
push attempted - instead of taking the agent's own account.
"""

import os
import subprocess

from cycle import registry, restrict
from cycle.plugins import agent_cli, agent_run, agent_stream
from cycle.plugins.agent import AgentReview, read_files
from cycle.plugins.agent_edit import AgentEdit
from cycle.registry import CyclePlugin, PluginMetadata, field, output

#: What the agent may run. Prefix rules, as the Claude Code CLI reads them:
#: ``git merge --no-ff:*`` allows that command with anything after it, and a
#: plain ``git merge`` is not among them - a release merge is always --no-ff.
GIT_ALLOWED = (
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
    "Bash(git show:*)", "Bash(git rev-parse:*)", "Bash(git ls-files:*)",
    "Bash(git merge-base:*)", "Bash(git grep:*)", "Bash(git branch --show-current)",
    "Bash(git branch --list:*)", "Bash(git merge --no-ff:*)",
    "Bash(git merge --abort)", "Bash(git merge --continue)",
    "Bash(git add:*)", "Bash(git rm:*)", "Bash(git commit:*)",
    "Bash(git checkout --ours:*)", "Bash(git checkout --theirs:*)",
    "Bash(git reset --hard:*)", "Bash(git reflog:*)")

#: Refused by name, which the CLI applies before any allowance: everything
#: that reaches another repository or changes how this one is configured.
GIT_REFUSED = (
    "Bash(git push:*)", "Bash(git fetch:*)", "Bash(git pull:*)",
    "Bash(git remote:*)", "Bash(git clone:*)", "Bash(git config:*)",
    "Bash(git submodule:*)", "Bash(git worktree:*)", "Bash(git update-ref:*)",
    "Bash(git filter-branch:*)", "Bash(git gc:*)")

TOOLS = agent_run.EDIT_TOOLS + GIT_ALLOWED


class AgentMerge(CyclePlugin):
    metadata = PluginMetadata(
        recoverable=True,
        id="agent.merge", name="Agent Merge", category=registry.AGENT,
        summary="Let an agent merge branches and resolve conflicts in a local "
                "repository that has no remote. Local git only - it cannot push.",
        permissions=("process.spawn", "filesystem.read", "filesystem.write",
                     "network"),
        inputs=(
            field("claude", "Claude Code binary", "file",
                  hint="Blank finds `claude` on PATH."),
            field("model", "Model", required=True,
                  hint="An alias such as opus or sonnet."),
            field("effort", "Effort", "choice", default="",
                  options=agent_run.EFFORT_LEVELS,
                  hint="How hard the model is asked to think."),
            agent_run.min_effort_field(),
            field("task", "Merging task", "multiline", required=True,
                  hint="What to merge, in which order, and how conflicts are "
                       "resolved. It runs git itself, so be exact."),
            field("directory", "Repository", "dir", required=True,
                  hint="A git work tree with no remote - a scratch clone whose "
                       "remote a step before this removed. A repository with a "
                       "remote is refused."),
            field("inputs", "Structured inputs", "env",
                  hint="A mapping of results or context for the work."),
            field("files", "Logs and artifacts", "args",
                  hint="Text files within the run workspace to include."),
            field("max_iterations", "Maximum tool iterations", "number",
                  hint="Empty for no limit: the step's timeout bounds it."),
        ),
        outputs=(
            output("commands", "list",
                   "Every shell command the agent ran, in order - for a later "
                   "step to check against what it was asked to do. Only the "
                   "ones that ran: an attempt the CLI refused is in refused."),
            output("command_count", "number", "How many commands it ran."),
            output("refused", "list",
                   "The commands the CLI would not run, in order. Attempted and "
                   "blocked - nothing they would have done happened."),
            output("files_changed", "list", "Every path the agent wrote."),
            output("changed", "number", "How many distinct files it wrote."),
            output("branch", "text", "The branch checked out when it finished."),
            output("merging", "number",
                   "1 when a merge was left unfinished (MERGE_HEAD exists), "
                   "else 0."),
            output("summary", "text", "What the agent said it did."),
            output("cost_usd", "number", "What the run cost, when the CLI says."),
            output("effort", "text", "The effort level it ran at."),
            output("stdout_path", "text", "The whole event stream, on disk."),
        ),
        actions=AgentReview.metadata.actions,
        accepts=(registry.accepts("text", "Clarification",
                                  when="Before its only agent call starts"),),
    )

    def run_action(self, key, settings):
        return AgentReview().run_action(key, settings)

    def problems(self, settings):
        # The same fields as agent.edit's, checked the same way.
        return AgentEdit().problems(settings)

    def execute(self, context, step):
        problems = self.problems(step.settings)
        if problems:
            return registry.failed("; ".join(problems))
        if context.cancel.is_set():
            return registry.failed("cancelled before starting the agent")

        where = os.path.realpath(os.path.join(
            context.workspace,
            os.path.expanduser(str(self.setting(step.settings, "directory")))))
        if not os.path.isdir(os.path.join(where, ".git")):
            return registry.failed("Not a git repository: %s" % where)
        remotes = _remotes(where)
        if remotes is None:
            return registry.failed("Cannot ask git about %s" % where)
        if remotes:
            return registry.failed(
                "%s has a remote (%s). agent.merge works only in a repository "
                "that can push nowhere - remove the remote in a step before it."
                % (where, ", ".join(remotes)))

        binary = self.setting(step.settings, "claude", "")
        status = agent_cli.auth_status(binary)
        if status["problem"]:
            return registry.failed(status["problem"])
        try:
            files = read_files(context, self.setting(step.settings, "files", []))
        except (OSError, ValueError) as exc:
            return registry.failed("Cannot prepare agent files: %s" % exc)

        prompt = _prompt(str(self.setting(step.settings, "task") or ""),
                         self.setting(step.settings, "inputs", {}), files, where)
        reply = agent_run.run_claude(
            context, step, prompt, where, TOOLS, status["binary"],
            mode=agent_run.ACCEPT_EDITS, add_dir=where,
            model=self.setting(step.settings, "model"),
            effort=agent_run.effort_for(self, step)[0],
            max_turns=self.setting(step.settings, "max_iterations"),
            clarify=True, last=True, disallowed=GIT_REFUSED)

        result = reply.result
        ran, refused = _commands(context, result.outputs.get("stdout_path", ""))
        result.outputs = dict(result.outputs or {},
                              commands=ran, command_count=len(ran),
                              refused=refused,
                              files_changed=reply.written,
                              changed=len(reply.written),
                              branch=_branch(where),
                              merging=int(os.path.exists(
                                  os.path.join(where, ".git", "MERGE_HEAD"))))
        after = _remotes(where)
        if after:
            result.status = "failed"
            result.message = ("agent.merge: %s gained a remote (%s) during the "
                              "step" % (where, ", ".join(after)))
            return result
        if not reply.ok:
            result.status = "failed"
            result.message = reply.problem
            return result
        result.outputs["summary"] = reply.text
        if reply.cost is not None:
            result.outputs["cost_usd"] = reply.cost
        effort, asked = agent_run.effort_for(self, step)
        result.outputs["effort"] = effort
        result.message = agent_run.with_effort(
            "agent.merge: %d command%s, %d file%s changed%s" % (
                len(ran), "" if len(ran) == 1 else "s",
                len(reply.written), "" if len(reply.written) == 1 else "s",
                ", merge left unfinished" if result.outputs["merging"] else ""),
            effort, asked)
        return result


def _git(where, *args):
    try:
        done = restrict.run(["git", "-C", where] + list(args),
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _remotes(where):
    """The repository's remotes, by name; None when git cannot be asked."""
    answer = _git(where, "remote")
    if answer is None:
        return None
    return [line.strip() for line in answer.splitlines() if line.strip()]


def _branch(where):
    return (_git(where, "branch", "--show-current") or "").strip()


def _commands(context, stdout_path):
    """``(ran, refused)``: the commands the agent ran, and the ones the CLI
    would not run, read back off its saved event stream.

    Kept apart because a review reads the list as what happened: a refused
    attempt listed among the commands that ran reads as a rule broken - an
    agent that tried a script, was stopped and did the job another way was
    reported as having run the script.
    """
    if not stdout_path:
        return [], []
    path = stdout_path if os.path.isabs(stdout_path) else os.path.join(
        context.workspace, stdout_path)
    calls, refused_ids = [], set()
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                calls.extend(agent_stream.command_calls(line))
                refused_ids.update(agent_stream.refused_calls(line))
    except OSError:
        return [], []
    ran = [command for call_id, command in calls if call_id not in refused_ids]
    refused = [command for call_id, command in calls if call_id in refused_ids]
    return ran, refused


def _prompt(task, inputs, files, where):
    parts = ["You are merging branches for an automated pipeline.", "",
             "# Task", task.strip(), "",
             "# Where",
             "The repository is %s, and you are in it." % where,
             "You may run only local git commands: status, diff, log, show, "
             "rev-parse, ls-files, merge-base, grep, branch --show-current, "
             "branch --list, merge --no-ff, merge --abort, merge --continue, "
             "add, rm, commit, checkout --ours/--theirs, reset --hard, reflog, "
             "and read-only commands such as grep or head. Anything else is "
             "refused, and so is a shell loop. The repository has no remote: "
             "nothing can be pushed or fetched, so do not try.",
             "Edit files only to resolve conflicts. Do not reformat, rename or "
             "tidy anything else - every change you make is kept."]
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
              "Say what you merged, in order, what conflicted and how you "
              "resolved it, and what you did not merge and why. Plain prose."]
    return "\n".join(parts)
