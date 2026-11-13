"""The QAVector MCP server (ADR-003 6a.2): MCP on stdio, everything to the host's socket."""

import json
import os
import subprocess
import sys
import time

import pytest

from qcr.agent_socket import AgentGateway, TokenBook, digest
from qcr.mcp_server import TOOLS, HostLink, Server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "k" * 64


@pytest.fixture
def gateway(tmp_path):
    asked = []

    def handle(actor, message):
        asked.append((actor, message))
        if message["type"] == "invoke":
            return {"ok": True, "execution": {"execution_id": "e1", "state": "queued"}}
        if message["type"] == "capabilities":
            return {"ok": True, "capabilities": [{"capability_id": "services.web.start"}]}
        return {"ok": False, "error": {"code": "not_found", "message": "Capability is not available."}}
    tokens = TokenBook()
    tokens.add("agent:claude/s1", digest(TOKEN), time.time() + 60)
    one = AgentGateway(handle, tokens, str(tmp_path / "qv"), name="s.sock")
    one.asked = asked
    yield one
    one.close()


def rpc(server, method, params=None, request_id=1):
    return server.handle({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})


def test_the_handshake_and_the_tool_list(gateway):
    server = Server(HostLink(gateway.path, TOKEN), lambda message: None)
    hello = rpc(server, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}})["result"]
    assert hello["protocolVersion"] == "2025-03-26" and hello["capabilities"]["tools"] == {"listChanged": False}
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = [tool["name"] for tool in rpc(server, "tools/list")["result"]["tools"]]
    assert names == ["qavector_list_capabilities", "qavector_describe", "qavector_read", "qavector_invoke",
                     "qavector_get_execution", "qavector_wait", "qavector_events", "qavector_cancel"]
    assert rpc(server, "resources/list")["error"]["code"] == -32601


def test_a_tool_call_goes_to_the_host_as_the_token_s_actor(gateway):
    server = Server(HostLink(gateway.path, TOKEN), lambda message: None)
    result = rpc(server, "tools/call", {"name": "qavector_invoke", "arguments": {
        "capability": "services.web.start", "input": {"wait_ready": False}}})["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"])["execution"]["execution_id"] == "e1"
    actor, message = gateway.asked[-1]
    assert actor == "agent:claude/s1"
    assert message["type"] == "invoke" and message["capability"] == "services.web.start"


def test_a_refusal_is_an_error_result_not_a_crash(gateway):
    server = Server(HostLink(gateway.path, TOKEN), lambda message: None)
    result = rpc(server, "tools/call", {"name": "qavector_describe", "arguments": {"capability": "x.y.z"}})["result"]
    assert result["isError"] is True and json.loads(result["content"][0]["text"])["error"]["code"] == "not_found"
    bad = rpc(server, "tools/call", {"name": "qavector_wait", "arguments": {}})["result"]
    assert bad["isError"] is True and json.loads(bad["content"][0]["text"])["error"]["code"] == "invalid_input"
    unknown = rpc(server, "tools/call", {"name": "rm_rf", "arguments": {}})["result"]
    assert json.loads(unknown["content"][0]["text"])["error"]["code"] == "unknown_tool"


def test_wait_is_capped_at_a_minute(gateway):
    server = Server(HostLink(gateway.path, TOKEN), lambda message: None)
    rpc(server, "tools/call", {"name": "qavector_wait", "arguments": {"execution_id": "e1", "timeout_seconds": 59}})
    assert gateway.asked[-1][1]["timeout"] == 59


def test_a_wrong_token_never_gets_a_link(gateway):
    with pytest.raises(PermissionError):
        HostLink(gateway.path, "nope")


def test_the_real_process_speaks_mcp_on_stdio_and_keeps_the_token_off_its_command_line(gateway, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN)
    os.chmod(token_file, 0o600)
    argv = [sys.executable, os.path.join(ROOT, "session_launcher.py"), "--qcr-mcp",
            "--socket=" + gateway.path, "--token-file=" + str(token_file)]
    assert TOKEN not in " ".join(argv)
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": "qavector_list_capabilities", "arguments": {"namespace": "services"}}}]
    done = subprocess.run(argv, input="\n".join(json.dumps(one) for one in lines) + "\n", capture_output=True,
                          text=True, timeout=60, cwd=ROOT)
    answers = [json.loads(line) for line in done.stdout.splitlines()]
    assert done.returncode == 0, done.stderr
    assert [a["id"] for a in answers] == [1, 2]
    assert json.loads(answers[1]["result"]["content"][0]["text"])["capabilities"][0]["capability_id"] == \
        "services.web.start"
    assert TOKEN not in done.stdout + done.stderr


def test_the_tool_schemas_are_closed():
    for tool in TOOLS:
        assert tool["inputSchema"]["additionalProperties"] is False, tool["name"]
