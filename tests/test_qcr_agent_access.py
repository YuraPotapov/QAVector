"""An agent's whole way to QAVector (ADR-003 6a): MCP -> socket -> host -> the GUI's dispatch.

The real ``--qcr-mcp`` process speaks MCP on its stdio; the host is in-process
with its socket in the test's folder; the test plays the GUI - it issues the
agent's grants and token, and answers the dispatch as the supervisor would.
"""

import json
import os
import subprocess
import sys
import time

import pytest

from qcr.agent_socket import digest
from qcr.host import Host
from test_qcr_host import USER, Timers, WEB, row, snapshot

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "a" * 64
AGENT = "agent:claude/s1"
AGENT_GRANTS = [{"permission": p} for p in ("services.read", "services.start", "executions.read")]


class World:
    def __init__(self, tmp_path):
        self.out = []
        self.host = Host("p", str(tmp_path / "e.sqlite"), lambda line: self.out.append(json.loads(line)),
                         schedule=Timers())
        self.socket = self.host.open_agent_socket(str(tmp_path / "sock"))
        self.feed({"type": "snapshot", "snapshot": snapshot(row(WEB, "web"))})
        self.feed({"type": "hello", "protocol": 1, "actors": {"gui-user": USER}})
        self.token_file = tmp_path / "token"
        self.token_file.write_text(TOKEN)
        os.chmod(self.token_file, 0o600)

    def feed(self, message):
        self.host.feed(json.dumps(message))

    def grant(self, actor=AGENT, grants=None, token=TOKEN, expires_in=60):
        self.feed({"type": "actor", "id": 1, "actor": actor, "grants": AGENT_GRANTS if grants is None else grants})
        self.feed({"type": "agent_token", "id": 2, "actor": actor, "token_sha256": digest(token),
                   "expires_at": time.time() + expires_in})

    def of(self, kind):
        return [m for m in self.out if m.get("type") == kind]


class Mcp:
    """The real MCP server process, driven one JSON-RPC message at a time."""

    def __init__(self, world):
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "session_launcher.py"), "--qcr-mcp", "--socket=" + world.socket,
             "--token-file=" + str(world.token_file)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, cwd=ROOT)
        self._id = 0

    def call(self, method, params=None):
        self._id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method,
                                          "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, self.proc.stderr.read()
        return json.loads(line)

    def tool(self, name, **arguments):
        result = self.call("tools/call", {"name": name, "arguments": arguments})["result"]
        return result["isError"], json.loads(result["content"][0]["text"])

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=20)


@pytest.fixture
def world(tmp_path):
    one = World(tmp_path)
    yield one
    one.host.close()


def test_an_agent_starts_a_service_and_waits_for_it_through_mcp(world):
    world.grant()
    mcp = Mcp(world)
    try:
        assert mcp.call("initialize", {"protocolVersion": "2025-06-18"})["result"]["serverInfo"]["name"] == "qavector"
        failed, listed = mcp.tool("qavector_list_capabilities", namespace="services")
        ids = [cap["capability_id"] for cap in listed["capabilities"]]
        assert not failed and "services.%s.start" % WEB in ids
        assert "services.%s.stop" % WEB not in ids, "the agent sees only what it was granted"
        failed, described = mcp.tool("qavector_describe", capability="services.%s.start" % WEB)
        assert described["capability"]["input_schema"]["properties"]["wait_ready"] == {"type": "boolean"}

        failed, started = mcp.tool("qavector_invoke", capability="services.%s.start" % WEB,
                                   input={"wait_ready": False})
        execution_id = started["execution"]["execution_id"]
        dispatch = world.of("dispatch")[-1]
        assert dispatch["actor"] == AGENT and dispatch["op"] == "start"
        world.feed({"type": "owner", "execution_id": execution_id, "event": "accepted"})
        world.feed({"type": "owner", "execution_id": execution_id, "event": "succeeded",
                    "output": {"service_status": "running", "readiness": "not_checked"}})
        failed, waited = mcp.tool("qavector_wait", execution_id=execution_id, timeout_seconds=5)
        assert not failed and waited["execution"]["state"] == "succeeded"
        assert waited["execution"]["actor_id"] == AGENT
    finally:
        mcp.close()


