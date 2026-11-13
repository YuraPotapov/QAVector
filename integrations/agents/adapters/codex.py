"""Codex: turns over ``codex app-server``; auth and threads belong to Codex.

    https://learn.chatgpt.com/docs/app-server

A turn is one run of :mod:`integrations.agents.codex_turn` - the bridge that
speaks the app-server's JSON-RPC for exactly one turn - started by the runner
like any CLI: message on stdin, a JSON line per event on stdout. What this
module reads is what the bridge writes: the app-server's own messages, and the
bridge's ``{"qavector": ...}`` lines.

``codex exec --json`` (:func:`exec_argv`) stays as a fallback, switched on with
``QAVECTOR_CODEX_TRANSPORT=exec``: Read and Full only, no planning, no skills.
Its JSONL is still read here.

No model is run by status/models. Threads are continued by their explicit id,
never --last. The user's Codex configuration is not written.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time

import runtime_paths
from integrations.agents import codex_turn
from integrations.agents.contracts import ProviderCapabilities, Unsupported

KIND, NAME, DEFAULT_BINARY = "codex", "Codex", "codex"
MODEL_DEFAULT_LABEL = "Use Codex default"
EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
# Full means commands inside workspace-write, never danger-full-access. Edit
# declines every command Codex asks about - all but the ones it holds to be
# safe and read-only, and those the user's own Codex rules allow.
MODES = ("read", "edit", "full")
MODE_DESCRIPTION = ("Read: read-only sandbox. Edit: edit files in the workspace-write sandbox; "
                    "commands other than Codex's safe read-only ones are refused. Full: edit "
                    "files and run commands in the workspace-write sandbox. Folder "
                    "restrictions still apply.")
#: Plans with Codex's own plan mode: questions end the turn, a plan comes as
#: Codex's plan item (see :mod:`integrations.agents.codex_turn`).
PLANS = True
STATUS_TIMEOUT = 20.0

#: ``exec`` switches turns back to ``codex exec --json``.
TRANSPORT_ENV = "QAVECTOR_CODEX_TRANSPORT"
EXEC_MODES = ("read", "full")

#: Where Codex keeps skills of its own; listed for information, never touched.
OWN_SKILLS = "~/.codex/skills"

MAX_QUESTIONS, MAX_OPTIONS = codex_turn.MAX_QUESTIONS, codex_turn.MAX_OPTIONS


def exec_transport():
    return os.environ.get(TRANSPORT_ENV, "").strip().lower() == "exec"


def capabilities():
    if exec_transport():
        return ProviderCapabilities(KIND, EXEC_MODES, resume=True, selected_model=True, effort=True,
                                    cancel=True, usage=True, qavector=True, assigns_session_id=True)
    return ProviderCapabilities(KIND, MODES, resume=True, selected_model=True, effort=True,
                                cancel=True, usage=True, qavector=True, planning=True, skills=True,
                                assigns_session_id=True)


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
    """The command line for one turn: the bridge, told what the turn is.

    Only paths and choices are in it - the message goes to stdin and the
    QAVector token stays in its file. ``qavector`` is not given to a planning
    turn, which only reads.
    """
    if exec_transport():
        return exec_argv(binary, mode, model, effort, session, resume, skills_dir,
                         attachments_dir, plan, qavector)
    if mode not in MODES:
        raise Unsupported("Codex has Read, Edit and Full.")
    if resume and not session:
        raise Unsupported("Resuming Codex requires its explicit thread id.")
    if effort and effort not in EFFORTS:
        raise Unsupported("Codex does not support the selected effort.")
    server = None
    if qavector and not plan:
        from qcr.mcp_server import server_command
        server = server_command(qavector["socket"], qavector["token_file"])
    spec = {"binary": binary, "mode": mode, "model": str(model or ""), "effort": str(effort or ""),
            "thread": session if resume else "", "plan": bool(plan),
            "skills_root": skills_dir or "", "qavector": server}
    return codex_turn.launcher() + ["--codex-turn", "--spec=" + json.dumps(spec, separators=(",", ":"))]


def plan_prompt(message):
    """A planning turn's message as it is: Codex's plan mode says how to plan."""
    return str(message or "")


def exec_argv(binary, mode="read", model="", effort="", session="", resume=False,
              skills_dir="", attachments_dir="", plan=False, qavector=None):
    """``codex exec --json`` for one turn: the fallback transport."""
    if mode not in EXEC_MODES or plan or skills_dir:
        raise Unsupported("Codex exec supports Read and Full; planning and selected skills "
                          "need the app-server transport.")
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
    """Copy the chosen skills under ``where``, one folder each, for the turn to
    name as an extra skills root. ``skills`` is ``[(name, folder)]``; returns
    ``where``, or "" when there is nothing to lay out."""
    if skills and exec_transport():
        raise Unsupported("Codex exec cannot load selected library skills.")
    shutil.rmtree(where, ignore_errors=True)
    if not skills:
        return ""
    os.makedirs(where)
    for name, folder in skills:
        shutil.copytree(folder, os.path.join(where, name), symlinks=False)
    return where


def own_skills():
    """The names of the skills Codex has by itself. A look at a folder."""
    base = os.path.expanduser(OWN_SKILLS)
    try:
        return sorted(name for name in os.listdir(base) if not name.startswith(".")
                      and os.path.isfile(os.path.join(base, name, "SKILL.md")))
    except OSError:
        return []


def _count(value):
    return value if type(value) is int and value >= 0 else None


def events(line):
    """One stdout line of a turn as conversation events (see the runner).

    The bridge's ``qavector`` lines and the app-server's messages; for the exec
    fallback, its JSONL. Anything else carries nothing.
    """
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        return []
    if not isinstance(event, dict):
        return []
    if "qavector" in event:
        return _bridge_events(event)
    if "method" in event or "id" in event:
        return _server_events(event)
    return _exec_events(event)


def _bridge_events(event):
    kind = event.get("qavector")
    if kind == "thread":
        found = {"type": "started", "provider_session": str(event.get("id") or "")}
        if event.get("model"):
            found["model"] = str(event["model"])
        return [found]
    if kind == "decision" and event.get("decision") == "declined" and event.get("item"):
        return [{"type": "tool.refused", "id": str(event["item"]), "reason": str(event.get("reason") or "")}]
    if kind != "end":
        return []
    tokens = {key: value for key, value in (event.get("tokens") or {}).items()
              if key in ("input", "cache_read", "cache_write", "output", "thinking") and _count(value) is not None}
    if event.get("state") == "completed":
        found = {"type": "completed", "tokens": tokens, "cost_usd": None}
    else:
        found = {"type": "failed", "error": str(event.get("error") or "Codex turn failed."),
                 "tokens": tokens, "cost_usd": None}
    window = _count(event.get("context_window"))
    if window:
        found["context_window"] = window
    return [found]


#: What a tool call is called in a conversation, by Codex's item type.
TOOL_NAMES = {"commandExecution": "shell", "fileChange": "apply_patch", "webSearch": "web_search"}


def _server_events(message):
    method = message.get("method")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if method == "item/tool/requestUserInput":
        return _questions(params.get("questions"))
    if method == "thread/tokenUsage/updated":
        last = (params.get("tokenUsage") or {}).get("last") or {}
        used = _count(last.get("inputTokens")) if isinstance(last, dict) else None
        return [{"type": "context", "used": used}] if used else []
    if method == "error":
        error = params.get("error") if isinstance(params.get("error"), dict) else {}
        text = str(error.get("message") or "Codex reported an error.")
        return [{"type": "notice", "text": text + (" Retrying." if params.get("willRetry") else "")}]
    if method not in ("item/started", "item/completed"):
        return []
    item = params.get("item")
    if not isinstance(item, dict):
        return []
    what, done = item.get("type"), method == "item/completed"
    if what == "agentMessage":
        return [{"type": "text", "text": str(item.get("text") or "")}] if done and item.get("text") else []
    if what == "reasoning":
        text = "\n\n".join(str(one) for one in (item.get("summary") or item.get("content") or []) if one)
        return [{"type": "thinking", "text": text}] if done and text else []
    if what == "plan":
        text = str(item.get("text") or "").strip()
        return [{"type": "plan", "text": text}] if done and text else []
    if what not in ("commandExecution", "fileChange", "mcpToolCall", "webSearch", "dynamicToolCall"):
        return []
    ident = str(item.get("id") or "")
    name = TOOL_NAMES.get(what) or "%s.%s" % (item.get("server") or item.get("namespace") or "tool",
                                               item.get("tool") or "call")
    if not done:
        if what == "fileChange":
            detail = ", ".join(str(one.get("path")) for one in item.get("changes") or []
                               if isinstance(one, dict) and one.get("path"))
        else:
            detail = item.get("command") or item.get("query") or json.dumps(item.get("arguments") or {})
            detail = detail if isinstance(detail, str) else json.dumps(detail)
        return [{"type": "tool.started", "id": ident, "name": name, "title": detail[:256],
                 "detail": detail[:2000]}]
    ok = item.get("status") == "completed" and not item.get("error")
    if what == "commandExecution":
        ok = ok and item.get("exitCode") == 0
    if what == "dynamicToolCall":
        ok = item.get("success") is True
    detail = item.get("aggregatedOutput") or item.get("error") or item.get("result") or ""
    detail = detail if isinstance(detail, str) else json.dumps(detail)
    result = [{"type": "tool.completed", "id": ident, "ok": bool(ok), "detail": detail[:2000]}]
    if what == "fileChange" and ok:
        result += [{"type": "file.written", "path": one["path"]}
                   for one in item.get("changes") or [] if isinstance(one, dict) and isinstance(one.get("path"), str)]
    return result


def _questions(questions):
    """Codex's ``request_user_input`` questions as a ``questions`` event."""
    items = []
    for one in questions if isinstance(questions, list) else []:
        if not isinstance(one, dict):
            continue
        text = " ".join(str(one.get("question") or one.get("header") or "").split())
        options = [" ".join(str(option.get("label") or "").split()) for option in one.get("options") or []
                   if isinstance(option, dict) and str(option.get("label") or "").strip()][:MAX_OPTIONS]
        if text:
            items.append({"question": text, "options": options, "multiple": False})
    return [{"type": "questions", "items": items[:MAX_QUESTIONS]}] if items else []


def _exec_events(event):
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
    server = None
    deadline = time.monotonic() + timeout
    try:
        with tempfile.TemporaryDirectory(prefix="qavector-codex-models-") as folder:
            server = codex_turn.AppServer([path, "app-server"], cwd=folder,
                                          env=turn_env(runtime_paths.clean_subprocess_env()))
            server.initialize(timeout=timeout, experimental=False)
            rows, cursor = [], None
            for _page in range(50):
                result = server.call("model/list", {"limit": 100, "cursor": cursor},
                                     timeout=max(0.001, deadline - time.monotonic()))
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
            raise ValueError("pagination")
    except (OSError, ValueError, KeyError, TypeError, codex_turn.ProtocolError, subprocess.SubprocessError):
        return failure
    finally:
        if server is not None:
            server.close()
