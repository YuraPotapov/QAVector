"""Make a change and prove it works: edit, run the checks, fix, repeat.

``agent.edit`` makes one pass and stops. That is the right shape when a person
is going to read the diff, and the wrong one when the step is supposed to
*finish* something: an agent that cannot run the tests cannot know whether what
it wrote works, so the first pass is a guess and the cycle file has no way to
say "keep going until it does".

This step closes that loop. It edits, runs the checks the step names, and when
they fail it goes round again with the failure in front of it - up to a budget
it owns itself.

**Why the loop is inside the step rather than in the graph.** The engine is a
DAG and refuses a graph with a ring in it, deliberately (see
``cycle/model.py``). That refusal is not what is being worked around here: the
loop genuinely belongs inside. What a person reads off the canvas is "make the
change and prove it" - one thing, that either held or did not. How many times
the agent went round is the step's own business, the way ``retry`` is, and the
way ``scenario.run``'s internal jobs are. Putting it in the graph would put a
counter on the canvas and call it structure.

**Verified means the checks agreed, not that the agent says so.** The agent's
own account of what it did is an output, never the verdict: `verified` is true
only when every named check exited zero and - unless the step turned it off - a
second agent reading only the result signed it off. That separation is the
whole point of the step; an agent marking its own homework is worth nothing.

**The handover to the commit.** The `tree` output is the git tree the working
directory hashed to at the moment the checks passed. ``git.commit`` takes it
and refuses to commit anything else, so "the thing that was verified" and "the
thing that was committed" are the same object rather than two descriptions that
are usually the same.
"""

import json
import os
import re
import subprocess
import tempfile

from cycle import registry
from cycle.plugins import agent_cli, agent_run
from cycle.plugins.agent import AgentReview, read_files
from cycle.plugins.agent_worker import extract_json, validate_review
from cycle.plugins._process import run_process
from cycle.registry import CyclePlugin, PluginMetadata, field, output
from domain.cycle import Artifact

#: Severities that send the loop round again. A low-severity note is reported
#: and not acted on: looping on nits would spend the whole budget polishing
#: something that already works, which is the opposite of what the budget is
#: for.
BLOCKING = ("high", "medium")

#: How long any one of the small git queries may take. They are sub-second on
#: any repository; the bound is so a broken git cannot hang a run.
GIT_TIMEOUT = 30

#: How much of a failed check's output goes into the next attempt's prompt.
#: Enough to see the failure and its traceback, not so much that the prompt is
#: mostly log.
FAILURE_CHARS = 4000


