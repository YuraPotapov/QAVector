"""Core commands behind the GUI's restrictions: check, preview and exec.

``--restrictions-check`` says what this computer can enforce. ``--restrictions-
preview`` reads one request on stdin and answers what a program would actually
get - the resolved restrictions, the folders added for its tools, and every
reason it could not start - without starting anything. ``--restricted-exec``
is what the GUI starts in place of a restricted service: it resolves the same
request, builds the sandbox and replaces itself with it, so the process the
GUI watches is the service, its output streams straight through, and Stop
reaches it the way it always did. The GUI never builds a sandbox command.

A request is ``{"project_folder", "layers": [[name, restrictions], ...],
"working_directory"}`` - the command comes from the request (preview) or after
``--`` (exec).
"""

import json
import os
import sys

from ._json import parse_document
from .errors import RuntimePolicyError
from . import restrictions, toolfolders

PREVIEW, CHECK, EXEC = "--restrictions-preview", "--restrictions-check", "--restricted-exec"
FLAGS = (PREVIEW, CHECK, EXEC)


def _launcher():
    if sys.platform != "linux":
        return None
    from .backends.linux.restricted import launcher
    return launcher()


def capabilities():
    launcher = _launcher()
    if launcher is None:
        return {"platform": sys.platform, "available": False,
                "reason": "Restrictions are not available on this operating system yet.",
                "launcher": {}, "limits": {name: {"available": False, "reason": "Not available on this operating system yet."}
                                           for name in ("memory_mb", "processes", "cpu_cores")}}
    return launcher.capabilities()


def _request(raw):
    if not isinstance(raw, dict) or set(raw) - {"project_folder", "layers", "working_directory", "command"}:
        raise RuntimePolicyError("invalid_request", "Expected project_folder, layers, working_directory and command.")
    layers = raw.get("layers") or []
    if (not isinstance(layers, list) or len(layers) > 4
            or any(not isinstance(one, list) or len(one) != 2 or not isinstance(one[0], str) for one in layers)):
        raise RuntimePolicyError("invalid_request", "Layers are [name, restrictions] pairs.")
    command = raw.get("command") or []
    if not isinstance(command, list) or any(not isinstance(one, str) for one in command):
        raise RuntimePolicyError("invalid_request", "A command is a list of strings.")
    folder = raw.get("project_folder") or ""
    if not isinstance(folder, str):
        raise RuntimePolicyError("invalid_request", "The project folder is a path.")
    return folder, [tuple(one) for one in layers], raw.get("working_directory") or "", command


def _plan(folder, layers, command, cwd, env):
    """``(effective, argv, tool folders)`` for one program, or raise."""
    effective = restrictions.resolve(layers, folder)
    if restrictions.unrestricted(effective):
        return effective, list(command), []
    visible = []
    if effective["files"] in ("read_write", "read_only") and effective["project_folder"]:
        visible.append(effective["project_folder"])
    visible += [one["path"] for one in effective["folders"]]
    argv, tools = toolfolders.for_command(command, env, visible=visible) if command else (list(command), [])
    if effective["files"] in ("all", "none"):
        tools = []
    return effective, argv, tools


def preview(raw, env=None):
    env = dict(os.environ if env is None else env)
    result = {"ok": True, "problems": [], "capabilities": capabilities()}
    try:
        folder, layers, cwd, command = _request(raw)
        effective, argv, tools = _plan(folder, layers, command, cwd, env)
        result.update(effective=effective, summary=restrictions.summary(effective), tool_folders=tools)
        if not restrictions.unrestricted(effective):
            launcher = _launcher()
            if launcher is None:
                raise RuntimePolicyError("restrictions_unavailable", result["capabilities"]["reason"])
            prepared = launcher.prepare(effective, argv or ["/bin/true"], cwd or effective["project_folder"] or "/", env, tools)
            prepared.close()
    except RuntimePolicyError as exc:
        result["ok"] = False
        result["problems"].append(exc.to_document())
    return result


def execute(arguments):
    """Replace this process with the restricted program, or say why not."""
    try:
        spec = arguments[0].split("=", 1)[1] if "=" in arguments[0] else ""
        if "--" not in arguments or not spec:
            raise RuntimePolicyError("invalid_request", "Usage: --restricted-exec=REQUEST_JSON -- COMMAND [ARGS].")
        command = arguments[arguments.index("--") + 1:]
        if not command:
            raise RuntimePolicyError("invalid_request", "No command to run.")
        folder, layers, cwd, _ = _request(parse_document(spec.encode("utf-8")))
        cwd = cwd or os.getcwd()
        env = dict(os.environ)
        effective, argv, tools = _plan(folder, layers, command, cwd, env)
        if restrictions.unrestricted(effective):
            prepared_argv, prepared_cwd, prepared_env = argv, cwd, env
        else:
            launcher = _launcher()
            if launcher is None:
                raise RuntimePolicyError("restrictions_unavailable", capabilities()["reason"])
            prepared = launcher.prepare(effective, argv, cwd, env, tools)
            prepared_argv, prepared_cwd, prepared_env = prepared.argv, prepared.cwd, prepared.env
        os.chdir(prepared_cwd)
        sys.stdout.flush()
        sys.stderr.flush()
        os.execvpe(prepared_argv[0], prepared_argv, prepared_env)
    except RuntimePolicyError as exc:
        sys.stderr.write("QAVector restrictions: %s\n" % exc)
        return 126
    except OSError as exc:
        sys.stderr.write("QAVector restrictions: cannot start %s (%s)\n" % (arguments[-1] if arguments else "", exc.strerror))
        return 127


def main(arguments):
    flag = arguments[0].split("=", 1)[0] if arguments else ""
    if flag == EXEC:
        return execute(arguments)
    if flag == CHECK and len(arguments) == 1:
        payload = {"ok": True, "capabilities": capabilities()}
    elif flag == PREVIEW and len(arguments) == 1:
        try:
            payload = preview(parse_document(sys.stdin.buffer.read(1024 * 1024 + 1)))
        except RuntimePolicyError as exc:
            payload = {"ok": False, "problems": [exc.to_document()]}
    else:
        payload = {"ok": False, "problems": [{"code": "invalid_arguments", "field": "", "layer": "",
                   "message": "Use --restrictions-check, --restrictions-preview or --restricted-exec=REQUEST -- COMMAND."}]}
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=True, allow_nan=False)
    sys.stdout.write("\n")
    return 0 if payload.get("ok") else 2
