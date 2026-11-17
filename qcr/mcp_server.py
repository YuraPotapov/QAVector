"""The QAVector MCP server an agent's CLI starts (ADR-003 D4/D5).

``session_launcher.py --qcr-mcp --socket=PATH --token-file=PATH``: MCP over
stdio (JSON-RPC 2.0, one message per line), and every tool call forwarded to
the QCR host over its agent socket as the actor the token stands for. This
process decides nothing: what the agent may see and do is the host's answer,
from the grants the GUI gave that actor.

The token is read from its file once, at start; it is never on a command line
and never in a reply.

Tools (their schemas are fixed here; what each capability takes is what
``qavector_describe`` says, generated from the registry):

* ``qavector_list_capabilities`` - what this agent may use, optionally one namespace
* ``qavector_describe`` - one capability: its input and output schema, risk
* ``qavector_read`` - a read-only capability (a service's status, a Jira issue,
  what a cycle has open)
* ``qavector_invoke`` - start an action; returns its execution, usually unfinished
* ``qavector_get_execution`` / ``qavector_wait`` (at most 60 s a call) /
  ``qavector_events`` / ``qavector_cancel`` - follow it and stop it
"""

import json
import os
import socket
import sys

PROTOCOL_VERSION = "2025-06-18"
SERVER = {"name": "qavector", "version": "1"}
WAIT_SECONDS = 60

_ID = {"type": "string", "minLength": 1, "maxLength": 256}
TOOLS = [
    {"name": "qavector_list_capabilities",
     "description": "List the QAVector capabilities you may use in this project: services, scenarios, cycles, "
                    "integrations, agents. Each names what it acts on (resource.name). The set changes - list "
                    "again in each new turn rather than relying on an earlier list. A service has reads beside "
                    "its actions: status, criteria (wait on readiness) and output (its log lines). agents: hand "
                    "another agent a task (agents.<id>.delegate), send it a message or ask it a question "
                    "(agents.<id>.message; expects_reply for an answer), record whether a task's criteria "
                    "passed (agents.tasks.verify). These may wait for the person's approval. Delegate a part "
                    "another agent does better or in parallel, or an independent check - not small things "
                    "or what needs this conversation's context. Read agents.<id>.describe first and give "
                    "the model and effort the task needs: a cheaper model and lower effort for simple work. "
                    "Roles (agents.roles.<id>.delegate) are the person's own: use the one they name, else pick by "
                    "what it does; a reviewer's rework skips what is after it. "
                    "Name tasks; after: [names] starts one only when those succeed; agents.tasks.wake_when "
                    "says when you are woken with results. The invoke answer's next says whether to end "
                    "your turn. A task that asks you a question waits: answer it with agents.tasks.reply, "
                    "which goes on in its own conversation - never delegate it again. "
                    "integrations: "
                    "Jira reads - for one issue start with integrations.jira.<id>.context: Jira's fields and "
                    "comments with what QAVector knows (conversations, tasks, cost, private notes); reading an "
                    "issue's details links this conversation to it -, and changes (comment, transition, assign, edit, comment_edit, create, attach "
                    "from your folder) that the person sees, may edit, and approves or denies - invoke, then "
                    "wait; one change per connection at a time.",
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "properties": {"namespace": {"type": "string", "description": "services, scenarios, cycles, "
                                                                                   "integrations, agents"}}}},
    {"name": "qavector_describe",
     "description": "One capability in full: what it does, its input and output JSON Schema, its risk.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["capability"],
                     "properties": {"capability": _ID}}},
    {"name": "qavector_read",
     "description": "Call a read-only capability (describe, status, pending, an integration read) and get its answer.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["capability"],
                     "properties": {"capability": _ID, "input": {"type": "object"}}}},
    {"name": "qavector_invoke",
     "description": "Start an action (start a service, run a scenario or cycle, answer an approval, change "
                    "Jira). Returns the "
                    "execution, usually still running: follow it with qavector_wait. Everything is journalled.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["capability"],
                     "properties": {"capability": _ID, "input": {"type": "object"},
                                    "idempotency_key": {"type": "string", "maxLength": 256}}}},
    {"name": "qavector_get_execution",
     "description": "An execution as it is now: state, output, error.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["execution_id"],
                     "properties": {"execution_id": _ID}}},
    {"name": "qavector_wait",
     "description": "Wait up to timeout_seconds (at most 60) for an execution to end; returns it as it is then. "
                    "Call again if it is still running.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["execution_id"],
                     "properties": {"execution_id": _ID,
                                    "timeout_seconds": {"type": "number", "minimum": 0, "maximum": WAIT_SECONDS}}}},
    {"name": "qavector_events",
     "description": "An execution's steps after a cursor (0 for all): what happened, in order.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["execution_id"],
                     "properties": {"execution_id": _ID, "after": {"type": "integer", "minimum": 0}}}},
    {"name": "qavector_cancel",
     "description": "Cancel an execution you started. A run stops; a service is NOT stopped (use its stop action).",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["execution_id"],
                     "properties": {"execution_id": _ID, "reason": {"type": "string", "maxLength": 500}}}},
]

