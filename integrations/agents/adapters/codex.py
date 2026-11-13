"""Codex exec JSONL adapter; auth and persistent threads belong to Codex.

    https://learn.chatgpt.com/docs/non-interactive-mode

No model is run by status/models. Turns use stdin and the explicit thread id,
never --last. No global configuration is edited. A CLI lacking required flags
fails visibly, without falling back to broader permissions.
"""

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time

import runtime_paths
from integrations.agents.contracts import ProviderCapabilities, Unsupported

KIND, NAME, DEFAULT_BINARY = "codex", "Codex", "codex"
MODEL_DEFAULT_LABEL = "Use Codex default"
EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
# Codex cannot promise Claude's edit-without-shell mode. Full means commands
# inside workspace-write, never danger-full-access.
MODES = ("read", "full")
MODE_DESCRIPTION = ("Read: read-only sandbox. Full: edit files and run commands in the "
                    "workspace-write sandbox. Folder restrictions still apply.")
PLANS = False
STATUS_TIMEOUT = 20.0


def capabilities():
    return ProviderCapabilities(KIND, MODES, resume=True, selected_model=True, effort=True,
                                cancel=True, usage=True, qavector=True, assigns_session_id=True)


def resolve(binary=None):
    name = (binary or "").strip() or DEFAULT_BINARY
    if os.path.dirname(os.path.expanduser(name)):
        path = os.path.abspath(os.path.expanduser(name))
        return path if os.path.isfile(path) and os.access(path, os.X_OK) else ""
    return shutil.which(name) or ""


def status(binary=None, timeout=STATUS_TIMEOUT, run=None):
    path = resolve(binary)
    found = dict(installed=bool(path), answered=False, logged_in=False, binary=path,
                 email="", organisation="", plan="", method="", problem="")
    if not path:
        found["problem"] = "Codex is not installed or its program cannot be found."
        return found
    try:
        result = (run or subprocess.run)([path, "login", "status"], stdin=subprocess.DEVNULL,
                    capture_output=True, timeout=timeout, env=turn_env(runtime_paths.clean_subprocess_env()))
    except (OSError, subprocess.SubprocessError):
        found["problem"] = "Codex did not answer the sign-in check."
        return found
    # login status can include part of an API key: never relay its raw output.
    answer = ((result.stdout or b"") + (result.stderr or b"")).decode("utf-8", "replace").lower()
    logged_in = result.returncode == 0 and "logged in" in answer
    found.update(answered=logged_in or "not logged in" in answer, logged_in=logged_in)
    if logged_in:
        found["method"] = "chatgpt" if "chatgpt" in answer else "api_key" if "api key" in answer else ""
    else:
        found["problem"] = "Not signed in. Run codex login." if found["answered"] else "Codex sign-in status is unavailable."
    return found


def describe(found):
    if found.get("problem"):
        return found["problem"]
    return "Signed in with ChatGPT" if found.get("method") == "chatgpt" else "Signed in to Codex"


def login_argv(binary=None, **_kwargs):
    return [resolve(binary) or binary or DEFAULT_BINARY, "login"]


def turn_env(env):
    return dict(env)


def turn_argv(binary, mode="read", model="", effort="", session="", resume=False,
              skills_dir="", attachments_dir="", plan=False, qavector=None):
    if mode not in MODES or plan or skills_dir:
        raise Unsupported("Codex supports Read and Full; planning and selected skills are unavailable.")
    if resume and not session:
        raise Unsupported("Resuming Codex requires its explicit thread id.")
    argv = [binary, "exec", "--json", "--ignore-user-config", "--skip-git-repo-check",
            "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
            "-c", 'sandbox_mode=' + json.dumps("read-only" if mode == "read" else "workspace-write")]
    # This invocation's MCP table replaces the ignored user configuration.
    # Prompt and credentials never appear in argv, only the token file path.
    servers = "{}"
    if qavector:
        from qcr.mcp_server import server_command
        command = server_command(qavector["socket"], qavector["token_file"])
        servers = "{qavector={command=%s,args=%s,required=true}}" % (
            json.dumps(command[0]), json.dumps(command[1:]))
    argv += ["-c", "mcp_servers=" + servers]
    if model:
        argv += ["--model", str(model)]
    if effort:
        if effort not in EFFORTS:
            raise Unsupported("Codex does not support the selected effort.")
        argv += ["-c", "model_reasoning_effort=" + json.dumps(effort)]
    if resume:
        argv += ["resume", session]
    return argv + ["-"]


def stage_skills(where, skills):
    if skills:
        raise Unsupported("Codex selected library skills are unavailable.")
    return ""


def own_skills():
    return []


def _count(value):
    return value if type(value) is int and value >= 0 else None


