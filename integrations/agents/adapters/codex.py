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
from integrations.agents import codex_turn, locate
from integrations.agents.adapters import claude
from integrations.agents.contracts import PATCH_CHARS, ProviderCapabilities, Unsupported, rate_limit_window

KIND, NAME, DEFAULT_BINARY = "codex", "Codex", "codex"
MODEL_DEFAULT_LABEL = "Use Codex default"
EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
# Full means commands inside workspace-write, never danger-full-access. Edit
# runs the commands Codex parses as reading, listing or searching files, and
# the ones it holds safe by itself or the user's own Codex rules allow; it
# declines the rest.
MODES = ("read", "edit", "full")
MODE_DESCRIPTION = ("Read: read-only sandbox. Edit: edit files in the workspace-write sandbox; "
                    "of the commands, only those Codex reads as reading, listing or searching "
                    "files run. Full: edit files and run commands, with the network, in the "
                    "workspace-write sandbox. Folder restrictions still apply.")
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


#: Where Codex's installers put it, after PATH (see :mod:`integrations.agents.locate`).
#: The ChatGPT desktop app carries a Codex CLI of its own; the Linux path is
#: the one its package installs. The macOS app paths are not yet verified on a
#: real machine.
PLACES = ((("the ChatGPT app", "/usr/lib/chatgpt/resources/codex"),
           ("the ChatGPT app", "/opt/ChatGPT/resources/codex"),
           ("the ChatGPT app", "/Applications/ChatGPT.app/Contents/Resources/codex"),
           ("the Codex app", "/Applications/Codex.app/Contents/Resources/codex"))
          if os.name != "nt" else ()) + locate.node_places("codex") + locate.common_places("codex")

#: What a person who has no Codex is told to do.
INSTALL_HINT = ("Install the ChatGPT desktop app, or the Codex CLI "
                "(npm install -g @openai/codex), then check again.")


def find(binary=None):
    """``(path, where)``: the program a connection uses, and where it was found."""
    return locate.find(DEFAULT_BINARY, PLACES, binary)


def resolve(binary=None):
    return find(binary)[0]


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
            "skills_root": skills_dir or "", "qavector": server, "stream": True}
    return codex_turn.launcher() + ["--codex-turn", "--spec=" + json.dumps(spec, separators=(",", ":"))]


def plan_prompt(message):
    """A planning turn's message as it is: Codex's plan mode says how to plan."""
    return str(message or "")


def streaming():
    """Whether a turn reads its input as lines it may be given more of: the
    app-server bridge does; ``codex exec`` reads one message to its end."""
    return not exec_transport()


def stream_message(message):
    """One message for the bridge, which reads its input a line at a time."""
    return json.dumps({"message": str(message or "")}, ensure_ascii=False) + "\n"


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
    if mode == "full":
        # As the app-server turn's (codex_turn.sandbox): Full's commands reach the network.
        argv += ["-c", "sandbox_workspace_write.network_access=true"]
    for feature in codex_turn.NATIVE_AGENTS:
        argv += ["-c", "features.%s=false" % feature]
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
        if event.get("effort"):
            found["effort"] = str(event["effort"])
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


def read_patch(stdout_path, item_id):
    """Read one completed edit from its turn's saved provider output."""
    with open(stdout_path, "rb") as stream:
        for line in stream:
            if not (b'"item/completed"' in line and b'"fileChange"' in line
                    or b'"item.completed"' in line and b'"file_change"' in line):
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("method") == "item/completed":
                params = message.get("params")
            elif message.get("type") == "item.completed":
                params = message
            else:
                continue
            item = params.get("item") if isinstance(params, dict) else None
            if (not isinstance(item, dict) or item.get("type") not in ("fileChange", "file_change")
                    or item.get("id") != item_id or item.get("status") != "completed" or item.get("error")):
                continue
            files, remaining, truncated = [], PATCH_CHARS, False
            for change in item.get("changes") or []:
                if not isinstance(change, dict) or not isinstance(change.get("path"), str):
                    continue
                diff = change.get("diff")
                diff = diff if isinstance(diff, str) else ""
                if remaining <= 0:
                    truncated = True
                    break
                files.append({"path": change["path"], "diff": diff[:remaining]})
                truncated = truncated or len(diff) > remaining
                remaining -= len(diff)
            if any(file["diff"] for file in files):
                return {"files": files, "truncated": truncated}
            break
    return None