def test_what_the_agent_was_not_granted_it_cannot_do_or_see(world):
    world.grant(grants=[{"permission": "services.read"}])
    mcp = Mcp(world)
    try:
        failed, answer = mcp.tool("qavector_invoke", capability="services.%s.start" % WEB, input={})
        assert failed and answer["error"]["code"] == "not_found"
        assert world.of("dispatch") == []
        # Someone else's execution is not there for it either.
        gui = world.host.handle_agent("gui-user", {"type": "invoke", "capability": "services.%s.start" % WEB,
                                                   "input": {}})
        failed, seen = mcp.tool("qavector_get_execution", execution_id=gui["execution"]["execution_id"])
        assert failed and seen["error"]["code"] == "not_found"
    finally:
        mcp.close()


def test_the_gui_s_own_messages_are_not_an_agent_s_to_send(world):
    world.grant()
    for kind in ("snapshot", "actor", "agent_token", "owner", "hello", "revoke", "shutdown"):
        answer = world.host.handle_agent(AGENT, {"type": kind, "actor": "gui-user", "grants": USER})
        assert answer["ok"] is False and answer["error"]["code"] == "invalid_message", kind
    assert world.host._callers[AGENT] is not None and not world.host.done


def test_a_token_only_for_an_agent_already_issued(world):
    world.feed({"type": "agent_token", "id": 3, "actor": "gui-user", "token_sha256": digest("x"),
                "expires_at": time.time() + 60})
    world.feed({"type": "agent_token", "id": 4, "actor": "agent:nobody", "token_sha256": digest("x"),
                "expires_at": time.time() + 60})
    assert [m["error"]["code"] for m in world.out if m.get("id") in (3, 4)] == ["invalid_message", "invalid_message"]


def test_revoking_the_agent_ends_its_access_at_once(world):
    world.grant()
    mcp = Mcp(world)
    try:
        assert not mcp.tool("qavector_list_capabilities")[0]
        world.feed({"type": "revoke", "id": 5, "actor": AGENT})
        failed, answer = mcp.tool("qavector_list_capabilities")
        assert failed and answer["error"]["code"] in ("authorization_denied", "host_unavailable")
    finally:
        mcp.close()


