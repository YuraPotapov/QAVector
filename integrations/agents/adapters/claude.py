"""The Claude Code CLI: where it is, who it is signed in as, how its stream reads.

**Signing in.** ``claude auth login`` is a browser sign-in against a Claude
subscription or a Console account, and the credentials it stores are the CLI's
own business afterwards. Nothing here ever sees a token, stores one, or passes
one - the binary is invoked and it knows who it is. So the first half of this
module is deliberately small. It answers three questions:

* where is the binary,
* is it signed in, and as whom,
* what argv signs in.

``claude auth status --json`` answers the second in about a second, which is
cheap enough for a form to ask on demand and far too slow to ask on every
start - so nothing calls it unasked.

**Reading the stream.** ``--output-format stream-json`` prints one JSON object
per line as the work happens, and every object is either a stage worth showing
or noise worth dropping::

    system/init          started, with this model
    assistant/thinking   what it is weighing
    assistant/tool_use   Read main.py
    user/tool_result     what came back
    assistant/text       what it concluded
    result               done, what it cost

The second half is that translation and nothing else: a line of JSON in, stage
dicts out. No process, no I/O, no Qt - so the mapping is testable against
recorded output, which is the only honest way to check a format somebody else
owns.

**Unknown is not an error.** The CLI is a separate program on its own release
schedule, and a type this does not know about is a line dropped, never a
failure. What a run actually answered comes from its ``result`` object, not
from the rows.

Nothing in this module runs the agent itself.
"""

import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from itertools import islice

import runtime_paths
from integrations.agents import locate
from integrations.agents.contracts import PATCH_CHARS, rate_limit_window

#: What a connection of this kind records, and what a person reads.
KIND = "claude"
NAME = "Claude Code"
MODEL_DEFAULT_LABEL = "Use Claude Code settings"

#: How hard the CLI is asked to think, from ``--effort``, lowest first. Written
#: down here rather than left to the binary because the CLI *warns and carries
#: on* when the level is not one it knows - so a typo would run at the default
#: and leave nobody able to tell that it did.
EFFORTS = ("low", "medium", "high", "xhigh", "max")

#: What the binary is called. Overridable, because somebody may have it
#: installed somewhere PATH does not reach - a nodenv shim, a per-project copy.
DEFAULT_BINARY = "claude"

#: How long to wait for `auth status`. It is a local read of a credentials file
#: plus, sometimes, a token refresh - a second is normal, ten means something is
#: wrong and a settings page should say so rather than hang.
STATUS_TIMEOUT = 20.0


def capabilities():
    from integrations.agents.contracts import ProviderCapabilities
    return ProviderCapabilities(KIND, MODES, resume=True, selected_model=True, effort=True,
                                cancel=True, usage=True, qavector=True,
                                planning=True, skills=True, compact=True, compact_hint=True,
                                cost_scope="session")


#: Where Claude Code's installers put it, after PATH (see
#: :mod:`integrations.agents.locate`): its own installer, then npm and the rest.
PLACES = ((("Claude Code's installer", "~/.claude/local/claude"),)
          + locate.node_places("claude") + locate.common_places("claude"))

#: What a person who has no Claude Code is told to do.
INSTALL_HINT = ("Install Claude Code with its installer or with npm "
                "(npm install -g @anthropic-ai/claude-code), then check again.")


def find(binary=None):
    """``(path, where)``: the program a connection uses, and where it was found.

    A program a connection names is used as it is - a path is checked, a bare
    name is looked up on PATH. Blank looks on PATH and then where Claude Code's
    installers put it. ``("", "")`` when it cannot be found: "it is not
    installed" is a thing the caller reports, which is the useful answer.
    """
    return locate.find(DEFAULT_BINARY, PLACES, binary)


def resolve(binary=None):
    """The command to run. Returns "" when it cannot be found (see :func:`find`)."""
    return find(binary)[0]


