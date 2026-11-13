"""A deterministic exec/app-server process. Never contacts a model."""

import sys


def fake_codex(tmp_path):
    path = tmp_path / "codex"
    path.write_text("#!%s\n" % sys.executable + r'''
import json, os, sys, time
args = sys.argv[1:]
def say(value):
    print(json.dumps(value), flush=True)
if args == ["login", "status"]:
    print("Logged in using ChatGPT", file=sys.stderr)
    sys.exit(0)
if args[0] == "app-server":
    for line in sys.stdin:
        request = json.loads(line)
        if request["method"] == "initialize":
            say({"id": request["id"], "result": {}})
        elif request["method"] == "model/list":
            say({"id": request["id"], "result": {"data": [{"model": "fixture-model", "displayName": "Fixture",
                "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}], "nextCursor": None}})
    sys.exit(0)
assert args[0] == "exec" and "--json" in args and args[-1] == "-"
message = sys.stdin.read()
thread = args[args.index("resume") + 1] if "resume" in args else "0199a213-81c0-7800-8aa1-bbab2a035a53"
if os.environ.get("FAKE_CODEX_LOG"):
    with open(os.environ["FAKE_CODEX_LOG"], "a") as log:
        log.write(json.dumps({"argv": args, "message": message, "cwd": os.getcwd()}) + "\n")
say({"type": "thread.started", "thread_id": thread})
if "MCP_LIST" in message:
    import subprocess
    config = next(arg for arg in args if arg.startswith("mcp_servers="))
    prefix = "mcp_servers={qavector={command="
    assert config.startswith(prefix)
    command, end = json.JSONDecoder().raw_decode(config[len(prefix):])
    remaining = config[len(prefix) + end:]
    assert remaining.startswith(",args=")
    arguments, end = json.JSONDecoder().raw_decode(remaining[len(",args="):])
    assert remaining[len(",args=") + end:] == ",required=true}}"
    requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                    "name": "qavector_list_capabilities", "arguments": {"namespace": "services"}}}]
    result = subprocess.run([command, *arguments],
        input="\n".join(json.dumps(row) for row in requests) + "\n", capture_output=True, text=True, timeout=10)
    result = json.loads(result.stdout.splitlines()[1])["result"]
    if result.get("isError"):
        say({"type": "turn.failed", "error": {"message": "MCP access refused"}})
        sys.exit(1)
    message = result["content"][0]["text"]
if "HANG" in message:
    time.sleep(600)
if "CRASH" in message:
    sys.exit(3)
if "REFUSE" in message:
    say({"type": "turn.failed", "error": {"message": "Unavailable feature"}})
    sys.exit(1)
say({"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "Answer: " + message}})
say({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 7}})
''')
    path.chmod(0o700)
    return str(path)