def test_an_expired_token_opens_nothing(world):
    world.grant(expires_in=-1)
    done = subprocess.run([sys.executable, os.path.join(ROOT, "session_launcher.py"), "--qcr-mcp",
                           "--socket=" + world.socket, "--token-file=" + str(world.token_file)],
                          input="", capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert done.returncode == 1 and "refused the token" in done.stderr and TOKEN not in done.stderr


def test_the_project_s_jira_reads_are_there_for_a_granted_agent(world, tmp_path):
    from integrations import store
    paths = (str(tmp_path / "integrations.json"), str(tmp_path / "s.json"), str(tmp_path / "env.json"))
    store.upsert({"name": "Product team", "site": "https://jira.example.invalid", "email": "x@example.invalid"},
                 paths[0], paths[1], new_token="tok")
    world.host.open_integrations(*paths)
    world.grant(grants=[{"permission": "integrations.jira.read"}, {"permission": "integrations.jira.issue"}])
    listed = world.host.handle_agent(AGENT, {"type": "capabilities", "namespace": "integrations"})
    assert [c["capability_id"] for c in listed["capabilities"]] == ["integrations.jira.product-team.issue"]


def test_an_agent_can_tell_the_services_apart_and_read_a_status(world):
    """2026-11-13, the user's first live run: the list gave ids only and status was not a read,
    so the agent could not tell which service was Odoo - and rightly started nothing."""
    world.grant()
    listed = world.host.handle_agent(AGENT, {"type": "capabilities", "namespace": "services"})
    start = next(c for c in listed["capabilities"] if c["capability_id"] == "services.%s.start" % WEB)
    assert start["resource"] == {"id": WEB, "type": "service", "name": "web", "aliases": ["Stand/web"]}
    status = next(c for c in listed["capabilities"] if c["capability_id"].endswith(".status"))
    assert status["read_only"] is True
    answer = world.host.handle_agent(AGENT, {"type": "read", "capability": "services.%s.status" % WEB})
    assert answer == {"ok": True, "result": "stopped"}
    assert world.host.handle_agent(AGENT, {"type": "invoke", "capability": "services.%s.status" % WEB,
                                           "input": {}})["error"]["code"] == "not_invocable"


# -- readiness the agent's way: criteria to wait on, output to read --------------------------
import threading as _threading                                     # noqa: E402

from qcr import host as host_module                                # noqa: E402


def with_criteria(world, criteria):
    one = row(WEB, "web", status="running")
    if criteria is not None:
        one["criteria"] = criteria
    world.feed({"type": "snapshot", "snapshot": snapshot(one)})


RUNNING = {"name": "Running", "color": "green", "met": False}
CRASHED = {"name": "Crashed", "color": "red", "met": False}


def criteria_read(world, **given):
    return world.host.handle_agent(AGENT, {"type": "read", "capability": "services.%s.criteria" % WEB,
                                           "input": given})


@pytest.mark.parametrize("criteria, settled", [
    (None, "none"), ([], "none"), ([RUNNING, CRASHED], "waiting"),
    ([dict(RUNNING, met=True), CRASHED], "ready"), ([RUNNING, dict(CRASHED, met=True)], "failed"),
    ([CRASHED, {"name": "Note", "color": "blue", "met": True}], "no_ready_signal"),
])
def test_the_criteria_say_whether_there_is_something_to_wait_for(world, criteria, settled):
    world.grant()
    with_criteria(world, criteria)
    answer = criteria_read(world)
    assert answer["ok"] and answer["result"]["settled"] == settled
    assert answer["result"]["has_criteria"] is bool(criteria)


def test_a_wait_on_the_criteria_ends_when_the_green_ones_light(world):
    world.grant()
    with_criteria(world, [RUNNING, CRASHED])
    _threading.Timer(0.3, lambda: with_criteria(world, [dict(RUNNING, met=True), CRASHED])).start()
    started = time.monotonic()
    answer = criteria_read(world, wait_seconds=10)
    assert answer["result"]["settled"] == "ready" and time.monotonic() - started < 5


def test_a_wait_on_the_criteria_ends_at_its_limit_still_waiting(world):
    world.grant()
    with_criteria(world, [RUNNING])
    started = time.monotonic()
    assert criteria_read(world, wait_seconds=0.5)["result"]["settled"] == "waiting"
    assert time.monotonic() - started < 3
    assert criteria_read(world, wait_seconds=61)["error"]["code"] == "invalid_input"


def test_the_output_is_asked_of_the_window_that_holds_it(world):
    world.grant()

    def window_answers():
        asked = [m for m in world.out if m.get("type") == "owner_read"]
        world.feed({"type": "owner_read_reply", "id": asked[-1]["id"],
                    "result": {"lines": ["booting", "HTTP service (werkzeug) running on 0.0.0.0:8069"],
                               "service_status": "running"}})
    _threading.Timer(0.2, window_answers).start()
    answer = world.host.handle_agent(AGENT, {"type": "read", "capability": "services.%s.output" % WEB,
                                             "input": {"lines": 20}})
    assert answer["ok"] and answer["result"]["lines"][-1].startswith("HTTP service")
    asked = [m for m in world.out if m.get("type") == "owner_read"][-1]
    assert asked["op"] == "service.output" and asked["resource_id"] == WEB and asked["lines"] == 20


def test_a_window_that_does_not_answer_is_owner_unavailable(world, monkeypatch):
    world.grant()
    monkeypatch.setattr(host_module, "OWNER_READ_SECONDS", 0.2)
    answer = world.host.handle_agent(AGENT, {"type": "read", "capability": "services.%s.output" % WEB,
                                             "input": {}})
    assert answer["error"]["code"] == "owner_unavailable"


def test_without_the_read_grant_neither_criteria_nor_output(world):
    world.grant(grants=[{"permission": "services.start"}])
    for op in ("criteria", "output"):
        answer = world.host.handle_agent(AGENT, {"type": "read", "capability": "services.%s.%s" % (WEB, op),
                                                 "input": {}})
        assert answer["error"]["code"] == "not_found", op
    assert [m for m in world.out if m.get("type") == "owner_read"] == []