def status(binary=None, timeout=STATUS_TIMEOUT, run=None):
    """Who the CLI is signed in as, as plain data. Never raises.

    Always returns the same shape, so a caller renders one thing::

        {"installed": bool, "answered": bool, "logged_in": bool, "binary": str,
         "email": str, "organisation": str, "plan": str, "method": str,
         "problem": str}

    ``problem`` is empty when all is well and a sentence otherwise - a missing
    binary, a CLI too old to have the subcommand, a timeout. It is what a
    settings page shows and what a caller quotes when it refuses to start.
    ``answered`` tells "it said nobody is signed in" from "it could not be
    asked": the first is fixed by signing in, the second is not.

    ``run`` starts the process, with ``subprocess.run``'s signature. A caller
    whose processes have to start somewhere in particular - inside a sandbox -
    passes its own; left out, the CLI is simply run.
    """
    found = {"installed": False, "answered": False, "logged_in": False,
             "binary": "", "email": "", "organisation": "", "plan": "",
             "method": "", "problem": ""}

    path = resolve(binary)
    if not path:
        found["problem"] = ("Claude Code is not installed, or %r is not on PATH."
                            % ((binary or "").strip() or DEFAULT_BINARY))
        return found
    found["installed"], found["binary"] = True, path

    try:
        finished = (run or subprocess.run)(
            [path, "auth", "status", "--json"],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
            env=runtime_paths.clean_subprocess_env(),
            # Somebody else's home directory is not ours to guess at; the CLI
            # reads its own configuration from the environment it is given.
            cwd=os.path.expanduser("~"))
    except (OSError, subprocess.SubprocessError) as exc:
        found["problem"] = "Cannot ask %s who it is signed in as: %s" % (path, exc)
        return found

    text = (finished.stdout or b"").decode("utf-8", "replace").strip()
    try:
        payload = json.loads(text)
    except ValueError:
        # An older CLI without `auth status`, or one that printed a prompt.
        detail = ((finished.stderr or b"").decode("utf-8", "replace").strip()
                  or text)
        found["problem"] = ("%s did not answer with the sign-in status%s"
                            % (path, ": %s" % detail[:200] if detail else "."))
        return found
    if not isinstance(payload, dict):
        found["problem"] = "%s answered with something unexpected." % path
        return found

    found["answered"] = True
    found["logged_in"] = bool(payload.get("loggedIn"))
    found["email"] = str(payload.get("email") or "")
    found["organisation"] = str(payload.get("orgName") or "")
    found["plan"] = str(payload.get("subscriptionType") or "")
    found["method"] = str(payload.get("authMethod") or "")
    if not found["logged_in"]:
        found["problem"] = "Not signed in. Run `%s auth login` once." % path
    return found


def login_argv(binary=None, console=False, email=""):
    """What to run to sign in. The CLI opens a browser and waits.

    Handed back rather than run, because who runs it differs: the settings page
    starts it and watches, and somebody at a terminal just types it.
    """
    path = resolve(binary) or ((binary or "").strip() or DEFAULT_BINARY)
    argv = [path, "auth", "login", "--console" if console else "--claudeai"]
    if email:
        argv.extend(["--email", email])
    return argv


def models(binary=None, timeout=STATUS_TIMEOUT, run=None):
    """Ask the CLI's SDK initialization channel for its model picker.

    No user message is sent. Safe mode disables custom hooks, plugins and
    project instructions; no tools or MCP servers are loaded, and no session
    is persisted. Keep only model metadata, never the account information the
    initialization response also carries. Older CLIs fail visibly rather than
    receiving a prompt or a guessed catalogue.
    """
    path = resolve(binary)
    if not path:
        return {"ok": False, "kind": "not_installed", "models": [],
                "error": "Claude Code is not installed or its program cannot be found."}
    try:
        payload = _sdk_request(path, {"subtype": "initialize", "hooks": {}}, timeout, run)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "kind": "error", "models": [],
                "error": "Could not read Claude Code's models: %s" % exc}
    raw = (payload or {}).get("models")
    if isinstance(raw, list):
        found, seen = [], set()
        for one in raw:
            if not isinstance(one, dict):
                continue
            value = one.get("value")
            if not isinstance(value, str) or not value.strip() or value in seen:
                continue
            seen.add(value)
            found.append({"value": value,
                          "label": str(one.get("displayName") or value),
                          "description": str(one.get("description") or ""),
                          "resolved_model": str(one.get("resolvedModel") or "")})
        if found:
            return {"ok": True, "models": found}
    return {"ok": False, "kind": "unavailable", "models": [],
            "error": "Claude Code did not return a model list. Check its sign-in and version, "
                     "then reopen the list to try again."}


