"""Agent reviews, through whichever backend the step names.

Three, and the third exists because of authentication. **CrewAI** and
**AutoGen** run in an isolated worker interpreter that only they import, so
registry discovery, the CLI and the GUI never need either framework installed.
Both resolve credentials themselves and look for an API key in the environment,
which means somebody first makes a key in a web console and works out where to
export it - a developer's errand standing between opening the application and
having an agent read a failing test.

**claude_cli** removes that. It runs the Claude Code CLI, which signs in
through a browser (`claude auth login`) against a Claude subscription or a
Console account and keeps the credentials to itself. Nothing here sees a token,
stores one or passes one, there is no worker interpreter and no framework to
install - so it is the backend that works the moment somebody who already uses
Claude Code opens QAVector.

All three answer with the same review document, validated the same way, so a
cycle can change backend without anything downstream noticing.
"""

import json
import os
import re
import tempfile

from cycle import registry
from cycle.plugins import agent_cli, agent_run
from cycle.operations import Operation, result_document, result_from_document
from cycle.plugins._process import run_process
from cycle.plugins.agent_worker import COMPLEXITY_GUIDE, extract_json, validate_review
from cycle.registry import (CyclePlugin, PluginMetadata, action, field,
                            output)
from domain.cycle import Artifact

FILE_BYTES = 128 * 1024
TOTAL_FILE_BYTES = 512 * 1024
RESULT_BYTES = 2 * 1024 * 1024

#: Backends that run the worker interpreter, and so need one configured.
WORKER_FRAMEWORKS = ("crewai", "autogen")

#: What the CLI backend is allowed to do. A review reads; it does not edit, run
#: commands or reach the network on its own account. Naming the tools is what
#: keeps a step that was asked to "review this" from being able to change it.
CLI_TOOLS = ("Read", "Glob", "Grep")