#: tool -> (host request type, how its arguments become the request's fields)
_REQUESTS = {
    "qavector_list_capabilities": ("capabilities", lambda a: {"namespace": a.get("namespace")}),
    "qavector_describe": ("describe", lambda a: {"capability": a["capability"]}),
    "qavector_read": ("read", lambda a: {"capability": a["capability"], "input": a.get("input") or {}}),
    "qavector_invoke": ("invoke", lambda a: dict({"capability": a["capability"], "input": a.get("input") or {}},
                                                 **({"idempotency_key": a["idempotency_key"]}
                                                    if a.get("idempotency_key") else {}))),
    "qavector_get_execution": ("get", lambda a: {"execution_id": a["execution_id"]}),
    "qavector_wait": ("wait", lambda a: {"execution_id": a["execution_id"],
                                         "timeout": min(float(a.get("timeout_seconds", 30)), WAIT_SECONDS)}),
    "qavector_events": ("events", lambda a: {"execution_id": a["execution_id"], "after": int(a.get("after") or 0)}),
    "qavector_cancel": ("cancel", lambda a: {"execution_id": a["execution_id"], "reason": a.get("reason")}),
}


class HostLink:
    """One authenticated connection to the host's agent socket."""

    def __init__(self, path, token):
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.connect(path)
        self._reader = self._socket.makefile("rb")
        self._next = 0
        answer = self._exchange({"type": "auth", "token": token})
        if not answer.get("ok"):
            raise PermissionError("The QAVector host refused the token.")
        self.actor = answer.get("actor", "")

    def _exchange(self, message):
        self._socket.sendall((json.dumps(message) + "\n").encode("utf-8"))
        line = self._reader.readline()
        if not line:
            raise ConnectionError("The QAVector host closed the connection.")
        return json.loads(line)

    def ask(self, kind, fields):
        self._next += 1
        return self._exchange(dict(fields, type=kind, id=self._next))

    def close(self):
        try:
            self._reader.close()
            self._socket.close()
        except OSError:
            pass


class Server:
    """MCP over newline-delimited JSON-RPC; tool calls go to ``link``."""

    def __init__(self, link, write):
        self._link, self._write = link, write

    def handle(self, message):
        """One JSON-RPC message; returns the response to write, or None for a notification."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return self._error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request")
        method, request_id = message["method"], message.get("id")
        if request_id is None:
            return None                         # notifications/initialized and the like
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "initialize":
            return self._result(request_id, {
                "protocolVersion": str(params.get("protocolVersion") or PROTOCOL_VERSION),
                "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER,
                "instructions": "QAVector's services, scenarios, cycles and integrations for this project. "
                                "List capabilities (each names what it acts on), describe one before using "
                                "it, invoke, then wait. A service's start ends when its process runs, not "
                                "when it is ready: if the result says has_criteria, wait on "
                                "services.<id>.criteria (green = ready, red = failed); without criteria read "
                                "services.<id>.output; with neither, judge for yourself and say so. "
                                "Other agents of this QAVector (Claude Code, Codex and the like) are reached "
                                "only here, never through a program on PATH: list the agents namespace, then "
                                "agents.<id>.delegate hands one a task (it runs once, may wait for the "
                                "person's approval, and is given your QAVector access where its own "
                                "connection allows it too) and agents.<id>.message sends it a message."})
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(request_id, {"tools": TOOLS})
        if method == "tools/call":
            return self._result(request_id, self._call(params.get("name"), params.get("arguments") or {}))
        return self._error(request_id, -32601, "Method not found")

    def _call(self, name, arguments):
        route = _REQUESTS.get(name)
        if route is None or not isinstance(arguments, dict):
            return self._content({"ok": False, "error": {"code": "unknown_tool", "message": "No such tool."}}, True)
        kind, build = route
        try:
            fields = build(arguments)
        except (KeyError, TypeError, ValueError):
            return self._content({"ok": False, "error": {"code": "invalid_input",
                                                         "message": "Missing or malformed arguments."}}, True)
        try:
            reply = self._link.ask(kind, fields)
        except (OSError, ConnectionError, ValueError):
            return self._content({"ok": False, "error": {"code": "host_unavailable",
                                                         "message": "QAVector is not reachable."}}, True)
        reply = {key: value for key, value in reply.items() if key not in ("type", "id")}
        return self._content(reply, not reply.get("ok", False))

    @staticmethod
    def _content(value, is_error):
        return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=1)}],
                "isError": bool(is_error)}

    @staticmethod
    def _result(request_id, result):
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id, code, message):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def serve(self, lines):
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except ValueError:
                self._write(self._error(None, -32700, "Parse error"))
                continue
            answer = self.handle(message)
            if answer is not None:
                self._write(answer)


def main(argv):
    """``--qcr-mcp --socket=PATH --token-file=PATH``. Returns the exit code."""
    options = dict(arg.split("=", 1) for arg in argv if "=" in arg)
    path, token_file = options.get("--socket"), options.get("--token-file")
    if not path or not token_file:
        sys.stderr.write("--qcr-mcp needs --socket=PATH and --token-file=PATH\n")
        return 2
    try:
        with open(token_file, encoding="utf-8") as handle:
            token = handle.read().strip()
        link = HostLink(path, token)
    except (OSError, PermissionError, ConnectionError, ValueError) as exc:
        sys.stderr.write("QAVector MCP: %s\n" % exc)
        return 1
    out = sys.stdout

    def write(message):
        out.write(json.dumps(message, ensure_ascii=False) + "\n")
        out.flush()
    try:
        Server(link, write).serve(sys.stdin)
    finally:
        link.close()
    return 0


def server_command(socket_path, token_file):
    """How an agent's CLI starts this server: the core itself - the installed
    executable, or this checkout's launcher under the running interpreter - with
    the socket's and the token file's paths. Never the token."""
    if getattr(sys, "frozen", False):
        command = [sys.executable]
    else:
        command = [sys.executable, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                "session_launcher.py")]
    return command + ["--qcr-mcp", "--socket=" + socket_path, "--token-file=" + token_file]
