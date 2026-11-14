"""One Codex turn over ``codex app-server``: what the runner starts for Codex.

The runner treats this like any agent CLI: the message on stdin, one JSON line
per event on stdout, the exit code at the end. Behind that it speaks the
app-server's JSON-RPC (``initialize`` -> ``thread/start|resume`` ->
``turn/start`` -> ``turn/completed``) and lives exactly one turn, so nothing is
held open between turns.

    https://learn.chatgpt.com/docs/app-server

What it writes to stdout, in order of arrival:

* every message the app-server sends, byte for byte - the turn's raw record;
* lines of its own, ``{"qavector": <kind>, ...}``: ``thread`` (the thread id
  and model), ``decision`` (how it answered a request) and ``end`` (how the
  turn ended, with the turn's own token counts).

Its decisions, none of them asked of anybody during the turn:

* commands that need approval are declined; file changes are accepted only in
  Edit and Full; extra permissions, MCP elicitations and other callbacks are
  refused - a turn never waits for an answer nobody will give;
* the agent's questions (``request_user_input``) end the turn: they are
  recorded, the turn is interrupted, and the answers come as the next message;
* MCP servers from the user's Codex configuration are switched off, and so
  are plugins, apps, computer and browser use, hooks and memories. Only the
  QAVector server this turn was given is there.

Stopped (SIGTERM), it interrupts the turn, gives Codex a moment to finish
writing its thread, and exits 130 without an ``end`` line.
"""

import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time

CLIENT_INFO = {"name": "qavector", "title": "QAVector", "version": "1.0"}

#: How long the app-server has to answer a request (thread/start may first
#: start the QAVector MCP server, which it then has to reach).
REPLY_TIMEOUT = 120.0

#: How long an interrupted turn has to end before the app-server is stopped.
INTERRUPT_GRACE = 3.0

#: Codex features a QAVector turn never has, whatever the user's configuration
#: says: it ran with ``exec --ignore-user-config`` before, and none of these
#: belong in a turn whose access QAVector decides.
FEATURES_OFF = ("plugins", "apps", "computer_use", "browser_use", "in_app_browser",
                "image_generation", "hooks", "memories")

#: The name the QAVector MCP server goes by in a turn.
QAVECTOR_SERVER = "qavector"

#: What the app-server may call an MCP server for ``-c mcp_servers.<name>``.
SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]+$")

#: How many questions, and options to one, are kept.
MAX_QUESTIONS, MAX_OPTIONS = 8, 6

STOPPED_EXIT = 130


class ProtocolError(Exception):
    """The app-server refused, ended or did not answer."""


class AppServer:
    """``codex app-server`` on stdio. Every message it sends goes through
    ``on_message(raw, message)`` once, in order, on the caller's thread."""

    def __init__(self, argv, cwd=None, env=None, stderr=None, on_message=None):
        self._next_id = 0
        self._inbox = queue.Queue()
        self.on_message = on_message or (lambda raw, message: None)
        # A session of its own: a stop meant for the turn reaches this
        # process, which interrupts the turn, rather than killing Codex
        # mid-write. Codex still ends with us: its stdin closes.
        self.process = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=stderr if stderr is not None else subprocess.DEVNULL,
            start_new_session=(os.name != "nt"))
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for raw in self.process.stdout:
                try:
                    message = json.loads(raw)
                except ValueError:
                    message = None
                self._inbox.put((raw, message if isinstance(message, dict) else None))
        except (OSError, ValueError):
            pass
        self._inbox.put(None)

    def send(self, message):
        try:
            self.process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise ProtocolError("Codex app-server stopped reading: %s" % exc) from None

    def request(self, method, params):
        """Send a request; its id. The reply arrives through :meth:`pump`."""
        self._next_id += 1
        self.send({"id": self._next_id, "method": method, "params": params})
        return self._next_id

    def pump(self, timeout):
        """One message, handed to ``on_message`` and returned; None when
        nothing came within ``timeout``. Raises when the app-server ended."""
        try:
            item = self._inbox.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None
        if item is None:
            self._inbox.put(None)
            raise ProtocolError("Codex app-server ended.")
        raw, message = item
        self.on_message(raw, message)
        return message or {}

    def call(self, method, params, timeout=REPLY_TIMEOUT, interrupted=None):
        """A request and its result. Raises :class:`ProtocolError` for an
        error reply, an ended app-server or no reply in ``timeout``."""
        ident = self.request(method, params)
        deadline = time.monotonic() + timeout
        while True:
            if interrupted is not None and interrupted():
                raise ProtocolError("stopped")
            left = deadline - time.monotonic()
            if left <= 0:
                raise ProtocolError("Codex app-server did not answer %s." % method)
            message = self.pump(min(0.1, left))
            if message is None or message.get("id") != ident or "method" in message:
                continue
            if "error" in message:
                error = message.get("error") or {}
                text = error.get("message") if isinstance(error, dict) else error
                raise ProtocolError("Codex refused %s: %s" % (method, text or "no reason given"))
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    def initialize(self, timeout=REPLY_TIMEOUT, experimental=True):
        result = self.call("initialize", {"clientInfo": CLIENT_INFO,
                                          "capabilities": {"experimentalApi": experimental}},
                           timeout=timeout)
        self.send({"method": "initialized"})
        return result

    def close(self):
        process = self.process
        try:
            process.stdin.close()             # stdio app-server ends on EOF
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "nt":
                    process.terminate()
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait()
        try:
            process.stdout.close()
        except OSError:
            pass


