"""An agent turn given QAVector (ADR-003 6a.3): the window's side.

A turn of a connection whose "QAVector access" grants something gets an actor
of its own, the grants those switches name, and a token the host knows only by
its digest - in a 0600 file whose path, never the token, goes into the turn.
The turn's end revokes it. A live test then has the real host take the token
and answer the real MCP server for that actor.
"""

import hashlib
import json
import os
import stat
import subprocess
import sys
import time
import types

import pytest

from cms_gui import qcrhost
from cms_gui.services import ServiceSupervisor

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORE_PYTHON = os.path.join(ROOT, ".venv", "bin", "python")


def ready_host(tmp_path):
    host = qcrhost.QcrHost(ServiceSupervisor())
    host.state, host.socket_path = qcrhost.READY, str(tmp_path / "qv" / "s.sock")
    os.makedirs(tmp_path / "qv", mode=0o700)
    host._proc = types.SimpleNamespace(write=lambda data: host.sent.append(json.loads(bytes(data))))
    host.sent = []
    return host


CONNECTION = {"id": "claude-code", "name": "Claude", "kind": "claude",
              "qavector_access": {"services": True, "approvals": True}}


def test_a_turn_is_given_its_actor_its_grants_and_a_token_by_digest(qapp, tmp_path):
    host = ready_host(tmp_path)
    access = host.grant_agent(CONNECTION, "s-1")
    assert access["actor"] == "agent:claude-code/s-1" and access["socket"] == host.socket_path
    actor, token = host.sent
    assert actor["type"] == "actor" and actor["actor"] == "agent:claude-code/s-1"
    assert sorted(g["permission"] for g in actor["grants"]) == [
        "cycles.approve", "cycles.read", "services.read", "services.restart", "services.start", "services.stop"]
    with open(access["token_file"]) as handle:
        secret = handle.read()
    assert token["type"] == "agent_token" and token["token_sha256"] == hashlib.sha256(secret.encode()).hexdigest()
    assert secret not in json.dumps(host.sent), "only the digest goes to the host"
    assert stat.S_IMODE(os.stat(access["token_file"]).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(access["token_file"])).st_mode) == 0o700
    host.end_agent(access)
    assert host.sent[-1] == {"type": "revoke", "id": host.sent[-1]["id"], "actor": "agent:claude-code/s-1"}
    assert not os.path.exists(access["token_file"])


@pytest.mark.parametrize("connection, state, socket", [
    ({"id": "c", "qavector_access": {}}, qcrhost.READY, "/x.sock"),                       # nothing switched on
    ({"id": "c", "qavector_access": {"services": "yes"}}, qcrhost.READY, "/x.sock"),       # only True turns it on
    (CONNECTION, qcrhost.STARTING, "/x.sock"),                                            # host not ready
    (CONNECTION, qcrhost.READY, ""),                                                      # no socket
    (None, qcrhost.READY, "/x.sock"),
])
def test_no_access_is_given_without_all_it_needs(qapp, connection, state, socket):
    host = qcrhost.QcrHost(ServiceSupervisor())
    host.state, host.socket_path = state, socket
    host._proc = types.SimpleNamespace(write=lambda data: pytest.fail("nothing is sent"))
    assert host.grant_agent(connection, "s-1") is None


def test_the_chat_puts_paths_not_the_token_into_the_turn_and_ends_access_with_it(qapp, tmp_path):
    from PySide6.QtCore import QObject, Signal
    from cms_gui.pages import agentchat
    from cms_gui.settings import Settings
    made = []

    class Turn(QObject):
        event = Signal(dict)
        answered = Signal(dict)

        def __init__(self, core, session_id, document, parent=None):
            super().__init__(parent)
            self.document = document
            made.append(self)

        def start(self):
            pass
    host = ready_host(tmp_path)
    tab = agentchat.ChatTab(Settings())
    tab.turn_factory = Turn
    tab.core = lambda: object()
    tab.set_connections([CONNECTION])       # as the page gives them - not poked into a private field
    previous = agentchat.ChatTab.qcr_access
    agentchat.ChatTab.qcr_access = host
    try:
        tab._start({"id": "s-1", "connection": "claude-code"}, "start the shop", document={"message": "start"})
        document = made[0].document
        assert set(document["qavector"]) == {"socket", "token_file"}
        token_file = document["qavector"]["token_file"]
        with open(token_file) as handle:
            assert handle.read() not in json.dumps(document)
        made[0].answered.emit({"ok": True})
        assert host.sent[-1]["type"] == "revoke" and not os.path.exists(token_file)
    finally:
        agentchat.ChatTab.qcr_access = previous
        tab.close()