class AgentImplement(CyclePlugin):
    metadata = PluginMetadata(
        recoverable=True,
        id="agent.implement", name="Agent Implement", category=registry.AGENT,
        summary="Make a change and prove it: edit, run the checks, fix what "
                "failed, repeat until they pass.",
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
                  hint="How hard the model is asked to think, on every attempt "
                       "and on the review that signs them off. Blank leaves it "
                       "to whatever the Claude Code CLI is configured to use."),
            agent_run.min_effort_field(),
            field("directory", "Directory to work in", "dir", required=True,
                  hint="Relative paths are read against the run's own "
                       "workspace, so a checkout step's output can be handed "
                       "straight in. An absolute path works in that path."),
            field("task", "What to do", "multiline", required=True,
                  hint="The change to make. Be specific about what to leave "
                       "alone: this step applies what it decides."),
            field("acceptance", "What done means", "multiline",
                  hint="The criteria the result is judged against. Also what "
                       "the review gate reads, so vague criteria make a vague "
                       "gate."),
            field("checks", "Checks that must pass", "multiline",
                  hint="One command per line, run in the directory above. "
                       "Every one must exit zero for the step to be verified. "
                       "Empty means the review below is the only verdict - "
                       "the step says so in its result - and with the review "
                       "turned off as well it refuses, since nothing would "
                       "verify anything."),
            field("max_attempts", "Attempts", "number", default=3,
                  hint="How many times it may edit and re-check before giving "
                       "up. The budget is the step's own and does not survive "
                       "into another run."),
            field("review", "Review the result", "check", default=True,
                  hint="After the checks pass, a second agent reads only the "
                       "result and judges it against the criteria. Off when "
                       "the checks alone are the definition of done."),
            field("inputs", "Structured inputs", "env",
                  hint="A mapping of results or context for the work."),
            field("files", "Logs and artifacts", "args",
                  hint="Text files within the run workspace to include."),
            field("max_iterations", "Maximum tool iterations", "number",
                  hint="Per attempt, inside the agent. Empty for no limit: "
                       "the step's timeout bounds it."),
        ),
        outputs=(
            output("verified", "boolean",
                   "True only when every check passed and the review agreed."),
            output("checked", "boolean",
                   "Whether any check was run at all. False means verified "
                   "rests on the review alone."),
            output("attempts", "number", "How many passes it took."),
            output("files_changed", "list", "Every path written, once each."),
            output("changed", "number", "How many distinct files changed."),
            output("tree", "text",
                   "The git tree the verified state hashes to. Hand it to "
                   "git.commit, which refuses to commit anything else."),
            output("summary", "text", "What the agent said it did."),
            output("failed_check", "text",
                   "The check that was still failing when the budget ran out."),
            output("findings", "list", "What the review still objected to."),
            output("cost_usd", "number", "What every attempt cost together."),
            output("effort", "text",
                   "The effort level every attempt and its review ran at; "
                   "empty for the CLI's own default."),
            output("report_path", "text", "The whole record, as JSON on disk."),
        ),
        # The same sign-in the other agent steps offer, and the same CLI.
        actions=AgentReview.metadata.actions,
    )

    def run_action(self, key, settings):
        return AgentReview().run_action(key, settings)

    def problems(self, settings):
        settings = settings or {}
        literal = dict(settings)
        for key in ("effort", "min_effort", "max_attempts", "max_iterations", "inputs", "review"):
            if isinstance(literal.get(key), str) and "${" in literal[key]:
                literal.pop(key)
        found = super().problems(literal)
        for key in ("model", "task", "directory", "claude", "acceptance",
                    "checks"):
            if key in settings and not isinstance(settings[key], str):
                found.append("%s must be text." % key)
        files = settings.get("files", [])
        if not (isinstance(files, str) and "${" in files):
            if not isinstance(files, list) or any(not isinstance(one, str)
                                                  for one in files):
                found.append("Files must be a list of paths.")
        for key, ceiling in (("max_attempts", 20), ("max_iterations", 200)):
            count = settings.get(key, 3)
            if isinstance(count, str) and "${" in count:
                continue
            if key == "max_iterations" and count in (None, ""):
                continue
            try:
                if (isinstance(count, bool) or not 1 <= int(count) <= ceiling
                        or float(count) != int(count)):
                    raise ValueError()
            except (TypeError, ValueError, OverflowError):
                found.append("%s must be an integer from 1 to %d."
                             % (key, ceiling))
        return found

    def execute(self, context, step):
        problems = self.problems(step.settings)
        if problems:
            return registry.failed("; ".join(problems))
        context.cancel.raise_if_set()

        binary = self.setting(step.settings, "claude", "")
        status = agent_cli.auth_status(binary)
        if status["problem"]:
            return registry.failed(status["problem"])

        where = os.path.realpath(os.path.join(
            context.workspace,
            os.path.expanduser(str(self.setting(step.settings, "directory")))))
        if not os.path.isdir(where):
            return registry.failed("Directory to work in does not exist: %s"
                                   % where)
        checks = [one.strip() for one
                  in str(self.setting(step.settings, "checks", "") or "").splitlines()
                  if one.strip()]
        if not checks and not self.setting(step.settings, "review", True):
            # No checks is allowed - the review is then the verdict, and the
            # result says so. No checks *and* no review is refused: a step
            # called implement that verifies nothing would report verified on
            # any edit at all, which is the one answer nobody could act on.
            return registry.failed(
                "agent.implement needs checks or the review. With neither "
                "there is nothing to verify against; use agent.edit if that "
                "is what you meant.")
        try:
            files = read_files(context, self.setting(step.settings, "files", []))
        except (OSError, ValueError) as exc:
            return registry.failed("Cannot prepare agent files: %s" % exc)

        return self._loop(context, step, status["binary"], where, checks, files)

    # -- the loop -------------------------------------------------------------
    def _loop(self, context, step, binary, where, checks, files):
        task = str(self.setting(step.settings, "task") or "")
        acceptance = str(self.setting(step.settings, "acceptance", "") or "")
        inputs = self.setting(step.settings, "inputs", {})
        model = self.setting(step.settings, "model")
        effort = agent_run.effort_for(self, step)[0]
        turns = self.setting(step.settings, "max_iterations")
        budget = int(self.setting(step.settings, "max_attempts", 3))
        reviewing = bool(self.setting(step.settings, "review", True))

        written, findings = [], []
        cost, attempts, summary, failure = 0.0, 0, "", ""
        verified = False

        # The checks once before anything is spent. A check that cannot pass
        # whatever the agent writes - pytest pointed at modules it cannot
        # import, a command that is not installed - otherwise buys the whole
        # attempt budget of paid runs, each told to fix a failure no edit
        # reaches.
        # Work an earlier run of this step already paid for, when the checkout
        # still holds exactly what it left. Then the check below is not "before
        # the change" but the verification of it - the case of an attempt that
        # did its work and failed on a check somebody has since fixed.
        source, earlier, returned = _earlier_work(context, step, where)
        if earlier is not None:
            written = list(earlier.get("files_changed") or [])
            summary = str(earlier.get("summary") or "")
            self._say(context, step, "start",
                      "Continuing the work of %s" % source,
                      "The checkout is exactly as that run left it (%d file(s) "
                      "changed), so it is checked before any new attempt."
                      % len(written))
            if returned:
                self._say(context, step, "review",
                          "Returned for rework: %d finding(s)" % len(returned),
                          detail=_findings_text(returned), status="failed")

        before = self._check_all(context, step, checks, where, 0,
                                 "Check the earlier work: " if earlier is not None
                                 else "Check before the change: ")
        if before and _unchanged(where):
            return self._answer(
                context, step, where, False, 0, written, "", before, [], cost,
                "agent.implement: the checks fail before any change, so nothing "
                "an agent writes could be verified. Make them pass on the "
                "untouched checkout first. %s" % _first_line(before))
        if earlier is not None:
            # What the first new attempt is told: the earlier work's check or
            # review failure, so it fixes that rather than starting over.
            failure = before
            if returned:
                # The steps after this one already judged this exact tree and
                # would not let it through. Passing checks do not overturn
                # that, so the work goes back with what they said.
                failure = "\n\n".join(part for part in (
                    before, "the steps after this one returned the work:\n"
                    + _findings_text(returned)) if part)
            if not failure and reviewing:
                findings, failure = self._review(
                    context, step, binary, where, task, acceptance, model, 0, effort)
            if not failure:
                return self._answer(
                    context, step, where, True, 0, written, summary, "", findings,
                    cost, "agent.implement: verified the work of %s without "
                          "another attempt" % source)
        elif before and _edited(where):
            # Edits are already in the checkout - an earlier attempt's, a merge
            # somebody brought in - and the checks fail on them. The first
            # attempt starts from that failure rather than being told nothing.
            failure = before

        for attempt in range(1, budget + 1):
            attempts = attempt
            context.cancel.raise_if_set()
            self._say(context, step, "start",
                      "Attempt %d of %d" % (attempt, budget))

            reply = agent_run.run_claude(
                context, step,
                _prompt(task, acceptance, inputs, files, where, checks, failure,
                        _conflicted(where)),
                where, agent_run.EDIT_TOOLS, binary,
                mode=agent_run.ACCEPT_EDITS, add_dir=where, model=model,
                effort=effort, max_turns=turns, name="attempt-%d" % attempt)
            for path in reply.written:
                if path not in written:
                    written.append(path)
            if reply.cost is not None:
                cost += reply.cost
            if not reply.ok:
                # The CLI itself failed - a different thing from the work not
                # holding up, and not worth spending the rest of the budget on.
                return self._answer(context, step, where, False, attempts,
                                    written, reply.text, reply.problem, [],
                                    cost, reply.problem)
            summary = reply.text or summary

            failure = self._check_all(context, step, checks, where, attempt)
            if failure and before and _same_failure(failure, before):
                # The checkout already had changes, so the failure before could
                # have been the work's. Failing identically after an attempt
                # says otherwise: whatever the agent did, the checks did not
                # notice, and another paid attempt will not change that.
                return self._answer(
                    context, step, where, False, attempts, written, summary,
                    failure, [], cost,
                    "agent.implement: the checks failed the same way before "
                    "the change and after attempt %d, so they are not testing "
                    "it. Stopped rather than spending the rest of the budget. %s"
                    % (attempt, _first_line(failure)))
            if failure:
                continue

            findings = []
            if reviewing:
                findings, failure = self._review(
                    context, step, binary, where, task, acceptance, model,
                    attempt, effort)
                if failure:
                    continue
            verified = True
            break

        message = ("agent.implement: verified in %d attempt%s"
                   % (attempts, "" if attempts == 1 else "s")) if verified else (
                   "agent.implement: not verified after %d attempt%s"
                   % (attempts, "" if attempts == 1 else "s"))
        if verified and not checks:
            message += " - by the review only; no checks were run"
        return self._answer(context, step, where, verified, attempts, written,
                            summary, failure, findings, cost, message)

    def _check_all(self, context, step, checks, where, attempt, title="Check: "):
        """Run every check. Returns "" when they all passed, else what failed."""
        for command in checks:
            context.cancel.raise_if_set()
            result = run_process(context, step, _shell(command), where,
                                 "check-%d" % attempt)
            passed = result.outputs.get("exit_code") == 0
            # Both streams: a command that failed usually says why on stderr,
            # and an attempt told only what it printed to stdout would be
            # sent round again knowing nothing.
            said = "\n".join(part for part in
                             (str(result.outputs.get("stdout_tail") or ""),
                              str(result.outputs.get("stderr_tail") or ""))
                             if part.strip())[-FAILURE_CHARS:]
            self._say(context, step, "tool", title + command, detail=said,
                      status="done" if passed else "failed")
            if not passed:
                return "%s\n%s" % (command, said)
        return ""

    def _review(self, context, step, binary, where, task, acceptance, model,
                attempt, effort=""):
        """A second agent, reading only the result. Returns (findings, failure).

        It gets the reading tools and nothing else, so it cannot quietly fix
        what it is meant to be judging.

        Same model and same effort as the work it is judging: a reviewer asked
        to think less than the writer did is not a check on it.
        """
        context.cancel.raise_if_set()
        # On continuation an earlier edit attempt may already have changed
        # the checkout. Its old review is not evidence about this tree.
        prompt = _review_prompt(task, acceptance, where)
        prompt += "\n\n# Tree being reviewed\n" + _tree(where)
        reply = agent_run.run_claude(
            context, step, prompt, where,
            agent_run.READ_TOOLS, binary, model=model, effort=effort,
            add_dir=where, name="review-%d" % attempt)
        if not reply.ok:
            # A review that could not run is not an approval.
            return [], "the review could not run: %s" % reply.problem
        try:
            review = validate_review(extract_json(reply.text))
        except (ValueError, TypeError) as exc:
            return [], "the review did not come back in the required shape: %s" % exc

        issues = [one for one in review.get("issues") or []
                  if one.get("severity") in BLOCKING]
        self._say(context, step, "review",
                  "Review - %s risk, %d blocking" % (review.get("risk"),
                                                     len(issues)),
                  detail=str(review.get("summary") or ""),
                  status="failed" if issues else "done", body=review)
        if not issues:
            return review.get("issues") or [], ""
        return issues, "the review found:\n" + "\n".join(
            "- %s%s" % (one.get("description", ""),
                        _where(one)) for one in issues)

    # -- reporting ------------------------------------------------------------
    def _answer(self, context, step, where, verified, attempts, written,
                summary, failure, findings, cost, message):
        record = {"verified": verified, "attempts": attempts,
                  "files_changed": written, "summary": summary,
                  "failed_check": failure, "findings": findings}
        path = os.path.join(context.step_dir(step.id), "implement.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, ensure_ascii=False, default=str)
            handle.write("\n")

        outputs = dict(record, changed=len(written), tree=_tree(where),
                       report_path=context.relative(path),
                       # Whether any check stood behind "verified", or only
                       # the review did - see the checks field.
                       checked=bool(self._checks(step)))
        if cost:
            outputs["cost_usd"] = round(cost, 4)
        effort, asked = agent_run.effort_for(self, step)
        outputs["effort"] = effort
        return registry.PluginResult(
            "success" if verified else "failed", outputs=outputs,
            artifacts=[Artifact("json", context.relative(path), step.id,
                                name="implement",
                                bytes=os.path.getsize(path))],
            message=agent_run.with_effort(message, effort, asked))

    def _checks(self, step):
        return [one.strip() for one
                in str(self.setting(step.settings, "checks", "") or "").splitlines()
                if one.strip()]

    def _say(self, context, step, kind, title, detail="", status="done",
             body=None):
        one = {"kind": kind, "title": title, "detail": detail, "status": status}
        if body:
            one["body"] = body
        context.stage(step.id, one)


# -- the prompts --------------------------------------------------------------
def _prompt(task, acceptance, inputs, files, where, checks, failure,
            conflicted=()):
    """What to ask for. The first attempt and a retry differ in one section."""
    parts = ["You are making changes for an automated pipeline.", "",
             "# Task", task.strip(), "",
             "# Where",
             "The files are at %s. Edit them in place." % where,
             "Change only what the task asks for. Do not reformat, rename or "
             "tidy anything you were not asked about - this runs unattended "
             "and every change you make is applied.",
             ]
    if checks:
        parts += ["You cannot run commands. These will be run for you "
                  "afterwards, and every one of them must pass:", ""]
        parts += ["    " + one for one in checks]
    else:
        parts += ["You cannot run commands, and no automated checks will be "
                  "run: a reviewer reading your change is the only check, so "
                  "keep it small, obviously correct and easy to read."]
    if conflicted:
        parts += ["", "# An unfinished merge",
                  "Newer work from the base branch was merged under earlier "
                  "edits for this task, and these files still hold conflict "
                  "markers (<<<<<<<, =======, >>>>>>>):", ""]
        parts += ["    " + one for one in conflicted]
        parts += ["",
                  "The side marked Updated upstream is what the base branch "
                  "has now; the other side is the earlier edits for this "
                  "task. Resolve every marker by combining the two: keep what "
                  "upstream added and build this task's change on it - reuse "
                  "its models, numbering and helpers rather than keeping a "
                  "parallel version. Read the upstream side's neighbouring "
                  "code first. No marker may be left in any file."]
    if acceptance.strip():
        parts += ["", "# Done means", acceptance.strip()]
    if inputs:
        parts += ["", "# Results so far",
                  json.dumps(inputs, indent=2, ensure_ascii=False, default=str)]
    for entry in files:
        parts += ["", "# File: %s%s" % (entry["path"],
                                        " (truncated)" if entry["truncated"]
                                        else ""),
                  "```", entry["content"].rstrip(), "```"]
    if failure:
        parts += ["", "# Your last attempt did not hold",
                  "This is what went wrong. Read it before changing anything, "
                  "and fix the cause rather than the symptom - if a check is "
                  "failing for a reason the task did not anticipate, say so in "
                  "your answer rather than weakening the check.", "",
                  "```", failure.strip(), "```"]
    parts += ["", "# When you are done",
              "Say in a short paragraph what you changed and why. Plain prose, "
              "not JSON."]
    return "\n".join(parts)


def _review_prompt(task, acceptance, where):
    """The gate. Reading tools only, and the review schema as the answer."""
    parts = ["You are reviewing work somebody else just did, for an automated "
             "pipeline. You cannot change anything and should not try.", "",
             "# What they were asked to do", task.strip(), "",
             "# Where", "The result is at %s. Read what you need of it, "
             "including `git diff` output if a repository is there - but do "
             "not run anything." % where]
    if acceptance.strip():
        parts += ["", "# Done means", acceptance.strip()]
    parts += ["", "# What to judge",
              "Whether the change actually does what was asked, and whether it "
              "broke anything nearby. The automated checks have already passed, "
              "so do not re-litigate them: look for what a test would not catch "
              "- a requirement met in name only, a case the task implies and the "
              "code misses, a change reaching further than it was asked to.",
              "Report severity honestly. high and medium send the work back; "
              "low is a note that will be recorded and not acted on. Do not "
              "invent issues to look thorough: an empty issues list is the "
              "right answer for work that is right.",
              "", "# Answer with", """Reply with one JSON object and nothing \
else - no prose before it, no code fence around it:

{
  "summary": "one paragraph on what you found",
  "risk": "low" | "medium" | "high" | "unknown",
  "issues": [
    {"severity": "low" | "medium" | "high",
     "description": "what is wrong",
     "file": "path, or an empty string",
     "line": 12 or null}
  ],
  "recommendations": ["what to do about it"]
}

Every field is required. Use an empty list when you found nothing."""]
    return "\n".join(parts)


def _where(issue):
    """`` (main.py:42)`` for an issue that says where it is."""
    name = str(issue.get("file") or "").strip()
    if not name:
        return ""
    line = issue.get("line")
    return " (%s:%d)" % (name, line) if isinstance(line, int) else " (%s)" % name


# -- talking to git -----------------------------------------------------------
def _shell(command):
    """One check as an argv that runs it through a shell.

    A shell, because people write checks with pipes and `&&` in them, and
    ``run_process`` takes an argv so that the plugins that do not want one are
    not handed one by default.
    """
    if os.name == "nt":
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", command]
    return ["/bin/sh", "-c", command]


def _tree(where):
    """The git tree the working directory hashes to, or "" when it is not one.

    Computed against a **temporary index**, so the caller's own staging is left
    exactly as it was. Somebody with a half-staged change should not find it
    staged for them because a step wanted a hash.
    """
    with tempfile.TemporaryDirectory(prefix="qavector-tree-") as scratch:
        environment = dict(os.environ, GIT_INDEX_FILE=os.path.join(scratch,
                                                                   "index"))
        # From HEAD first so an unchanged file is already there; an empty
        # repository has no HEAD and simply starts from nothing.
        _git(where, ["read-tree", "HEAD"], environment)
        if _git(where, ["add", "-A"], environment) is None:
            return ""
        return _git(where, ["write-tree"], environment) or ""


def _earlier_work(context, step, where):
    """``(run id, outputs, returned)`` of this step's last run if the checkout
    is still exactly what it left, else ``(None, None, [])``.

    The tree hash is the whole test. Anything else - somebody editing the
    checkout since, a different branch, a run that left nothing - and the
    earlier work is not what is on disk, so it is not continued.

    ``returned`` is what the steps after this one, in that same run, found
    blocking about the work: ``[(step id, issue)]``. Same run only, because
    only there were they judging this tree.
    """
    now = _tree(where)
    if not now:
        return None, None, []
    later = _after(context, step)
    earlier_of = getattr(context, "earlier_of", None)
    source, was = earlier_of(step.id) if earlier_of else (None, None)
    outputs = dict(getattr(was, "outputs", None) or {})
    if outputs.get("tree") == now:
        steps = {name: getattr(entry[1], "outputs", None) or {}
                 for name, entry in (getattr(context, "earlier", None) or {}).items()
                 if entry[0] == source and name in later}
        return source, outputs, _blocking(steps)
    # The run just before may not have reached this step - stopped at a gate,
    # say - while the one before that did the work. So the other runs of this
    # cycle are looked through too, newest first, for the same tree.
    source, outputs, record = _earlier_in_runs(context, step, now)
    if outputs is None:
        return None, None, []
    steps = {name: (entry or {}).get("outputs") or {}
             for name, entry in ((record or {}).get("steps") or {}).items()
             if name in later}
    return source, outputs, _blocking(steps)


def _after(context, step):
    """Every step that waits on this one, however far forward."""
    cycle = getattr(context, "cycle", None)
    if cycle is None:
        return set()
    from cycle import model
    return model.downstream(cycle, {step.id}) - {step.id}


def _blocking(outputs_by_step):
    """``[(step id, issue)]`` for every blocking issue a later step reported."""
    found = []
    for name in sorted(outputs_by_step):
        for issue in outputs_by_step[name].get("issues") or []:
            if isinstance(issue, dict) and issue.get("severity") in BLOCKING:
                found.append((name, issue))
    return found


def _findings_text(returned):
    return "\n".join("- [%s] %s%s" % (name, issue.get("description", ""), _where(issue))
                     for name, issue in returned)


def _earlier_in_runs(context, step, tree):
    """The newest other run of this cycle whose ``step`` left ``tree``:
    ``(run id, outputs, record)``, or three Nones."""
    runs = os.path.dirname(os.path.abspath(context.workspace))
    cycle_id = getattr(context.run, "cycle_id", "")
    try:
        names = sorted(os.listdir(runs), reverse=True)
    except OSError:
        return None, None, None
    for name in names:
        path = os.path.join(runs, name, "metadata.json")
        if name == os.path.basename(context.workspace) or not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            continue
        if record.get("cycle_id") != cycle_id:
            continue
        outputs = ((record.get("steps") or {}).get(step.id) or {}).get("outputs") or {}
        if outputs.get("tree") == tree:
            return record.get("id") or name, dict(outputs), record
    return None, None, None


def _unchanged(where):
    """True when the working directory is exactly its HEAD commit.

    Only then is "before the change" what it says. False for a checkout with
    edits in it - an earlier attempt's, say, on a resumed run - and for a
    directory git cannot vouch for, where a failing check might be the work's.
    """
    head = _git(where, ["rev-parse", "HEAD^{tree}"], None)
    return bool(head) and head == _tree(where)


def _edited(where):
    """True when this is a git checkout whose files differ from HEAD."""
    head = _git(where, ["rev-parse", "HEAD^{tree}"], None)
    tree = _tree(where) if head else ""
    return bool(head and tree) and head != tree


def _conflicted(where):
    """Files git has as unmerged that still hold a conflict marker."""
    listed = _git(where, ["diff", "--name-only", "--diff-filter=U"], None) or ""
    found = []
    for name in sorted(set(listed.splitlines())):
        try:
            with open(os.path.join(where, name), encoding="utf-8",
                      errors="replace") as handle:
                if any(line.startswith("<<<<<<< ") for line in handle):
                    found.append(name)
        except OSError:
            continue
    return found


def _same_failure(one, other):
    """Two check failures that differ only in numbers - timings, counts, ids."""
    return re.sub(r"\d+", "#", one).strip() == re.sub(r"\d+", "#", other).strip()


def _first_line(failure):
    """The command that failed and the last thing it said, for a message."""
    lines = [line for line in failure.splitlines() if line.strip()]
    if not lines:
        return ""
    return lines[0] if len(lines) == 1 else "%s: %s" % (lines[0], lines[-1].strip())


def _git(where, arguments, environment):
    """One git query. ``None`` when git refused or is not there."""
    try:
        done = subprocess.run(["git"] + arguments, cwd=where, env=environment,
                              capture_output=True, text=True,
                              timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None