def _patch_events(item, done, ok, detail):
    """Keep file operations as structured data for the chat feed."""
    changes = []
    for change in item.get("changes") or []:
        if not isinstance(change, dict) or not isinstance(change.get("path"), str) or not change["path"]:
            continue
        kind = change.get("kind")
        destination = None
        if isinstance(kind, dict):
            destination = kind.get("movePath") or kind.get("move_path")
            kind = kind.get("type")
        file = {"path": change["path"]}
        if kind in ("add", "update", "delete"):
            file["kind"] = kind
        if isinstance(destination, str) and destination:
            file["destination"] = destination
        changes.append(file)
    event = {"id": str(item.get("id") or ""), "name": "apply_patch", "file_changes": changes}
    if not done:
        return [dict(event, type="tool.started", title="Applying patch",
                     detail=", ".join(file["path"] for file in changes)[:2000])]
    if item.get("status") == "declined":
        event["declined"] = True
    events = [dict(event, type="tool.completed", ok=ok, detail="" if ok else detail[:2000])]
    if ok:
        events.extend({"type": "file.written", "path": file["path"]} for file in changes)
    return events


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
            return _patch_events(item, done=False, ok=False, detail="")
        return [_started(ident, name, item)]
    ok = item.get("status") == "completed" and not item.get("error")
    if what == "commandExecution":
        ok = ok and item.get("exitCode") == 0
    if what == "dynamicToolCall":
        ok = item.get("success") is True
    detail = item.get("aggregatedOutput") or item.get("error") or item.get("result") or ""
    detail = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
    if what == "fileChange":
        return _patch_events(item, done=True, ok=ok, detail=detail)
    return [{"type": "tool.completed", "id": ident, "ok": bool(ok), "detail": detail[:2000]}]


def _started(ident, name, item):
    """A tool call as it begins: a command or query as it is, and a call's
    arguments as Claude's rows say them - lines, not escaped JSON."""
    said = item.get("command") or item.get("query")
    arguments = item.get("arguments")
    if not said and isinstance(arguments, dict):
        title, used = claude.tool_title(name, arguments)
        return {"type": "tool.started", "id": ident, "name": name, "title": title[:256],
                "detail": claude.tool_arguments(arguments, used)[:2000]}
    detail = said or json.dumps(arguments or {}, ensure_ascii=False)
    detail = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
    return {"type": "tool.started", "id": ident, "name": name, "title": detail[:256], "detail": detail[:2000]}


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
        if what == "file_change":
            return _patch_events(item, done=False, ok=False, detail="")
        return [_started(ident, name, item)]
    ok = item.get("status") not in ("failed", "declined") and not item.get("error")
    if what == "command_execution":
        ok = ok and item.get("exit_code") == 0
    detail = str(item.get("aggregated_output") or item.get("error") or "")
    if what == "file_change":
        return _patch_events(item, done=True, ok=ok and item.get("status") == "completed", detail=detail)
    return [{"type": "tool.completed", "id": ident, "ok": bool(ok), "detail": detail[:2000]}]


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


def rate_limits(binary=None, timeout=STATUS_TIMEOUT):
    """Read the account's windows without starting a thread or a model turn."""
    path = resolve(binary)
    if not path:
        return {"ok": False, "error": "Codex is not installed or its program cannot be found."}
    deadline = time.monotonic() + timeout
    try:
        with tempfile.TemporaryDirectory(prefix="qavector-codex-limits-") as folder:
            server = codex_turn.AppServer([path, "app-server"], cwd=folder,
                                          env=turn_env(runtime_paths.clean_subprocess_env()))
            try:
                server.initialize(timeout=timeout, experimental=False)
                result = server.call("account/rateLimits/read", {},
                                     timeout=max(0.001, deadline - time.monotonic()))
            finally:
                server.close()
    except (OSError, codex_turn.ProtocolError, subprocess.SubprocessError):
        return {"ok": False, "error": "Codex did not return limit data. Check its sign-in and version."}

    buckets = result.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        buckets = {"codex": result.get("rateLimits")}
    windows = []
    for key, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        name = bucket.get("limitName") or key
        for field, fallback in (("primary", "Primary limit"), ("secondary", "Secondary limit")):
            raw = bucket.get(field)
            if not isinstance(raw, dict):
                continue
            minutes = raw.get("windowDurationMins")
            label = _window_label(minutes, fallback)
            if key != "codex":
                label += " · " + str(name)
            window = rate_limit_window(label, raw.get("usedPercent"), raw.get("resetsAt"),
                                       duration_minutes=minutes, account_wide=key == "codex")
            if window:
                windows.append(window)
    return {"ok": True, "windows": windows}


def _window_label(minutes, fallback):
    if type(minutes) is not int or minutes <= 0:
        return fallback
    if minutes == 7 * 24 * 60:
        return "Weekly limit"
    for size, unit in ((24 * 60, "day"), (60, "hour"), (1, "minute")):
        if minutes % size == 0:
            return "%d-%s limit" % (minutes // size, unit)