def events(line):
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        return []
    if not isinstance(event, dict):
        return []
    kind = event.get("type")
    if kind == "thread.started":
        return [{"type": "started", "provider_session": str(event.get("thread_id") or "")}]
    if kind == "turn.completed":
        usage = event.get("usage") or {}
        usage = usage if isinstance(usage, dict) else {}
        tokens = {}
        for src, dest in (("input_tokens", "input"), ("cached_input_tokens", "cache_read"),
                          ("output_tokens", "output"), ("reasoning_output_tokens", "thinking")):
            value = _count(usage.get(src))
            if value is not None:
                tokens[dest] = value
        # Codex input includes cache reads; QAVector's input is the uncached part.
        if "input" in tokens and "cache_read" in tokens:
            tokens["input"] = max(0, tokens["input"] - tokens["cache_read"])
        return [{"type": "completed", "tokens": tokens, "cost_usd": None}]
    if kind == "turn.failed":
        error = event.get("error") or {}
        return [{"type": "failed", "error": str(error.get("message") or "Codex turn failed.")
                 if isinstance(error, dict) else str(error)}]
    # A top-level error may be followed by a successful retry. Only turn.failed
    # or missing terminal result makes the turn fail.
    if kind == "error":
        return [{"type": "notice", "text": str(event.get("message") or "Codex reported an error.")}]
    item = event.get("item")
    if not isinstance(item, dict) or kind not in ("item.started", "item.completed"):
        return []
    what, done = item.get("type"), kind == "item.completed"
    if what in ("agent_message", "reasoning"):
        return [{"type": "text" if what == "agent_message" else "thinking", "text": str(item.get("text") or "")}] if done else []
    if what not in ("command_execution", "mcp_tool_call", "file_change", "web_search"):
        return []
    ident = str(item.get("id") or "")
    name = {"command_execution": "shell", "file_change": "apply_patch", "web_search": "web_search"}.get(what)
    name = name or "%s.%s" % (item.get("server", "mcp"), item.get("tool", "tool"))
    if not done:
        detail = str(item.get("command") or item.get("query") or json.dumps(item.get("arguments") or {}))
        return [{"type": "tool.started", "id": ident, "name": name, "title": detail[:256], "detail": detail[:2000]}]
    ok = item.get("status") not in ("failed", "declined") and not item.get("error")
    if what == "command_execution":
        ok = ok and item.get("exit_code") == 0
    result = [{"type": "tool.completed", "id": ident, "ok": bool(ok),
               "detail": str(item.get("aggregated_output") or item.get("error") or "")[:2000]}]
    if what == "file_change" and ok:
        result += [{"type": "file.written", "path": one["path"]}
                   for one in item.get("changes", []) if isinstance(one, dict) and isinstance(one.get("path"), str)]
    return result


def stages(line, on_text=None):
    return [{"title": event.get("title") or event.get("text") or event["type"],
             "detail": event.get("detail", ""), "kind": event["type"],
             "status": "failed" if event["type"] == "failed" or event.get("ok") is False else "done"}
            for event in events(line)]


def models(binary=None, timeout=STATUS_TIMEOUT):
    """Read model/list via app-server without starting a thread or a model turn."""
    path = resolve(binary)
    failure = {"ok": False, "kind": "unavailable", "models": [], "error": "Codex model list is unavailable."}
    if not path:
        return dict(failure, kind="not_installed")
    process = None
    replies = queue.Queue()
    try:
        with tempfile.TemporaryDirectory(prefix="qavector-codex-models-") as folder:
            process = subprocess.Popen([path, "app-server"], cwd=folder,
                        env=turn_env(runtime_paths.clean_subprocess_env()), stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            def read():
                for line in process.stdout:
                    replies.put(line)
                replies.put(None)
            threading.Thread(target=read, daemon=True).start()
            deadline = time.monotonic() + timeout
            def request(ident, method, params):
                process.stdin.write(json.dumps({"id": ident, "method": method, "params": params}) + "\n")
                process.stdin.flush()
                while True:
                    if time.monotonic() >= deadline:
                        raise queue.Empty()
                    line = replies.get(timeout=max(0.001, deadline - time.monotonic()))
                    if line is None:
                        raise ValueError("closed")
                    response = json.loads(line)
                    if not isinstance(response, dict):
                        raise ValueError("invalid response")
                    if response.get("id") == ident:
                        if "error" in response:
                            raise ValueError("refused")
                        return response["result"]
            request(1, "initialize", {"clientInfo": {"name": "qavector", "version": "1.0"}})
            process.stdin.write('{"method":"initialized"}\n')
            process.stdin.flush()
            rows, cursor, ident = [], None, 2
            while True:
                result = request(ident, "model/list", {"limit": 100, "cursor": cursor})
                for one in result["data"]:
                    if not isinstance(one, dict):
                        raise ValueError("invalid model")
                    if one.get("hidden"):
                        continue
                    rows.append({"value": one["model"], "label": one.get("displayName") or one["model"],
                                 "description": one.get("description") or "",
                                 "efforts": [e["reasoningEffort"] for e in one.get("supportedReasoningEfforts", [])]})
                cursor = result.get("nextCursor")
                if not cursor:
                    return {"ok": True, "models": rows}
                ident += 1
                if ident > 50:
                    raise ValueError("pagination")
    except (OSError, ValueError, KeyError, TypeError, queue.Empty, subprocess.SubprocessError):
        return failure
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdin.close()
            process.stdout.close()