def _sdk_request(path, request, timeout, run=None):
    request_id = "qavector-models" if request["subtype"] == "initialize" else "qavector-limits"
    envelope = {"type": "control_request", "request_id": request_id, "request": request}
    argv = [path, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--safe-mode", "--strict-mcp-config", "--mcp-config",
            '{"mcpServers":{}}', "--tools", "", "--no-session-persistence"]
    with tempfile.TemporaryDirectory(prefix="qavector-claude-sdk-") as folder:
        finished = (run or subprocess.run)(
            argv, input=(json.dumps(envelope) + "\n").encode("utf-8"),
            capture_output=True, timeout=timeout, cwd=folder,
            env=turn_env(runtime_paths.clean_subprocess_env()))
    for line in (finished.stdout or b"").decode("utf-8", "replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "control_response":
            continue
        response = event.get("response")
        if not isinstance(response, dict) or response.get("request_id") != request_id:
            continue
        payload = response.get("response")
        return payload if response.get("subtype") == "success" and isinstance(payload, dict) else None
    return None


def rate_limits(binary=None, timeout=STATUS_TIMEOUT, run=None):
    """Read plan windows through the CLI's SDK control channel."""
    path = resolve(binary)
    if not path:
        return {"ok": False, "error": "Claude Code is not installed or its program cannot be found."}
    try:
        payload = _sdk_request(path, {"subtype": "get_usage", "skip_behaviors": True}, timeout, run)
    except (OSError, subprocess.SubprocessError):
        payload = None
    if payload is None or (payload.get("rate_limits_available") is True
                           and not isinstance(payload.get("rate_limits"), dict)):
        return {"ok": False, "error": "Claude Code did not return limit data. Check its sign-in and version."}
    raw = payload.get("rate_limits")
    windows = []
    if isinstance(raw, dict):
        labels = {"five_hour": "5-hour limit", "seven_day": "Weekly limit",
                  "seven_day_oauth_apps": "Weekly limit · connected apps",
                  "extra_usage": "Extra usage"}
        for key, window in raw.items():
            label = labels.get(key, key.replace("seven_day_", "Weekly limit · ").replace("_", " "))
            if isinstance(window, dict) and (key != "extra_usage" or window.get("is_enabled")):
                minutes = None
                if key == "five_hour":
                    minutes = 5 * 60
                elif key == "seven_day" or key.startswith("seven_day_"):
                    minutes = 7 * 24 * 60
                windows.append(_usage_window(label, window, minutes, account_wide=key in ("five_hour", "seven_day")))
        scoped = raw.get("model_scoped")
        if isinstance(scoped, list):
            for window in scoped:
                if isinstance(window, dict) and isinstance(window.get("display_name"), str):
                    windows.append(_usage_window("Weekly limit · " + window["display_name"], window, 7 * 24 * 60))
    return {"ok": True, "windows": [window for window in windows if window]}


def _usage_window(label, window, minutes, account_wide=False):
    reset = window.get("resets_at")
    resets_at = reset if type(reset) in (int, float) else None
    if isinstance(reset, str):
        try:
            parsed = datetime.fromisoformat(reset.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                resets_at = parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    return rate_limit_window(label, window.get("utilization"), resets_at,
                             duration_minutes=minutes, account_wide=account_wide)


def describe(found):
    """One line for a status row, from what :func:`status` returned."""
    if not found.get("installed"):
        return "Claude Code is not installed"
    if not found.get("logged_in"):
        return "Not signed in"
    who = found.get("email") or found.get("organisation") or "signed in"
    plan = found.get("plan")
    return "Signed in as %s%s" % (who, " (%s)" % plan if plan else "")


# -- the event stream ----------------------------------------------------------
#: How much of a stage's detail is kept. These are read in a panel, not
#: archived - the whole of everything is in the run's stdout.log on disk.
DETAIL_CHARS = 2000

#: How much of a tool's answer is worth showing. Shorter than the rest: a file
#: read comes back whole, and the point of the row is that it came back.
RESULT_CHARS = 600

#: Types that carry no stage. ``thinking_tokens`` arrives many times a turn as a
#: running count, and rate limit notices are about the account rather than the
#: work.
SKIPPED_TYPES = ("rate_limit_event", "stream_event")
SKIPPED_SUBTYPES = ("thinking_tokens",)

#: Where to look, per tool, for the one value that says what it is doing. First
#: match wins, so a tool absent from here still gets a row - just a plainer one.
TELLING = ("file_path", "path", "pattern", "command", "query", "url",
           "notebook_path", "prompt", "description", "skill", "capability")


def stages(line, on_text=None):
    """Every stage one line of the stream carries. Usually none or one.

    A list rather than a single stage because one assistant message can hold
    several blocks - it may weigh something up and then call a tool in the same
    breath, and both are worth a row.

    ``on_text`` is for a caller that asked the agent for something more than
    prose and can recognise it: it is handed each piece of text the agent says,
    whole, and returns the stage to show for it (see :func:`stage`) or ``None``
    for the ordinary row.
    """
    event = _parse(line)
    if not isinstance(event, dict):
        return []

    kind = event.get("type")
    if kind in SKIPPED_TYPES or event.get("subtype") in SKIPPED_SUBTYPES:
        return []
    if kind == "system":
        return _system(event)
    if kind == "result":
        return _result(event)
    if kind in ("assistant", "user"):
        return _message(event, on_text)
    return []


def _parse(line):
    try:
        return json.loads(line)
    except (TypeError, ValueError):
        return None                 # a partial line, or not JSON at all


def _system(event):
    if event.get("subtype") != "init":
        return []
    model = str(event.get("model") or "").strip()
    return [_stage("start", "Started" + (" - %s" % model if model else ""),
                   status="done")]


def _result(event):
    failed = bool(event.get("is_error"))
    bits = []
    turns = event.get("num_turns")
    if isinstance(turns, int):
        bits.append("%d turn%s" % (turns, "" if turns == 1 else "s"))
    cost = event.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        bits.append("$%.4f" % float(cost))
    title = "Failed" if failed else "Finished"
    return [_stage("result", title, detail=", ".join(bits),
                   status="failed" if failed else "done")]


def _message(event, on_text=None):
    """One assistant or user turn, as the blocks it is made of."""
    blocks = ((event.get("message") or {}).get("content")) or []
    if isinstance(blocks, str):                 # some turns carry plain text
        blocks = [{"type": "text", "text": blocks}]
    found = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        one = _block(block, on_text)
        if one is not None:
            found.append(one)
    return found


def _block(block, on_text=None):
    kind = block.get("type")
    if kind == "thinking":
        text = _clean(block.get("thinking"))
        return _stage("thinking", "Thinking", detail=text) if text else None
    if kind == "text":
        text = _clean(block.get("text"))
        if not text:
            return None
        # Whoever asked may know this text for more than prose - an answer it
        # asked for as JSON, say - and how to lay that out. Handed whole: the
        # row's own detail is cut to a length, and half an answer parses as
        # nothing.
        special = on_text(block.get("text")) if on_text is not None else None
        return special or _stage("text", "Answered", detail=text)
    if kind == "tool_use":
        name = str(block.get("name") or "tool")
        arguments = block.get("input")
        title, used = _tool_title(name, arguments)
        return _stage("tool", title, detail=_arguments(arguments, used),
                      status="running")
    if kind == "tool_result":
        return _stage("tool_result", "Returned",
                      detail=_clean(_content(block.get("content")),
                                    RESULT_CHARS),
                      status="failed" if block.get("is_error") else "done")
    return None


def _tool_title(name, arguments):
    """``Read main.py`` rather than ``Read`` - which file it opened is the point.

    Returns the title and the argument it fully accounts for, so the body can
    leave that one out. Only *fully*: a path shows as its basename, and the
    directory it was in is the half a reader still needs.
    """
    if not isinstance(arguments, dict):
        return name, ""
    for key in TELLING:
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip().splitlines()[0]
        if key.endswith("path"):
            text = os.path.basename(text.rstrip("/\\")) or text
        shown = _shorten(text, 70)
        return "%s %s" % (name, shown), key if shown == value else ""
    return name, ""


def _arguments(arguments, used=""):
    """The rest of what a tool was called with, as lines rather than as JSON.

    ``{\n  "file_path": "..."\n}`` is three lines of punctuation around one
    fact. The row is read, not parsed.
    """
    if not isinstance(arguments, dict) or not arguments:
        return ""
    lines = []
    for key in sorted(arguments):
        if key and key == used:
            continue                    # the title already says it, in full
        value = arguments[key]
        if isinstance(value, dict) and value:
            # What a QAVector capability is invoked with: one level more of
            # lines, so its words read as words rather than as escaped JSON.
            lines.append("%s:" % key)
            lines.extend("  %s: %s" % (inner, _one_line(value[inner])) for inner in sorted(value))
        else:
            lines.append("%s: %s" % (key, _one_line(value)))
    return _clean("\n".join(lines))


#: The two for an adapter whose program calls tools of its own shape (Codex):
#: its rows then read as Claude's do.
tool_title, tool_arguments = _tool_title, _arguments


def _one_line(value):
    if isinstance(value, str):
        text = value.strip()
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    return " ".join(text.split("\n"))


def _content(content):
    """A tool result's payload, which the CLI sends as text or as blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [one.get("text", "") for one in content
                 if isinstance(one, dict) and one.get("type") == "text"]
        return "\n".join(part for part in parts if part)
    return "" if content is None else str(content)


def _clean(text, limit=DETAIL_CHARS):
    text = ("" if text is None else str(text)).strip()
    return _shorten(text, limit)


def _shorten(text, limit):
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def stage(kind, title, detail="", status="done", body=None):
    """One row. ``body`` is whatever structure the row has beyond its text."""
    found = {"kind": kind, "title": title, "detail": detail, "status": status}
    if body:
        found["body"] = body
    return found


#: The same two under the names this module calls them by; the public ones are
#: for an ``on_text`` that builds a row of its own.
_stage = stage
clean = _clean


#: The tools that change a file. Whoever lets an agent edit needs to know
#: what it actually touched, and the stream already says so - every write is a
#: ``tool_use`` naming its path, so nothing has to diff a directory afterwards.
WRITING_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def written(line):
    """Every path one line of the stream says was written. Usually none."""
    event = _parse(line)
    if not isinstance(event, dict) or event.get("type") != "assistant":
        return []
    found = []
    for block in ((event.get("message") or {}).get("content")) or []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        if block.get("name") not in WRITING_TOOLS:
            continue
        arguments = block.get("input")
        if not isinstance(arguments, dict):
            continue
        for key in ("file_path", "notebook_path", "path"):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                found.append(value.strip())
                break
    return found


def commands(line):
    """Every shell command one line of the stream says the agent ran.

    What whoever lets an agent run commands reports - the whole list, in
    order - so it can be checked against what the agent was asked to do,
    rather than taking the agent's own account of it.
    """
    event = _parse(line)
    if not isinstance(event, dict) or event.get("type") != "assistant":
        return []
    found = []
    for block in ((event.get("message") or {}).get("content")) or []:
        if (isinstance(block, dict) and block.get("type") == "tool_use"
                and block.get("name") == "Bash"):
            command = (block.get("input") or {}).get("command")
            if isinstance(command, str) and command.strip():
                found.append(command.strip())
    return found


def command_calls(line):
    """``[(tool use id, command)]`` for each shell command one line asks for."""
    event = _parse(line)
    if not isinstance(event, dict) or event.get("type") != "assistant":
        return []
    found = []
    for block in ((event.get("message") or {}).get("content")) or []:
        if (isinstance(block, dict) and block.get("type") == "tool_use"
                and block.get("name") == "Bash"):
            command = (block.get("input") or {}).get("command")
            if isinstance(command, str) and command.strip():
                found.append((str(block.get("id") or ""), command.strip()))
    return found


def refused_calls(line):
    """The tool use ids the CLI refused to run, from the final result line.

    The CLI lists them as ``permission_denials`` when the run ends - the one
    account of a refusal that does not depend on reading its message.
    """
    event = _parse(line)
    if not isinstance(event, dict) or event.get("type") != "result":
        return []
    return [str(one.get("tool_use_id") or "")
            for one in event.get("permission_denials") or []
            if isinstance(one, dict) and one.get("tool_use_id")]


def last_result(text):
    """The final ``result`` object in a stream, as a dict.

    The stream ends with one, and it carries the answer that was actually
    asked for. Read by scanning from the end rather than parsing the whole file:
    every other line is a turn, and only the last one is the answer.
    """
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        event = _parse(line)
        if isinstance(event, dict) and event.get("type") == "result":
            return event
    return None


# -- one turn of a conversation ---------------------------------------------------
#: What a conversation may let the agent do, narrowest first, and the tools
#: that come to. They are named twice: as the only tools the agent is shown
#: (``--tools``) and as the ones it may use without asking (``--allowedTools``).
#: Allowing alone would leave every other tool on offer, refused only when
#: reached for - a Read conversation asked about git tried the shell three
#: times before reading the file it could. ``full`` adds the shell, and with it
#: everything the account can do inside whatever the folder's restrictions
#: leave open.
MODES = ("read", "edit", "full")
MODE_DESCRIPTION = ("Read: it reads the folder and changes nothing. Edit: it also changes files. "
                    "Full: it also runs commands inside the folder's restrictions.")
_READ = ("Read", "Glob", "Grep")
_EDIT = _READ + ("Edit", "Write", "MultiEdit")
TOOLS = {"read": _READ, "edit": _EDIT, "full": _EDIT + ("Bash",)}

#: No MCP servers in a turn. The CLI would otherwise bring in every server the
#: account has - claude.ai connectors among them - though no tool of theirs is
#: one a mode names, and their status notices end up in the agent's answer.
NO_MCP = ("--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}')

# -- planning ---------------------------------------------------------------------
#: This CLI can plan before it works: a planning turn explores read-only and
#: answers with questions or a plan, each as data.
#:
#: Not with the CLI's own planning tools. Run with ``-p`` and nobody at a
#: terminal, Claude Code 2.1.291 does not offer ``AskUserQuestion`` or
#: ``ExitPlanMode`` at all - a model told to use them says it cannot and
#: writes its question as prose. What it does offer is ``--json-schema``: the
#: turn's answer then arrives as ``structured_output``, through a tool of its
#: own (``StructuredOutput``), whatever the model wrote along the way.
PLANS = True

#: Tools the CLI adds for its own purposes, never a row in a conversation.
HIDDEN_TOOLS = ("StructuredOutput",)

#: What a planning turn answers with.
PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["kind"],
    "properties": {
        "kind": {"enum": ["questions", "plan"]},
        "questions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["question", "options"],
            "properties": {"question": {"type": "string"},
                           "options": {"type": "array", "items": {"type": "string"}},
                           "multiple": {"type": "boolean"}}}},
        "plan": {"type": "string"}}}

#: Said before the person's message in a planning turn. In the message, not
#: ``--append-system-prompt``: the CLI keeps the system prompt a conversation
#: began with, so a conversation that starts planning later would never hear it.
PLANNING = (
    "[QAVector: planning turn] Explore read-only and plan; change nothing. If "
    "anything the plan depends on is unclear, answer with kind \"questions\": "
    "each question with 2-4 short options. Otherwise answer with kind \"plan\": "
    "the whole plan in Markdown - what changes where, and how it will be checked.\n\n")

#: How many questions, and options to one, are shown; the rest would not be
#: answered anyway.
MAX_QUESTIONS, MAX_OPTIONS = 8, 6


def plan_prompt(message):
    """What a planning turn tells the CLI: how to answer, then the message."""
    return PLANNING + str(message or "")


def _structured(answer):
    """A planning turn's answer as an event: ``plan {text}`` or ``questions
    {items: [{question, options, multiple}]}``; None for anything else."""
    if not isinstance(answer, dict):
        return None
    if answer.get("kind") == "plan":
        text = str(answer.get("plan") or "").strip()
        return {"type": "plan", "text": text} if text else None
    if answer.get("kind") != "questions":
        return None
    items = []
    for one in answer.get("questions") or []:
        if not isinstance(one, dict):
            continue
        question = " ".join(str(one.get("question") or "").split())
        options = [" ".join(str(option).split()) for option in one.get("options") or []
                   if str(option).strip()][:MAX_OPTIONS]
        if question:
            items.append({"question": question, "options": options,
                          "multiple": bool(one.get("multiple"))})
    return {"type": "questions", "items": items[:MAX_QUESTIONS]} if items else None

#: Variables a turn must not inherit. ``CLAUDE_CODE_SIMPLE`` is the CLI's bare
#: mode, in which it never reads its own sign-in - set in whatever started
#: QAVector, it would leave every turn refused as signed out.
ENV_DROPPED = ("CLAUDE_CODE_SIMPLE",)


#: The name the QAVector MCP server goes by in a turn, and so its tools' prefix.
QAVECTOR_SERVER = "qavector"


def qavector_mcp(socket_path, token_file):
    """``--strict-mcp-config --mcp-config ...`` naming only the QAVector server
    (ADR-003 D4): paths on the command line, the token in its 0600 file."""
    from qcr.mcp_server import server_command
    command = server_command(socket_path, token_file)
    config = {"mcpServers": {QAVECTOR_SERVER: {"type": "stdio", "command": command[0], "args": command[1:]}}}
    return ("--strict-mcp-config", "--mcp-config", json.dumps(config))


def turn_argv(binary, mode="read", model="", effort="", session="", resume=False,
              skills_dir="", attachments_dir="", plan=False, qavector=None, compact=False):
    """The command line for one turn. The message itself is written to stdin.

    ``compact`` changes nothing here: a compaction is the CLI's own ``/compact``
    command, which is the message the runner then writes.

    On stdin rather than in argv for two reasons: argv is readable by every
    process on the machine, and one argument cannot be longer than the kernel
    allows - which a pasted log is.

    ``session`` is the conversation's id in the CLI's own store: given to a
    first turn as the id to use, and to every later one as the conversation to
    continue (``resume``). Empty model and effort leave both to the CLI.

    ``skills_dir`` is what :func:`stage_skills` laid out for this turn, when
    skills were chosen for it. Named, the CLI loads it for this one run, and
    the tool that reads a skill is allowed with the rest - otherwise chosen
    skills would be listed to the agent and refused when it reached for one.

    ``plan`` makes it a planning turn: the CLI's plan mode with the read tools
    whatever ``mode`` says, answering as :data:`PLAN_SCHEMA` - the message
    then goes through :func:`plan_prompt`.

    ``qavector`` (``{"socket", "token_file"}``) gives the turn QAVector itself
    through MCP - the services, scenarios, cycles and reads its connection was
    granted (ADR-003). Only that server; not for a planning turn, which only
    reads. Without it, as ever, no MCP server at all.
    """
    from qavector_agent_claude.command import tool_list
    argv = [binary, "-p", "--output-format", "stream-json", "--verbose", "--input-format", "stream-json"]
    tools = TOOLS["read" if plan else mode] + (("Skill",) if skills_dir else ())
    argv += ["--tools", ",".join(tools)]
    access = qavector if isinstance(qavector, dict) and qavector.get("socket") and qavector.get("token_file") \
        and not plan else None
    argv += tool_list("--allowedTools", tools + (("mcp__" + QAVECTOR_SERVER,) if access else ()))
    argv += qavector_mcp(access["socket"], access["token_file"]) if access else NO_MCP
    if skills_dir:
        argv += ["--plugin-dir", skills_dir]
    if attachments_dir:
        argv += ["--add-dir", attachments_dir]
    if plan:
        argv += ["--permission-mode", "plan", "--json-schema", json.dumps(PLAN_SCHEMA)]
    elif mode != "read":
        # Edits are applied as they are made: a turn that waited to be asked
        # would wait for ever. Not bypassPermissions, which would hand over
        # every tool there is rather than the ones named above.
        argv += ["--permission-mode", "acceptEdits"]
    model = str(model or "").strip()
    if model:
        argv += ["--model", model]
    effort = str(effort or "").strip()
    if effort:
        argv += ["--effort", effort]
    if session:
        argv += ["--resume", session] if resume else ["--session-id", session]
    return argv


def stream_message(message):
    """One complete message, readable by the CLI while its input remains open."""
    return json.dumps({"type": "user", "message": {"role": "user", "content": message}},
                      ensure_ascii=False) + "\n"


def turn_env(env):
    """``env`` as a turn should be started with it."""
    return {key: value for key, value in dict(env).items() if key not in ENV_DROPPED}


# -- skills -----------------------------------------------------------------------
#: Where the CLI keeps skills of its own. Every turn has those whatever is
#: chosen here; they are listed for information and never touched.
OWN_SKILLS = "~/.claude/skills"

#: What the skills chosen for a conversation are called as a plugin: the CLI
#: names each skill of a plugin ``<plugin>:<skill>``.
PLUGIN_NAME = "qavector"


def own_skills():
    """The names of the skills the CLI has by itself. A look at a folder."""
    base = os.path.expanduser(OWN_SKILLS)
    try:
        return sorted(name for name in os.listdir(base)
                      if os.path.isfile(os.path.join(base, name, "SKILL.md")))
    except OSError:
        return []


def stage_skills(where, skills):
    """Lay the chosen skills out the way the CLI loads them for one run.

    ``skills`` is ``[(name, folder)]``. They are *copied* under ``where`` - a
    folder of the conversation's own, made again each turn - as a plugin
    (``--plugin-dir``), so neither the CLI's own configuration nor the folder
    the agent works in is changed, and a restricted turn needs to be shown
    this one folder and nothing of the library. Returns ``where``, or "" when
    there is nothing to lay out.
    """
    shutil.rmtree(where, ignore_errors=True)
    if not skills:
        return ""
    os.makedirs(os.path.join(where, ".claude-plugin"))
    with open(os.path.join(where, ".claude-plugin", "plugin.json"), "w",
              encoding="utf-8") as handle:
        json.dump({"name": PLUGIN_NAME, "version": "1.0.0",
                   "description": "Skills chosen for this conversation in QAVector.",
                   "author": {"name": "QAVector"}},
                  handle, indent=2)
    for name, folder in skills:
        shutil.copytree(folder, os.path.join(where, "skills", name), symlinks=False)
    return where


def events(line):
    """What one line of the stream says happened, for a conversation's record.

    The same lines :func:`stages` reads, kept differently: a stage is a row cut
    to fit a panel, and a record keeps what was said whole. Each is a dict with
    a ``type``::

        started          model, provider_session
        thinking, text   text
        tool.started     id, name, title, detail, and writes and file_changes
                         for a tool that changes a file
        tool.completed   id, ok, detail, and file_changes and patch_available
                         when Claude returned a structured edit result
        tool.refused     id, reason - the CLI would not run that call; its
                         ``tool.completed`` follows, as a failure
        context          used - how much of the agent's context one request
                         filled (input, cache read and cache write together);
                         the last one is how full it was when the turn ended
        compacted        before, after, trigger - the CLI stored a summary in
                         place of the conversation: its size in tokens then
                         and now
        plan             text - a planning turn's plan, in Markdown
        questions        items - a planning turn's questions, each
                         {question, options, multiple}
        completed        text, steps, cost_usd, tokens, context_window,
                         provider_session
        failed           the same, and error

    ``cost_usd`` is what the CLI reports, and for a conversation it continued
    that is the whole conversation so far, not the turn; ``tokens`` - input,
    cache_read, cache_write, output, thinking - are the turn's own.

    Unknown lines carry nothing, as for stages.

    Whether a file *was* written is not said here: a line asking to write one
    cannot know whether the CLI will let it, and the turn's mode may refuse.
    Whoever reads the stream pairs ``writes`` with its ``tool.completed``.
    """
    event = _parse(line)
    if not isinstance(event, dict):
        return []
    kind = event.get("type")
    if kind in SKIPPED_TYPES or event.get("subtype") in SKIPPED_SUBTYPES:
        return []
    if kind == "system":
        if event.get("subtype") == "background_tasks_changed" and isinstance(event.get("tasks"), list):
            return [{"type": "background.tasks", "count": len(event["tasks"])}]
        if event.get("subtype") == "task_notification":
            return [{"type": "background.notification", "id": event.get("task_id"),
                     "status": event.get("status"), "summary": event.get("summary") or ""}]
        if event.get("subtype") == "compact_boundary":
            meta = event.get("compact_metadata") if isinstance(event.get("compact_metadata"), dict) else {}
            return [{"type": "compacted", "before": _count(meta.get("pre_tokens")),
                     "after": _count(meta.get("post_tokens")),
                     "trigger": str(meta.get("trigger") or "")}]
        if event.get("subtype") == "permission_denied":
            # Said apart from the call's own result, whose text reads as a
            # question ("requires approval") nobody in a turn can answer.
            return [{"type": "tool.refused", "id": str(event.get("tool_use_id") or ""),
                     "reason": str(event.get("message") or event.get("decision_reason")
                                   or "").strip()}]
        if event.get("subtype") != "init":
            return []
        return [{"type": "started", "model": str(event.get("model") or "").strip(),
                 "provider_session": str(event.get("session_id") or "")}]
    if kind == "result":
        failed = bool(event.get("is_error"))
        text = str(event.get("result") or "").strip()
        answered = None if failed else _structured(event.get("structured_output"))
        if answered:
            # The result's text is the same answer as JSON; the event before
            # the end says it in a form a page can show.
            text = ""
        found = {"type": "failed" if failed else "completed", "text": text,
                 "provider_session": str(event.get("session_id") or "")}
        if isinstance(event.get("num_turns"), int):
            found["steps"] = event["num_turns"]
        cost = event.get("total_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            found["cost_usd"] = round(float(cost), 6)
        tokens = _tokens(event.get("usage"))
        if tokens:
            found["tokens"] = tokens
        window = _context_window(event.get("modelUsage"))
        if window:
            found["context_window"] = window
        if failed:
            found["error"] = text or str(event.get("subtype") or "The agent refused.")
        return [answered, found] if answered else [found]
    if kind not in ("assistant", "user"):
        return []
    blocks = ((event.get("message") or {}).get("content")) or []
    if isinstance(blocks, str):
        blocks = [{"type": "text", "text": blocks}]
    found = []
    if kind == "assistant" and not event.get("parent_tool_use_id"):
        # The agent's own request, not a helper it started: what it was sent
        # is how full its context is now.
        used = _context_used((event.get("message") or {}).get("usage"))
        if used:
            found.append({"type": "context", "used": used})
    for block in blocks:
        if not isinstance(block, dict):
            continue
        what = block.get("type")
        # Only the agent's own words: a "user" line is the CLI handing a tool's
        # answer back, and text in it is not something the agent said.
        if what in ("thinking", "text") and kind == "assistant":
            text = str(block.get(what) or "").strip()
            if text:
                found.append({"type": what, "text": text})
        elif what == "tool_use":
            name = str(block.get("name") or "tool")
            arguments = block.get("input")
            title, used = _tool_title(name, arguments)
            started = {"type": "tool.started", "id": str(block.get("id") or ""),
                       "name": name, "title": title, "detail": _arguments(arguments, used)}
            target = _write_target(name, arguments)
            if target:
                started["writes"] = target
                change = {"path": target}
                if name != "Write":
                    change["kind"] = "update"
                started["file_changes"] = [change]
            found.append(started)
        elif what == "tool_result":
            completed = {"type": "tool.completed", "id": str(block.get("tool_use_id") or ""),
                         "ok": not block.get("is_error"),
                         "detail": _clean(_content(block.get("content")), RESULT_CHARS)}
            if kind == "user" and completed["ok"] and len(blocks) == 1:
                result = _edit_result(event)
                if result:
                    completed["file_changes"] = [{"path": result["filePath"],
                                                  "kind": "add" if result.get("type") == "create" else "update"}]
                    completed["patch_available"] = bool(
                        result.get("structuredPatch") or
                        (result.get("type") == "create" and result.get("content")))
            found.append(completed)
    return found


def _edit_result(event):
    result = event.get("tool_use_result", event.get("toolUseResult"))
    if not isinstance(result, dict) or not isinstance(result.get("filePath"), str) or not result["filePath"]:
        return None
    if isinstance(result.get("structuredPatch"), list):
        return result
    if result.get("type") == "create" and isinstance(result.get("content"), str):
        return result
    return None


def read_patch(stdout_path, item_id):
    """Build a diff from the saved, successful result of one file edit."""
    with open(stdout_path, "rb") as stream:
        for line in stream:
            if b'"tool_use_id"' not in line:
                continue
            event = _parse(line)
            if not isinstance(event, dict):
                continue
            if event.get("subtype") == "permission_denied" and event.get("tool_use_id") == item_id:
                return None
            if event.get("type") != "user":
                continue
            blocks = (event.get("message") or {}).get("content")
            if not isinstance(blocks, list) or len(blocks) != 1:
                continue
            block = blocks[0]
            if (not isinstance(block, dict) or block.get("type") != "tool_result"
                    or block.get("tool_use_id") != item_id):
                continue
            result = _edit_result(event)
            if not result or block.get("is_error"):
                return None
            diff = "".join(islice(_patch_chars(result), PATCH_CHARS + 1))
            if diff:
                return {"files": [{"path": result["filePath"], "diff": diff[:PATCH_CHARS]}],
                        "truncated": len(diff) > PATCH_CHARS}
            return None
    return None


def _patch_chars(result):
    for hunk in result.get("structuredPatch") or ():
        if not isinstance(hunk, dict):
            continue
        ranges = [hunk.get(key) for key in ("oldStart", "oldLines", "newStart", "newLines")]
        lines = hunk.get("lines")
        if not all(type(value) is int for value in ranges) or not isinstance(lines, list):
            continue
        yield from "@@ -%d,%d +%d,%d @@\n" % tuple(ranges)
        for line in lines:
            if isinstance(line, str):
                yield from line
                yield "\n"
    if not result.get("structuredPatch") and result.get("type") == "create":
        content = result.get("content")
        if isinstance(content, str):
            # A created file has no before state. Bound the preview before splitting it.
            for line in content[:PATCH_CHARS + 1].splitlines():
                yield "+"
                yield from line
                yield "\n"


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _tokens(usage):
    """The turn's tokens from a result's ``usage``, which - unlike
    ``modelUsage`` and the cost - counts this turn alone."""
    if not isinstance(usage, dict):
        return {}
    details = usage.get("output_tokens_details") or {}
    found = {"input": _count(usage.get("input_tokens")),
             "cache_read": _count(usage.get("cache_read_input_tokens")),
             "cache_write": _count(usage.get("cache_creation_input_tokens")),
             "output": _count(usage.get("output_tokens")),
             "thinking": _count(details.get("thinking_tokens")
                                if isinstance(details, dict) else 0)}
    return found if any(found.values()) else {}


def _context_used(usage):
    if not isinstance(usage, dict):
        return 0
    return sum(_count(usage.get(key)) for key in (
        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


def _context_window(models):
    """How much context the turn's model holds: the largest any model it
    used says, the main model's being the one a conversation fills."""
    if not isinstance(models, dict):
        return 0
    return max((_count((one or {}).get("contextWindow")) for one in models.values()
                if isinstance(one, dict)), default=0)


def _write_target(name, arguments):
    """The path a tool call asks to write, when it is one that writes."""
    if name not in WRITING_TOOLS or not isinstance(arguments, dict):
        return ""
    for key in ("file_path", "notebook_path", "path"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