class AgentReview(CyclePlugin):
    metadata = PluginMetadata(
        recoverable=True,
        id="agent.review", name="Agent Review", category=registry.AGENT,
        summary="Review code and step results. The default backend needs no API key.",
        permissions=("process.spawn", "filesystem.read", "filesystem.write", "network"),
        inputs=(
            field("framework", "Framework", "choice", default="claude_cli",
                  options=("claude_cli", "crewai", "autogen"),
                  hint="claude_cli runs the Claude Code CLI and needs no API "
                       "key - it uses the browser sign-in that CLI already "
                       "has. The other two need a key and a Python "
                       "environment with the framework installed."),
            field("claude", "Claude Code binary", "file",
                  hint="Only for claude_cli. Blank finds `claude` on PATH."),
            field("python", "Agent Python", "file",
                  hint="Only for crewai and autogen: an interpreter in an "
                       "environment with the chosen framework installed."),
            field("model", "Model", required=True,
                  hint="Provider/model for CrewAI; model name for the AutoGen "
                       "client; an alias such as opus or sonnet for claude_cli."),
            field("effort", "Effort", "choice", default="",
                  options=agent_run.EFFORT_LEVELS,
                  hint="Only for claude_cli: how hard the model is asked to "
                       "think. Blank leaves it to whatever the Claude Code CLI "
                       "is configured to use."),
            field("model_client", "AutoGen model client",
                  hint="AutoGen component class, required for AutoGen. See docs/cycle-agents.md."),
            field("model_options", "Model options", "env",
                  hint="Provider options as a mapping, e.g. base_url or temperature."),
            field("api_key_env", "API key environment variable",
                  hint="Only for crewai and autogen. Name of an inherited "
                       "environment variable; its value is not saved here."),
            field("task", "Review task", "multiline", required=True),
            field("repository", "Repository", "dir",
                  hint="Optional checkout path, e.g. ${steps.checkout.outputs.workspace}."),
            field("inputs", "Structured inputs", "env",
                  hint="A mapping of results or context for the review."),
            field("files", "Logs and artifacts", "args",
                  hint="List of text files within the run workspace to include in the review."),
            field("max_iterations", "Maximum tool iterations", "number",
                  hint="Empty means 12 for a worker framework and no limit "
                       "for claude_cli, where the step's timeout bounds it."),
            field("assess", "Assess", "choice", default="",
                  options=("complexity",),
                  hint="complexity also asks how hard the task is, on the "
                       "effort scale (low to max), and publishes it as "
                       "`complexity` - so a later agent step can take "
                       "effort: ${steps.<id>.outputs.complexity}."),
        ),
        outputs=(
            output("summary", "string"), output("issues", "array"),
            output("issue_count", "number",
                   "How many issues were found. Here because `${...}` cannot "
                   "measure a list, and a gate that asks 'did the review find "
                   "anything' needs a number to compare."),
            output("blocking_count", "number",
                   "How many of them are high or medium. The count a gate "
                   "usually wants: a low-severity note should not stop a run."),
            output("recommendations", "array"), output("risk", "string"),
            output("complexity", "string",
                   "How hard the task is, as an effort level - only when the "
                   "step sets assess: complexity."),
            output("effort", "string",
                   "The effort level claude_cli ran at; empty for the CLI's "
                   "own default or a worker framework."),
            output("report_path", "string", "Structured review JSON in the step directory."),
            output("cost_usd", "number", "What the review cost, when the backend says."),
        ),
        # Setting this plugin up belongs to this plugin. Signing in is not a
        # property of the application - it is a property of the backend this
        # step named - so it is declared here and a front-end offers whatever
        # it finds, the same way it builds the form from the fields above.
        actions=(
            action("sign_in_status", "Sign-in", "status",
                   hint="Whether the chosen backend can run. claude_cli needs "
                        "no API key - it uses the Claude Code CLI's own "
                        "browser sign-in."),
            action("sign_in", "Sign in...", "command",
                   hint="Opens the browser sign-in for the Claude Code CLI. "
                        "Nothing is stored here; the CLI keeps its own "
                        "credentials."),
        ),
    )

    def run_action(self, key, settings):
        """Answer the two actions above. Never raises - the report IS the answer."""
        settings = settings or {}
        framework = str(settings.get("framework") or "claude_cli")
        binary = str(settings.get("claude") or "")

        if framework != "claude_cli":
            # Said plainly rather than by offering a button that would not
            # help: these two backends authenticate through an API key in the
            # environment, which is the thing claude_cli exists to avoid.
            detail = ("%s reads its credentials from the environment variable "
                      "named in \"API key environment variable\". Choose "
                      "claude_cli to sign in through a browser instead."
                      % framework)
            if key == "sign_in_status":
                name = str(settings.get("api_key_env") or "")
                ready = bool(name and os.environ.get(name))
                return {"ok": ready,
                        "summary": ("%s is set" % name if ready else
                                    "%s is not set" % (name or "No API key "
                                                       "environment variable")),
                        "detail": detail, "argv": []}
            return {"ok": False, "summary": "Not available for %s" % framework,
                    "detail": detail, "argv": []}

        if key == "sign_in_status":
            status = agent_cli.auth_status(binary)
            return {"ok": bool(status["logged_in"]),
                    "summary": agent_cli.describe(status),
                    "detail": status["problem"], "argv": []}
        if key == "sign_in":
            status = agent_cli.auth_status(binary)
            if not status["installed"]:
                return {"ok": False, "summary": "Claude Code is not installed",
                        "detail": status["problem"] + " Install it from "
                                  "claude.com/claude-code, then try again.",
                        "argv": []}
            return {"ok": True, "summary": "Sign in through the browser",
                    "detail": "The Claude Code CLI opens your browser and "
                              "keeps the credentials itself. Nothing is stored "
                              "in this application.",
                    "argv": agent_cli.login_argv(binary)}
        return super().run_action(key, settings)

    def problems(self, settings):
        settings = settings or {}
        literal = dict(settings)
        for key in ("framework", "effort", "max_iterations", "inputs", "model_options"):
            if isinstance(literal.get(key), str) and "${" in literal[key]:
                literal.pop(key)
        found = super().problems(literal)
        for key in ("python", "model", "task", "repository", "model_client", "api_key_env"):
            if key in settings and not isinstance(settings[key], str):
                found.append("%s must be text." % key)
        framework = settings.get("framework")
        if framework == "autogen" and not str(settings.get("model_client") or "").strip():
            found.append("AutoGen requires a model_client component class.")
        # `python` is the worker's interpreter, and the CLI backend has no
        # worker - so it is required for the two that do rather than for all
        # three, which is why it is not simply a required field.
        if framework in WORKER_FRAMEWORKS and not str(settings.get("python") or "").strip():
            found.append("%s needs an interpreter in Agent Python." % framework)
        if framework == "claude_cli":
            for key in ("python", "api_key_env", "model_client"):
                if str(settings.get(key) or "").strip():
                    found.append("%s is not used by claude_cli; it signs in "
                                 "through the Claude Code CLI." % key)
        # The other way round, and said rather than ignored: an effort level
        # that quietly does nothing is worse than one that is refused, because
        # the cycle file would go on claiming the review thinks harder than it
        # does.
        elif framework in WORKER_FRAMEWORKS and str(settings.get("effort") or "").strip():
            found.append("effort is only used by claude_cli; %s has no "
                         "equivalent." % framework)
        files = settings.get("files", [])
        if not (isinstance(files, str) and "${" in files):
            if not isinstance(files, list) or any(not isinstance(one, str) for one in files):
                found.append("Files must be a list of paths.")
        count = settings.get("max_iterations")
        if count not in (None, "") and not (isinstance(count, str) and "${" in count):
            try:
                if isinstance(count, bool) or not 1 <= int(count) <= 100 or float(count) != int(count):
                    raise ValueError()
            except (TypeError, ValueError, OverflowError):
                found.append("Maximum tool iterations must be an integer from 1 to 100.")
        options = settings.get("model_options") or {}
        if isinstance(options, dict) and "api_key" in options:
            found.append("Use api_key_env instead of storing an api_key in model_options.")
        key = settings.get("api_key_env")
        if isinstance(key, str) and key and "${" not in key and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            found.append("API key environment variable must be a variable name.")
        return found

    def execute(self, context, step):
        problems = self.problems(step.settings)
        if problems:
            return registry.failed("; ".join(problems))
        if context.cancel.is_set():
            return registry.failed("cancelled before starting the agent")
        if self.setting(step.settings, "framework") == "claude_cli":
            return self._claude_cli(context, step)
        key = self.setting(step.settings, "api_key_env", "")
        if key and not context.environment.get(key):
            return registry.failed("API key environment variable %s is not set." % key)
        repository = self.setting(step.settings, "repository", "")
        if repository:
            repository = os.path.realpath(os.path.join(context.workspace, os.path.expanduser(repository)))
            if not os.path.isdir(repository):
                return registry.failed("Agent repository does not exist: %s" % repository)
        try:
            files = read_files(context, self.setting(step.settings, "files", []))
        except (OSError, ValueError) as exc:
            return registry.failed("Cannot prepare agent files: %s" % exc)
        request = {
            "framework": self.setting(step.settings, "framework"),
            "model": self.setting(step.settings, "model"),
            "model_client": self.setting(step.settings, "model_client", ""),
            "model_options": self.setting(step.settings, "model_options", {}),
            "api_key_env": key, "task": self.setting(step.settings, "task"),
            "repository": repository,
            "inputs": self.setting(step.settings, "inputs", {}), "files": files,
            "max_iterations": int(self.setting(step.settings, "max_iterations", 12)),
            "assess": self.setting(step.settings, "assess", ""),
        }
        interpreter = os.path.expanduser(self.setting(step.settings, "python"))
        # Explicit relative interpreter paths are relative to the run workspace;
        # a bare executable name is resolved through PATH by Popen.
        if not os.path.isabs(interpreter) and os.path.dirname(interpreter):
            interpreter = os.path.abspath(os.path.join(context.workspace, interpreter))
        directory = context.step_dir(step.id)
        operation = Operation(context, step, "agent-worker",
                              {"request": request, "interpreter": interpreter})
        cached = operation.cached()
        if cached is None:
            request_path = os.path.join(operation.directory, "request.json")
            result_path = os.path.join(operation.directory, "result.json")
            operation.store.write(request_path, request)
            worker = os.path.join(os.path.dirname(__file__), "agent_worker.py")
            operation.begin()
            try:
                result = run_process(
                    operation.process_context(), step,
                    [interpreter, "-I", "-u", "-X", "utf8", worker, request_path, result_path],
                    directory, "agent", env={"OTEL_SDK_DISABLED": "true"})
            finally:
                os.unlink(request_path)
            raw = ""
            if result.ok or os.path.isfile(result_path):
                try:
                    with open(result_path, "rb") as handle:
                        raw = handle.read(RESULT_BYTES + 1).decode("utf-8")
                except (OSError, UnicodeError) as exc:
                    result.status, result.message = "failed", "Cannot read agent response: %s" % exc
            cached = {"result": result_document(result), "raw": raw}
            if raw or result.outputs.get("started") is False:
                operation.complete(cached, bool(raw))
        result = result_from_document(cached["result"])
        if not result.ok:
            return result
        try:
            raw = cached["raw"]
            if len(raw.encode("utf-8")) > RESULT_BYTES:
                raise ValueError("review exceeds the 2 MiB limit")
            review = validate_review(json.loads(raw),
                                     request["assess"] == "complexity")
        except (OSError, ValueError, TypeError) as exc:
            result.status = "failed"
            result.message = "Agent did not return a valid review: %s" % exc
            return result
        report_path = os.path.join(directory, "review.json")
        operation.store.write(report_path, review)
        result.outputs = dict(review, report_path=context.relative(report_path),
                              **counts(review))
        result.artifacts.append(Artifact("json", context.relative(report_path), step.id,
                                         name="review", bytes=os.path.getsize(report_path)))
        result.message = "%s review: %s" % (request["framework"], _said(review))
        return result


