"""Universal agent node: role/task contracts are independent of review workflows."""

import json
import os
import threading

from cycle import registry
from cycle.registry import CyclePlugin, PluginMetadata, field, output


class ProfileAgent(CyclePlugin):
    metadata = PluginMetadata(
        id="agent", name="Agent", category=registry.AGENT, recoverable=True,
        summary="Run a task using a named agent profile and optional output schema.",
        permissions=("process.spawn", "filesystem.read", "filesystem.write", "network"),
        inputs=(
            field("agent_profile", "Agent profile", "choice", required=True,
                  hint="A role declared in this cycle's agent_profiles mapping."),
            field("task", "Task", "multiline", required=True),
            field("directory", "Working directory", "dir",
                  hint="Relative to the run workspace; blank uses that workspace."),
            field("inputs", "Structured context", "env"),
            field("output_schema", "Output JSON Schema", "env",
                  hint="Optional JSON Schema object. Without one, output is text."),
        ),
        outputs=(output("summary", "text"), output("output", "any"),
                 output("agent_result", "object"), output("files_changed", "list"),
                 output("cost_usd", "number")),
        accepts=(registry.accepts("text", "Clarification", when="Before the agent call starts"),))

    def problems(self, settings):
        from qavector_sdk import ContractError, validate_schema
        settings = settings or {}
        # Whole references are checked again after the executor resolves them.
        literal = {k: v for k, v in settings.items()
                   if not (k in ("inputs", "output_schema") and isinstance(v, str) and "${" in v)}
        found = super().problems(literal)
        for key in ("agent_profile", "task", "directory"):
            if key in settings and not isinstance(settings[key], str):
                found.append("%s must be text." % key)
        reference = settings.get("agent_profile")
        if isinstance(reference, str) and "${" in reference:
            found.append("agent_profile must be a fixed profile ID.")
        schema = literal.get("output_schema")
        if schema is not None:
            try:
                validate_schema(schema)
            except ContractError as exc:
                found.append("output_schema: %s" % exc)
        return found

    def execute(self, context, step):
        from cycle.agent_profiles import ProfileError, selected
        from cycle.plugins import agent_cli, agent_run
        problems = self.problems(step.settings)
        if problems:
            return registry.failed("; ".join(problems))
        try:
            profile = selected(context.cycle, step.settings["agent_profile"])
        except ProfileError as exc:
            return registry.failed(str(exc))
        directory = os.path.realpath(os.path.join(context.workspace,
            os.path.expanduser(step.settings.get("directory") or "")))
        if not os.path.isdir(directory):
            return registry.failed("Agent working directory does not exist: " + directory)
        prompt = step.settings["task"]
        if step.settings.get("inputs"):
            prompt += "\n\n# Structured context\n" + json.dumps(
                step.settings["inputs"], ensure_ascii=False, indent=2, allow_nan=False)
        schema = step.settings.get("output_schema")
        if schema is not None:
            prompt += "\n\nReturn JSON matching this schema:\n" + json.dumps(
                schema, ensure_ascii=False, sort_keys=True, allow_nan=False)

        token = context.cancel.child()
        scoped = context.for_step(token)
        seconds = profile["limits"].get("wall_time_seconds")
        expired = threading.Event()

        def expire():
            expired.set()
            token.set("agent profile wall time exceeded")

        timer = threading.Timer(seconds, expire) if seconds is not None else None
        if timer:
            timer.daemon = True
            timer.start()
        try:
            if token.is_set():
                return registry.failed("cancelled before starting the agent", started=False)
            options = profile["backend_options"]
            status = agent_cli.auth_status(options["binary"],
                timeout=min(seconds, agent_cli.STATUS_TIMEOUT) if seconds is not None else agent_cli.STATUS_TIMEOUT)
            if status["problem"]:
                return registry.failed(status["problem"], started=False)
            # Resolve once, then include this exact executable in the operation
            # identity. The declaration itself is never changed.
            options["binary"] = status["binary"]
            reply = agent_run.run_claude(scoped, step, prompt, directory,
                options["tools"], status["binary"], model=profile["model"] or "",
                mode=options.get("permission_mode", ""), effort=options.get("effort", ""),
                max_turns=profile["limits"].get("turns"),
                add_dir=options.get("additional_directory", ""),
                disallowed=options.get("disallowed_tools", ()), clarify=True, last=True,
                agent_profile=profile, output_schema=schema)
            result = reply.result
            if not reply.ok:
                result.status = "timeout" if expired.is_set() else "failed"
                result.message = reply.problem
                result.outputs["agent_result"] = reply.agent_result
                return result
            result.message = reply.agent_result["summary"]
            result.outputs.update(summary=reply.agent_result["summary"],
                output=reply.agent_result["output"], agent_result=reply.agent_result,
                files_changed=reply.written)
            if reply.cost is not None:
                result.outputs["cost_usd"] = reply.cost
            return result
        finally:
            if timer:
                timer.cancel()