#: What Codex says a command does (its ``commandActions``) that Edit lets run.
READ_ACTIONS = frozenset(("read", "listFiles", "search"))


def reads_only(actions):
    """Whether Codex parsed a command into reading, listing and searching only.
    Nothing parsed, or anything it could not name ("unknown"), is not."""
    return (isinstance(actions, list) and bool(actions)
            and all(isinstance(one, dict) and one.get("type") in READ_ACTIONS for one in actions))


def qavector_tool_call(params):
    """An approval Codex asks for a tool of the turn's own QAVector server."""
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    return params.get("serverName") == QAVECTOR_SERVER and meta.get("codex_approval_kind") == "mcp_tool_call"


def launcher():
    """How the runner starts a bridge: the core itself, as for --qcr-mcp."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "session_launcher.py")]


def server_argv(binary, qavector=None, switched_off=()):
    """The app-server's command line. ``qavector`` is the MCP server command
    (a list) or None; ``switched_off`` names configured MCP servers to turn off."""
    argv = [binary, "app-server", "-c", 'web_search="disabled"']
    for feature in FEATURES_OFF:
        argv += ["-c", "features.%s=false" % feature]
    for name in switched_off:
        argv += ["-c", "mcp_servers.%s.enabled=false" % name]
    if qavector:
        # Its tools are not asked about: what an agent may do there is the QCR
        # host's grants, which the person set on the connection.
        argv += ["-c", "mcp_servers.%s={command=%s,args=%s,required=true,"
                       "default_tools_approval_mode=\"approve\"}" % (
            QAVECTOR_SERVER, json.dumps(qavector[0]), json.dumps(list(qavector[1:])))]
    return argv


def sandbox(mode, plan):
    """``(thread sandbox, turn sandboxPolicy, approvalPolicy)`` for a mode.

    Read and planning: read-only. Full: workspace-write, nothing asked. Edit:
    workspace-write with ``untrusted`` - Codex asks before any command it does
    not hold to be safe and read-only, and this turn declines every one.
    Never danger-full-access, and never network from the sandbox.
    """
    if plan or mode == "read":
        return "read-only", {"type": "readOnly", "networkAccess": False}, "never"
    policy = {"type": "workspaceWrite", "networkAccess": False}
    return "workspace-write", policy, "untrusted" if mode == "edit" else "never"


class Turn:
    """One turn, as :func:`main` runs it."""

    def __init__(self, spec, prompt, out=None, env=None):
        self.spec = spec
        self.prompt = prompt
        self.out = out or sys.stdout.buffer
        self.env = env if env is not None else dict(os.environ)
        self.cwd = os.getcwd()
        self.stopping = threading.Event()
        self.server = None
        self.thread_id = ""
        self.turn_id = ""
        self.asked = False
        self.interrupting = False
        self.usage = None                 # (total before this turn, last total, window)
        self.ended = None                 # the turn/completed params' turn

    # -- output ---------------------------------------------------------------
    def _write(self, raw):
        self.out.write(raw if raw.endswith(b"\n") else raw + b"\n")
        self.out.flush()

    def say(self, kind, **fields):
        self._write(json.dumps(dict({"qavector": kind}, **fields)).encode("utf-8"))

    def _on_message(self, raw, message):
        self._write(raw)
        if not message:
            return
        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method and "id" in message:
            self._answer(message["id"], method, params)
        elif method == "thread/tokenUsage/updated" and params.get("turnId") == self.turn_id:
            self._count(params.get("tokenUsage") or {})
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            if isinstance(turn, dict) and turn.get("id") == self.turn_id:
                self.ended = turn

    # -- requests Codex makes of us ------------------------------------------
    def _answer(self, ident, method, params):
        item = str(params.get("itemId") or "")
        mode, plan = self.spec.get("mode"), bool(self.spec.get("plan"))
        if method == "item/tool/requestUserInput":
            # Interrupt before answering, so nothing is done on an empty answer.
            self.asked = True
            self._interrupt()
            self.server.send({"id": ident, "result": {"answers": {}}})
            self.say("decision", request=method, item=item, decision="asked",
                     reason="The questions end the turn; the answers come as the next message.")
            return
        if method == "item/fileChange/requestApproval" and mode in ("edit", "full") and not plan:
            self.server.send({"id": ident, "result": {"decision": "accept"}})
            self.say("decision", request=method, item=item, decision="accepted")
            return
        if method == "item/commandExecution/requestApproval" and mode == "edit" and not plan \
                and reads_only(params.get("commandActions")):
            # Codex reads through the shell; in Edit it may, as long as Codex
            # itself says the command only reads, lists or searches.
            self.server.send({"id": ident, "result": {"decision": "accept"}})
            self.say("decision", request=method, item=item, decision="accepted", reason="reads only")
            return
        if method == "mcpServer/elicitation/request" and qavector_tool_call(params):
            # Asked only if the server's own setting was not honoured: the same answer.
            self.server.send({"id": ident, "result": {"action": "accept", "content": {}}})
            self.say("decision", request=method, item=item, decision="accepted", reason="QAVector tool")
            return
        reason = {
            "item/commandExecution/requestApproval":
                "In Edit only commands that read, list or search files run (cat, rg, ls); Full runs "
                "the rest." if mode == "edit" and not plan else
                "This conversation's mode does not let the agent run that command.",
            "item/fileChange/requestApproval": "This conversation's mode does not let the agent edit files.",
            "item/permissions/requestApproval": "A turn is never given more access than its mode.",
            "mcpServer/elicitation/request": "Nobody can answer an MCP server's question during a turn.",
        }.get(method)
        result = {
            "item/commandExecution/requestApproval": {"decision": "decline"},
            "item/fileChange/requestApproval": {"decision": "decline"},
            "item/permissions/requestApproval": {"permissions": {}, "scope": "turn"},
            "mcpServer/elicitation/request": {"action": "decline"},
            "applyPatchApproval": {"decision": "denied"},
            "execCommandApproval": {"decision": "denied"},
        }.get(method)
        if result is None:
            self.server.send({"id": ident, "error": {"code": -32601,
                                                     "message": "QAVector does not handle %s." % method}})
            reason = "QAVector does not handle this request."
        else:
            self.server.send({"id": ident, "result": result})
        self.say("decision", request=method, item=item, decision="declined",
                 reason=reason or "Declined: nobody is asked during a turn.")

    def _interrupt(self):
        if self.interrupting or not self.turn_id:
            return
        self.interrupting = True
        self.server.request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id})

    def _count(self, usage):
        total, last = usage.get("total"), usage.get("last")
        if not isinstance(total, dict) or not isinstance(last, dict):
            return
        window = usage.get("modelContextWindow")
        if self.usage is None:
            # The thread's total before this turn's first request.
            before = {key: _int(total.get(key)) - _int(last.get(key)) for key in total}
            self.usage = [before, total, window]
        else:
            self.usage[1:] = [total, window]

    def tokens(self):
        """The turn's own counts, as QAVector names them: input is the uncached
        part (Codex counts cache reads into input)."""
        if self.usage is None:
            return {}, None
        before, total, window = self.usage
        spent = {key: max(0, _int(total.get(key)) - _int(before.get(key))) for key in total}
        tokens = {"input": max(0, spent.get("inputTokens", 0) - spent.get("cachedInputTokens", 0)),
                  "cache_read": spent.get("cachedInputTokens", 0),
                  "output": spent.get("outputTokens", 0),
                  "thinking": spent.get("reasoningOutputTokens", 0)}
        if spent.get("cacheWriteInputTokens"):
            tokens["cache_write"] = spent["cacheWriteInputTokens"]
        return tokens, window if isinstance(window, int) and window > 0 else None

    # -- the turn -------------------------------------------------------------
    def _open(self):
        """The app-server, with every MCP server of the user's own configuration
        switched off: read from the configuration this folder sees, then started
        again without them when there are any."""
        qavector = self.spec.get("qavector") or None
        server = AppServer(server_argv(self.spec["binary"], qavector), cwd=self.cwd, env=self.env,
                           stderr=sys.stderr, on_message=self._on_message)
        self.server = server
        server.initialize()
        config = (server.call("config/read", {"cwd": self.cwd, "includeLayers": False})
                  .get("config") or {})
        servers = config.get("mcp_servers") if isinstance(config.get("mcp_servers"), dict) else {}
        foreign = sorted(name for name, one in servers.items() if name != QAVECTOR_SERVER
                         and not (isinstance(one, dict) and one.get("enabled") is False))
        if foreign:
            bad = [name for name in foreign if not SERVER_NAME.match(name)]
            if bad:
                raise ProtocolError("Codex's configuration has an MCP server QAVector cannot "
                                    "switch off: %s" % ", ".join(bad))
            server.close()
            server = AppServer(server_argv(self.spec["binary"], qavector, foreign), cwd=self.cwd,
                               env=self.env, stderr=sys.stderr, on_message=self._on_message)
            self.server = server
            server.initialize()
        skills_root = self.spec.get("skills_root") or ""
        if skills_root:
            # For this process only; the user's configuration is not written.
            server.call("skills/extraRoots/set", {"extraRoots": [skills_root]})
        return server

    def _thread(self):
        thread_sandbox, _policy, approval = sandbox(self.spec.get("mode"), self.spec.get("plan"))
        params = {"cwd": self.cwd, "approvalPolicy": approval, "approvalsReviewer": "user",
                  "sandbox": thread_sandbox}
        if self.spec.get("model"):
            params["model"] = self.spec["model"]
        if self.spec.get("thread"):
            params["threadId"] = self.spec["thread"]
            result = self.server.call("thread/resume", params, interrupted=self.stopping.is_set)
        else:
            result = self.server.call("thread/start", params, interrupted=self.stopping.is_set)
        thread = result.get("thread") or {}
        self.thread_id = str(thread.get("id") or self.spec.get("thread") or "")
        if not self.thread_id:
            raise ProtocolError("Codex started no thread.")
        model = str(result.get("model") or self.spec.get("model") or "")
        effort = str(self.spec.get("effort") or result.get("reasoningEffort") or "")
        self.say("thread", id=self.thread_id, model=model, effort=effort, resumed=bool(self.spec.get("thread")))
        return model

    def _start_turn(self, model):
        _sandbox, policy, approval = sandbox(self.spec.get("mode"), self.spec.get("plan"))
        effort = self.spec.get("effort") or None
        params = {"threadId": self.thread_id, "cwd": self.cwd,
                  "input": [{"type": "text", "text": self.prompt, "text_elements": []}],
                  "approvalPolicy": approval, "approvalsReviewer": "user", "sandboxPolicy": policy,
                  # Always said: a mode chosen for one turn stays for the next.
                  "collaborationMode": {"mode": "plan" if self.spec.get("plan") else "default",
                                        "settings": {"model": model, "reasoning_effort": effort,
                                                     "developer_instructions": None}}}
        if effort:
            params["effort"] = effort
        if self.spec.get("model"):
            params["model"] = self.spec["model"]
        turn = self.server.call("turn/start", params, interrupted=self.stopping.is_set).get("turn") or {}
        self.turn_id = str(turn.get("id") or "")
        if not self.turn_id:
            raise ProtocolError("Codex started no turn.")

    def _follow(self):
        """Read until the turn ends; its ``turn`` object."""
        deadline = None
        while self.ended is None:
            if self.stopping.is_set():
                self._interrupt()
                deadline = deadline or time.monotonic() + INTERRUPT_GRACE
                if time.monotonic() >= deadline:
                    return None
            self.server.pump(0.1)
        return self.ended

    def run(self):
        """The exit code: 0 completed, 1 failed, 130 stopped."""
        if os.name != "nt":
            signal.signal(signal.SIGTERM, lambda *_args: self.stopping.set())
        try:
            self._open()
            model = self._thread()
            self._start_turn(model)
            turn = self._follow()
        except ProtocolError as exc:
            if self.stopping.is_set():
                return STOPPED_EXIT
            self.say("end", state="failed", error=str(exc))
            return 1
        finally:
            if self.server is not None:
                self.server.close()
        if turn is None or (self.stopping.is_set() and not self.asked):
            return STOPPED_EXIT
        tokens, window = self.tokens()
        status = turn.get("status")
        error = turn.get("error") if isinstance(turn.get("error"), dict) else {}
        if status == "completed" or (status == "interrupted" and self.asked):
            self.say("end", state="completed", tokens=tokens, context_window=window,
                     asked=self.asked)
            return 0
        message = str(error.get("message") or "") or (
            "Codex interrupted the turn." if status == "interrupted" else "Codex turn failed.")
        self.say("end", state="failed", error=message, tokens=tokens, context_window=window)
        return 1


def _int(value):
    return value if type(value) is int else 0


def main(argv):
    """``--codex-turn --spec=<json>``; the message on stdin."""
    spec = None
    for arg in argv:
        if arg.startswith("--spec="):
            try:
                spec = json.loads(arg[len("--spec="):])
            except ValueError:
                spec = None
    if not isinstance(spec, dict) or not isinstance(spec.get("binary"), str) or not spec["binary"]:
        print("--codex-turn needs --spec with the Codex binary.", file=sys.stderr)
        return 2
    prompt = sys.stdin.buffer.read().decode("utf-8", "replace")
    return Turn(spec, prompt).run()