def _claude_cli_method(self, context, step):
    """Run the review through the Claude Code CLI. No API key anywhere.

    The CLI is already signed in or it is not; either way nothing here handles
    a credential. When it is not, the step says so and names the one command
    that fixes it rather than failing somewhere inside the binary with a
    message written for a terminal.
    """
    binary = self.setting(step.settings, "claude", "")
    status = agent_cli.auth_status(binary)
    if status["problem"]:
        return registry.failed(status["problem"])

    repository = self.setting(step.settings, "repository", "")
    if repository:
        repository = os.path.realpath(
            os.path.join(context.workspace, os.path.expanduser(repository)))
        if not os.path.isdir(repository):
            return registry.failed("Agent repository does not exist: %s" % repository)
    try:
        files = read_files(context, self.setting(step.settings, "files", []))
    except (OSError, ValueError) as exc:
        return registry.failed("Cannot prepare agent files: %s" % exc)

    assess = self.setting(step.settings, "assess", "") == "complexity"
    prompt = _claude_cli_prompt(
        str(self.setting(step.settings, "task") or ""),
        self.setting(step.settings, "inputs", {}), files, repository,
        complexity=assess)

    directory = context.step_dir(step.id)
    # The prompt is on the command line and can be long, so it is kept out of
    # the log the runner writes; what was asked is in the review beside it.
    reply = agent_run.run_claude(
        context, step, prompt, repository or context.workspace,
        # Read-only: a review reads, and a step asked to review something must
        # not be able to change it.
        CLI_TOOLS, status["binary"],
        model=self.setting(step.settings, "model"),
        effort=self.setting(step.settings, "effort", ""),
        max_turns=self.setting(step.settings, "max_iterations"),
        add_dir=repository)
    result = reply.result
    if not reply.ok:
        result.status = "failed"
        result.message = reply.problem
        return result

    try:
        review = validate_review(extract_json(reply.text), assess)
    except (ValueError, TypeError) as exc:
        result.status = "failed"
        result.message = "Agent did not return a valid review: %s" % exc
        return result

    report_path = os.path.join(directory, "review.json")
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(review, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    result.outputs = dict(review, report_path=context.relative(report_path),
                          **counts(review))
    if reply.cost is not None:
        result.outputs["cost_usd"] = reply.cost
    result.artifacts.append(Artifact("json", context.relative(report_path), step.id,
                                     name="review", bytes=os.path.getsize(report_path)))
    effort = self.setting(step.settings, "effort", "") or ""
    result.outputs["effort"] = effort
    result.message = agent_run.with_effort("claude_cli review: %s" % _said(review),
                                           effort)
    return result


#: Severities that should stop a run. The same two ``agent.implement`` treats
#: as blocking - a low-severity note is worth reading and not worth refusing a
#: commit over, and the two steps must not disagree about which is which.
BLOCKING = ("high", "medium")


def counts(review):
    """The two numbers a gate needs, which the list itself cannot give it.

    ``${...}`` walks mappings and cannot measure a list, so "did the review
    find anything" is unaskable from a cycle file without these. Both are here
    rather than only the total because the useful question is almost always
    the second one: a low-severity note should not stop a run.
    """
    issues = review.get("issues") or []
    return {"issue_count": len(issues),
            "blocking_count": sum(1 for one in issues
                                  if isinstance(one, dict)
                                  and one.get("severity") in BLOCKING)}


def _said(review):
    """The one line a review step reports: risk, issues, and a judged level."""
    line = "%s risk, %d issue(s)" % (review["risk"], len(review["issues"]))
    if review.get("complexity"):
        line += ", %s complexity" % review["complexity"]
    return line


def _claude_cli_prompt(task, inputs, files, repository, complexity=False):
    """The whole request, as one prompt.

    Written out rather than handed over as attachments because the CLI takes a
    prompt and nothing else. The schema is spelled out at the end because the
    answer has to survive :func:`validate_review` - and a model told exactly
    what shape to answer in is far likelier to produce it than one asked to
    "return JSON".
    """
    parts = ["You are reviewing work for an automated pipeline.", "",
             "# Task", task.strip()]
    if repository:
        parts += ["", "# Repository",
                  "The checkout is at %s. Read what you need from it." % repository]
    if inputs:
        parts += ["", "# Results so far",
                  json.dumps(inputs, indent=2, ensure_ascii=False, default=str)]
    for entry in files:
        parts += ["", "# File: %s%s" % (entry["path"],
                                        " (truncated)" if entry["truncated"] else ""),
                  "```", entry["content"].rstrip(), "```"]
    parts += ["", "# Answer with", """Reply with one JSON object and nothing \
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
  "recommendations": ["what to do about it"]%s
}

Every field is required. Use an empty list when you found nothing.""" % (
        ',\n  "complexity": "low" | "medium" | "high" | "xhigh" | "max"'
        if complexity else "")]
    if complexity:
        parts += ["", COMPLEXITY_GUIDE]
    return "\n".join(parts)


AgentReview._claude_cli = _claude_cli_method


def read_files(context, paths):
    root = os.path.realpath(context.workspace)
    files, remaining = [], TOTAL_FILE_BYTES
    for name in paths:
        path = os.path.realpath(os.path.join(root, name))
        if os.path.commonpath([root, path]) != root:
            raise ValueError("file is outside the run workspace: %s" % name)
        if not os.path.isfile(path):
            raise ValueError("not a regular file: %s" % name)
        limit = min(FILE_BYTES, remaining)
        if limit <= 0:
            raise ValueError("combined files exceed 512 KiB")
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
        truncated = len(data) > limit
        data = data[:limit]
        remaining -= len(data)
        if b"\0" in data:
            raise ValueError("file is not text: %s" % name)
        files.append({"path": context.relative(path),
                      "content": data.decode("utf-8", "replace"), "truncated": truncated})
    return files
