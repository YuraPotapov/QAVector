"""Offline Claude stand-in shared by process and native Runtime acceptance tests."""

import json
from pathlib import Path

from qavector_agent_claude.backend import BACKEND_ID, VERSION, config_schema


def fake_cli(path, interpreter, *, authenticated=True, answer=None, body=None):
    if answer is None:
        answer = {"type": "result", "result": '{"verdict":"approved"}', "num_turns": 1}
    script = "#!" + interpreter + "\nimport json, sys\n"
    script += "if sys.argv[1:4] == ['auth', 'status', '--json']:\n"
    script += "    print(json.dumps({'loggedIn': %r})); sys.exit(0)\n" % authenticated
    script += body if body is not None else (
        "print(json.dumps({'type': 'system', 'subtype': 'init'}))\n"
        "print(json.dumps(%r))\n" % answer)
    path.write_text(script)
    path.chmod(0o755)
    return str(path)


def configure(initialization, params, binary):
    root = Path(__file__).parents[1] / "examples/plugin_contracts"
    initialization["identity"].update(plugin_id=BACKEND_ID, version=VERSION, contribution={
        "kind": "agent_backend", "id": BACKEND_ID,
        "entrypoint": "qavector_agent_claude:ClaudeCliBackend", "config_schema": "schemas/config.json"})
    initialization["schemas"] = {"config": config_schema(), "input": True, "output": True}
    # A stopped local process cannot prove a remote paid request was cancelled.
    initialization["execution_scope"] = "remote_job"
    profile = json.loads((root / "agent-profile.json").read_text())
    profile.update(backend_ref=BACKEND_ID, limits={}, instructions="Review the task.",
                   backend_options={"binary": binary, "tools": ["Read", "Glob", "Grep"]})
    request = json.loads((root / "agent-request.json").read_text())
    request.update(workspace_ref=None, context_refs=[], artifact_refs=[], limits={},
                   instructions="", output_schema={"type": "object", "properties": {
                       "verdict": {"enum": ["approved"]}}, "required": ["verdict"],
                       "additionalProperties": False})
    params.update(profile=profile, request=request, requirements={"schema_version": "1.0.0"})
