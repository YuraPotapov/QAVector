"""A deterministic exec/app-server process. Never contacts a model.

The app-server side speaks enough JSON-RPC for one QAVector turn. What the
message says decides the turn:

    HANG    nothing until turn/interrupt, then an interrupted turn
    CRASH   the process exits 3
    REFUSE  a failed turn
    ASK     request_user_input, then (after turn/interrupt) an interrupted turn
    PLAN    a plan item
    CMD     a command approval request (Codex could not parse it); the answer decides the row
    READCMD the same for a command Codex parsed as reading a file
    ELICIT  an MCP tool approval for the qavector server, then one for another server;
            says which were accepted
    PATCH   a file change approval request; accepted -> file changed
    MCP_LIST  calls the QAVector MCP server from its -c argument and says what it listed
    MCP_DELEGATE  through that server, delegates "Say hello" to the Claude connection,
              waits for it and says the delegated agent's answer
    WONDER  ends with a question line for whoever gave the task (QCR ADR-011) - unless
            the message is the answer to it; WONDER-ALWAYS asks after answers too

``FAKE_CODEX_LOG`` gets one line per start (argv) and per request received.
``FAKE_CODEX_MCP`` (JSON) is the mcp_servers table config/read reports.
"""

import sys


def fake_codex(tmp_path):
    path = tmp_path / "codex"
    path.write_text("#!%s\n" % sys.executable + r'''
import json, os, sys, time
args = sys.argv[1:]
WONDERED = 'I cannot tell which database the tests use.\n{"needs": "answer", "question": "Which database should the tests use?"}'
def say(value):
    print(json.dumps(value), flush=True)
def log(value):
    if os.environ.get("FAKE_CODEX_LOG"):
        with open(os.environ["FAKE_CODEX_LOG"], "a") as handle:
            handle.write(json.dumps(value) + "\n")
if args == ["login", "status"]:
    print("Logged in using ChatGPT", file=sys.stderr)
    sys.exit(0)
if args[0] == "app-server":
    log({"argv": args, "cwd": os.getcwd()})
    THREAD = "0199a213-81c0-7800-8aa1-bbab2a035a53"
    lines = iter(sys.stdin)
    def read():
        for line in lines:
            message = json.loads(line)
            if "method" in message:
                log({"method": message["method"], "params": message.get("params")})
            return message
        sys.exit(0)
    def ask(ident, method, params):
        say({"id": ident, "method": method, "params": params})
        while True:
            message = read()
            if message.get("id") == ident and "method" not in message:
                return message
    def finish(turn, status, error=None):
        say({"method": "thread/tokenUsage/updated", "params": {"threadId": THREAD, "turnId": turn,
             "tokenUsage": {"total": {"inputTokens": 1100, "cachedInputTokens": 880, "outputTokens": 77,
                                      "reasoningOutputTokens": 5, "totalTokens": 1177},
                            "last": {"inputTokens": 100, "cachedInputTokens": 80, "outputTokens": 7,
                                     "reasoningOutputTokens": 5, "totalTokens": 107},
                            "modelContextWindow": 272000}}})
        say({"method": "turn/completed", "params": {"threadId": THREAD, "turn": {
             "id": turn, "status": status, "items": [], "error": error}}})
    def wait_interrupt(turn):
        while True:
            message = read()
            if message.get("method") == "turn/interrupt":
                say({"id": message["id"], "result": {}})
                finish(turn, "interrupted")
                return
    def mcp_list():
        import subprocess
        spec = next(arg for arg in args if arg.startswith("mcp_servers.qavector="))
        prefix = "mcp_servers.qavector={command="
        assert spec.startswith(prefix), spec
        command, end = json.JSONDecoder().raw_decode(spec[len(prefix):])
        rest = spec[len(prefix) + end:]
        assert rest.startswith(",args=")
        arguments, end = json.JSONDecoder().raw_decode(rest[len(",args="):])
        assert rest[len(",args=") + end:] == ',required=true,default_tools_approval_mode="approve"}'

        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                        "name": "qavector_list_capabilities", "arguments": {"namespace": "services"}}}]
        done = subprocess.run([command, *arguments], input="\n".join(json.dumps(r) for r in requests) + "\n",
                              capture_output=True, text=True, timeout=10)
        result = json.loads(done.stdout.splitlines()[1])["result"]
        return None if result.get("isError") else result["content"][0]["text"]
    def mcp_delegate(to_codex=False):
        import subprocess
        spec = next(arg for arg in args if arg.startswith("mcp_servers.qavector="))
        prefix = "mcp_servers.qavector={command="
        command, end = json.JSONDecoder().raw_decode(spec[len(prefix):])
        rest = spec[len(prefix) + end:]
        arguments, _end = json.JSONDecoder().raw_decode(rest[len(",args="):])
        server = subprocess.Popen([command, *arguments], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        def call(ident, method, params):
            server.stdin.write(json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}) + "\n")
            server.stdin.flush()
            return json.loads(server.stdout.readline())
        def tool(ident, name, **arguments):
            reply = call(ident, "tools/call", {"name": name, "arguments": arguments})
            return json.loads(reply["result"]["content"][0]["text"])
        call(1, "initialize", {})
        started = tool(2, "qavector_invoke", capability="agents.codex.delegate" if to_codex else "agents.claude.delegate",
                       input={"goal": "Say hello", "acceptance_criteria": ["it says hello"]})
        if not started.get("ok"):
            server.stdin.close()
            return None
        execution = started["execution"]
        for ident in range(3, 40):
            if execution["state"] in ("succeeded", "failed", "canceled", "timed_out"):
                break
            execution = tool(ident, "qavector_wait", execution_id=execution["execution_id"],
                             timeout_seconds=1)["execution"]
        if to_codex and execution["state"] == "succeeded":
            execution = tool(40, "qavector_invoke", capability="agents.tasks.reply",
                             input={"task": execution["execution_id"], "answer": "Review the follow-up."})["execution"]
            for ident in range(41, 80):
                if execution["state"] in ("succeeded", "failed", "canceled", "timed_out"):
                    break
                execution = tool(ident, "qavector_wait", execution_id=execution["execution_id"],
                                 timeout_seconds=1)["execution"]
        server.stdin.close()
        server.wait(timeout=10)
        return "delegated %s: %s" % (execution["state"], (execution.get("output") or {}).get("answer", ""))
    turns = 0
    while True:
        message = read()
        method, ident, params = message.get("method"), message.get("id"), message.get("params") or {}
        if method == "initialize":
            say({"id": ident, "result": {"userAgent": "fake", "codexHome": "/nowhere"}})
        elif method == "initialized":
            pass
        elif method == "config/read":
            say({"id": ident, "result": {"config": {"mcp_servers": json.loads(os.environ.get("FAKE_CODEX_MCP", "{}"))}}})
        elif method == "skills/extraRoots/set":
            say({"id": ident, "result": {}})
        elif method == "model/list":
            say({"id": ident, "result": {"data": [{"model": "fixture-model", "displayName": "Fixture",
                "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}], "nextCursor": None}})
        elif method == "account/rateLimits/read":
            primary = {"usedPercent": 34, "windowDurationMins": 300, "resetsAt": 1795200000}
            secondary = {"usedPercent": 48, "windowDurationMins": 10080, "resetsAt": None}
            default = {"limitId": "codex", "primary": primary, "secondary": secondary}
            extra = {"limitId": "extra", "limitName": "Example", "primary": primary}
            say({"id": ident, "result": {"rateLimits": default, "rateLimitsByLimitId": {
                "codex": default, "extra": extra}}})
        elif method in ("thread/start", "thread/resume"):
            if method == "thread/resume":
                THREAD = params["threadId"]
            else:
                say({"method": "thread/started", "params": {"thread": {"id": THREAD}}})
            say({"id": ident, "result": {"thread": {"id": THREAD}, "model": params.get("model") or "fixture-default"}})
        elif method == "turn/start":
            turns += 1
            turn = "turn-%d" % turns
            text = params["input"][0]["text"]
            say({"id": ident, "result": {"turn": {"id": turn, "status": "inProgress", "items": []}}})
            say({"method": "turn/started", "params": {"threadId": THREAD, "turn": {"id": turn}}})
            if "CRASH" in text:
                sys.exit(3)
            if "HANG" in text:
                wait_interrupt(turn)
                continue
            if "REFUSE" in text:
                finish(turn, "failed", {"message": "Unavailable feature"})
                continue
            if "ASK" in text:
                say({"id": "q-1", "method": "item/tool/requestUserInput", "params": {
                    "threadId": THREAD, "turnId": turn, "itemId": "ask-1", "isBlocking": True,
                    "questions": [{"id": "db", "header": "Database", "question": "Which database?",
                                   "options": [{"label": "Postgres", "description": "p"},
                                               {"label": "SQLite", "description": "s"}]}]}})
                wait_interrupt(turn)
                continue
            if "PLAN" in text:
                say({"method": "item/completed", "params": {"threadId": THREAD, "turnId": turn,
                     "item": {"type": "plan", "id": "plan-1", "text": "1. Change a.py\n2. Test"}}})
            if "ELICIT" in text:
                said = []
                for n, server in enumerate(("qavector", "elsewhere")):
                    answer = ask("e-%d" % n, "mcpServer/elicitation/request", {
                        "threadId": THREAD, "turnId": turn, "serverName": server, "mode": "form",
                        "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_name": "tool"},
                        "message": "Allow?", "requestedSchema": {"type": "object", "properties": {}}})
                    said.append("%s=%s" % (server, answer["result"]["action"]))
                text = " ".join(said)
            for keyword, ident, command, actions in (
                    ("READCMD", "cmd-2", "cat a.py", [{"type": "read", "command": "cat a.py", "name": "a.py", "path": "a.py"}]),
                    ("CMD", "cmd-1", "rm -rf build", [{"type": "unknown", "command": "rm -rf build"}])):
                if keyword not in text.split():
                    continue
                item = {"type": "commandExecution", "id": ident, "command": command, "status": "inProgress"}
                say({"method": "item/started", "params": {"threadId": THREAD, "turnId": turn, "item": item}})
                answer = ask("a-" + ident, "item/commandExecution/requestApproval",
                             {"threadId": THREAD, "turnId": turn, "itemId": ident, "command": command,
                              "commandActions": actions})
                ran = answer["result"]["decision"] == "accept"
                say({"method": "item/completed", "params": {"threadId": THREAD, "turnId": turn, "item": dict(
                    item, status="completed" if ran else "declined", exitCode=0 if ran else None)}})
            if "PATCH" in text:
                item = {"type": "fileChange", "id": "patch-1", "status": "inProgress",
                        "changes": [{"path": os.path.join(os.getcwd(), "a.py"), "kind": {"type": "add"}, "diff": ""}]}
                say({"method": "item/started", "params": {"threadId": THREAD, "turnId": turn, "item": item}})
                answer = ask("a-2", "item/fileChange/requestApproval",
                             {"threadId": THREAD, "turnId": turn, "itemId": "patch-1"})
                applied = answer["result"]["decision"] == "accept"
                if applied:
                    with open("a.py", "w") as handle:
                        handle.write("print('hi')\n")
                say({"method": "item/completed", "params": {"threadId": THREAD, "turnId": turn, "item": dict(
                    item, status="completed" if applied else "declined")}})
            if "MCP_DELEGATE" in text:
                said = mcp_delegate(to_codex="MCP_DELEGATE_CODEX" in text)
                if said is None:
                    finish(turn, "failed", {"message": "delegation refused"})
                    continue
                text = said
            if "MCP_LIST" in text:
                listed = mcp_list()
                if listed is None:
                    finish(turn, "failed", {"message": "MCP access refused"})
                    continue
                text = listed
            if "WONDER-ALWAYS" in text or ("WONDER" in text and "answers your question" not in text):
                text = WONDERED
            say({"method": "item/completed", "params": {"threadId": THREAD, "turnId": turn,
                 "item": {"type": "agentMessage", "id": "m-%d" % turns, "text": "Answer: " + text}}})
            finish(turn, "completed")
        elif ident is not None and method:
            say({"id": ident, "error": {"code": -32601, "message": "fake does not know " + method}})
assert args[0] == "exec" and "--json" in args and args[-1] == "-"
message = sys.stdin.read()
thread = args[args.index("resume") + 1] if "resume" in args else "0199a213-81c0-7800-8aa1-bbab2a035a53"
log({"argv": args, "message": message, "cwd": os.getcwd()})
say({"type": "thread.started", "thread_id": thread})
if "HANG" in message:
    time.sleep(600)
if "CRASH" in message:
    sys.exit(3)
if "REFUSE" in message:
    say({"type": "turn.failed", "error": {"message": "Unavailable feature"}})
    sys.exit(1)
if "WONDER-ALWAYS" in message or ("WONDER" in message and "answers your question" not in message):
    message = WONDERED
say({"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "Answer: " + message}})
say({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 7}})
''')
    path.chmod(0o700)
    return str(path)