@pytest.mark.skipif(not os.path.exists(CORE_PYTHON), reason="needs the core's .venv")
def test_the_real_host_takes_the_token_and_answers_the_real_mcp_server(qapp, tmp_path):
    supervisor = ServiceSupervisor()
    host = qcrhost.QcrHost(supervisor)
    host.start([CORE_PYTHON, os.path.join(ROOT, "session_launcher.py"), "--qcr-host", "--qcr-project=p",
                "--qcr-store=" + qcrhost.store_path(str(tmp_path)), "--qcr-socket-dir=" + str(tmp_path / "sock"),
                "--integrations-file=" + str(tmp_path / "integrations.json")], "p")
    try:
        end = time.monotonic() + 20
        while host.state != qcrhost.READY and time.monotonic() < end:
            qapp.processEvents()
            time.sleep(0.01)
        assert host.socket_path.startswith(str(tmp_path / "sock"))
        access = host.grant_agent(CONNECTION, "s-1")
        for _ in range(20):
            qapp.processEvents()
            time.sleep(0.01)
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "qavector_list_capabilities", "arguments": {}}}]
        done = subprocess.run([CORE_PYTHON, os.path.join(ROOT, "session_launcher.py"), "--qcr-mcp",
                               "--socket=" + access["socket"], "--token-file=" + access["token_file"]],
                              input="\n".join(json.dumps(one) for one in lines) + "\n", capture_output=True,
                              text=True, timeout=60, cwd=ROOT)
        assert done.returncode == 0, done.stderr
        answer = json.loads(done.stdout.splitlines()[1])["result"]
        assert answer["isError"] is False and json.loads(answer["content"][0]["text"])["ok"] is True
        host.end_agent(access)
        for _ in range(20):
            qapp.processEvents()
            time.sleep(0.01)
        refused = subprocess.run([CORE_PYTHON, os.path.join(ROOT, "session_launcher.py"), "--qcr-mcp",
                                  "--socket=" + access["socket"], "--token-file=" + access["token_file"]],
                                 input="", capture_output=True, text=True, timeout=60, cwd=ROOT)
        assert refused.returncode == 1, "the turn is over: its token file is gone and its actor revoked"
    finally:
        host.stop()
        supervisor.shutdown(1000)


def test_a_turn_starts_with_qcr_off_and_with_a_connection_that_grants_nothing(qapp, tmp_path):
    """The chat's own path, through set_connections, with no access to give: the turn starts."""
    from PySide6.QtCore import QObject, Signal
    from cms_gui.pages import agentchat
    from cms_gui.settings import Settings
    started = []

    class Turn(QObject):
        event = Signal(dict)
        answered = Signal(dict)

        def __init__(self, core, session_id, document, parent=None):
            super().__init__(parent)
            self.document = document

        def start(self):
            started.append(self.document)
    previous = agentchat.ChatTab.qcr_access
    try:
        for access in (None, ready_host(tmp_path)):
            agentchat.ChatTab.qcr_access = access
            tab = agentchat.ChatTab(Settings())
            tab.turn_factory = Turn
            tab.core = lambda: object()
            tab.set_connections([{"id": "claude-code", "name": "Claude", "kind": "claude"}])
            tab._start({"id": "s-2", "connection": "claude-code"}, "hi", document={"message": "hi"})
            tab.close()
        assert started == [{"message": "hi"}, {"message": "hi"}], "no qavector key, and the turn went"
    finally:
        agentchat.ChatTab.qcr_access = previous


@pytest.mark.skipif(not os.path.exists(CORE_PYTHON), reason="needs the core's .venv")
def test_an_agent_reads_a_service_s_output_from_the_window_through_the_real_host(qapp, tmp_path):
    """The whole way an agent sees a log: MCP -> host -> owner_read -> this window -> the Console."""
    from cms_gui import servicesfile as sf
    web = "a" * 32
    supervisor = ServiceSupervisor()
    supervisor.sync([sf.ProjectRow(name="Group", extra={"qcr_id": "f" * 32}, runners=[sf.RunnerRow(
        name="Web", type="shell", settings={"command": "true"}, extra={"qcr_id": web})])])
    supervisor.service("Group", "Web")._lines.extend(["booting", "HTTP service (werkzeug) running on 0.0.0.0:8170"])
    host = qcrhost.QcrHost(supervisor)
    host.start([CORE_PYTHON, os.path.join(ROOT, "session_launcher.py"), "--qcr-host", "--qcr-project=p",
                "--qcr-store=" + qcrhost.store_path(str(tmp_path)), "--qcr-socket-dir=" + str(tmp_path / "sock"),
                "--integrations-file=" + str(tmp_path / "integrations.json")], "p")

    def pump(until, seconds=20):
        end = time.monotonic() + seconds
        while not until() and time.monotonic() < end:
            qapp.processEvents()
            time.sleep(0.01)
    try:
        pump(lambda: host.state == qcrhost.READY)
        access = host.grant_agent({"id": "claude-code", "qavector_access": {"services": True}}, "s-9")
        pump(lambda: False, 0.3)
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                     "name": "qavector_read", "arguments": {"capability": "services.%s.output" % web,
                                                            "input": {"lines": 5}}}}]
        mcp = subprocess.Popen([CORE_PYTHON, os.path.join(ROOT, "session_launcher.py"), "--qcr-mcp",
                                "--socket=" + access["socket"], "--token-file=" + access["token_file"]],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               cwd=ROOT)
        mcp.stdin.write("\n".join(json.dumps(one) for one in lines) + "\n")
        mcp.stdin.close()
        pump(lambda: mcp.poll() is not None)          # the window must keep answering while it waits
        answer = json.loads(mcp.stdout.read().splitlines()[1])["result"]
        assert answer["isError"] is False, answer
        assert json.loads(answer["content"][0]["text"])["result"]["lines"][-1].startswith("HTTP service")
        host.end_agent(access)
    finally:
        host.stop()
        supervisor.shutdown(1000)
