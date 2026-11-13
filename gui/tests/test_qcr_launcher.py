"""The launcher's status, output and Stop for QCR (ADR-004 L.1): the main window's side.

The window's own methods run on a stand-in window (a QObject, as the waiting
read's timers need one), with the launcher feed fed by hand.
"""

import functools
import time
import types

from PySide6.QtCore import QObject

from cms_gui.launcherfeed import LauncherFeed
from cms_gui.main_window import MainWindow


class Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *args, **kwargs: self.calls.append((name, args, kwargs))


class Fake(QObject):
    pass


def window(running=True):
    fake, host, process = Fake(), Recorder(), Recorder()
    process.is_running = lambda: running
    host.actor_of = lambda execution_id: "agent:claude/s1"
    lines = []
    fake.__dict__.update(
        qcr_host=host, process=process, launcher_feed=LauncherFeed(), _launcher_stops=[], _stopping=False,
        log=types.SimpleNamespace(append=lines.append), lines=lines,
        run_state=types.SimpleNamespace(cycle_running=False, cycle=None),
        status_right=types.SimpleNamespace(setText=lambda text: None))
    for name in ("_qcr_launcher_status", "_qcr_launcher_output", "_qcr_launcher_stop", "_qcr_launcher_ended",
                 "_stop_running"):
        setattr(fake, name, functools.partial(getattr(MainWindow, name), fake))
    return fake, host, process


def owner_events(host):
    return [(args[1], kwargs) for name, args, kwargs in host.calls if name == "owner"]


def pump(qapp, until, seconds=3.0):
    end = time.monotonic() + seconds
    while not until() and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)


def test_status_is_the_feed_s(qapp):
    fake, _host, _process = window()
    fake.launcher_feed.begin("--run-tests=login", "flows")
    replies = []
    fake._qcr_launcher_status({}, replies.append)
    assert replies[0]["running"] is True and replies[0]["run"]["label"] == "--run-tests=login"


def test_output_with_something_new_answers_at_once(qapp):
    fake, _host, _process = window()
    fake.launcher_feed.log({"text": "attached"})
    replies = []
    fake._qcr_launcher_output({"after": 0, "limit": 10, "wait_seconds": 30}, replies.append)
    assert replies and replies[0]["entries"][0]["text"] == "attached"


def test_a_waiting_read_answers_with_the_burst_that_woke_it(qapp):
    fake, _host, _process = window()
    replies = []
    fake._qcr_launcher_output({"after": 0, "wait_seconds": 30}, replies.append)
    assert replies == []
    fake.launcher_feed.log({"text": "one"})
    fake.launcher_feed.log({"text": "two"})
    pump(qapp, lambda: replies)
    assert [entry["text"] for entry in replies[0]["entries"]] == ["one", "two"], "one answer for the burst"
    assert fake.launcher_feed.listeners == []
    fake.launcher_feed.log({"text": "three"})
    pump(qapp, lambda: False, 0.4)
    assert len(replies) == 1, "answered once"


def test_a_waiting_read_with_nothing_new_answers_empty_when_its_time_is_up(qapp):
    fake, _host, _process = window()
    replies = []
    started = time.monotonic()
    fake._qcr_launcher_output({"after": 0, "wait_seconds": 0.3}, replies.append)
    pump(qapp, lambda: replies)
    assert replies[0]["entries"] == [] and 0.25 < time.monotonic() - started < 2
    assert fake.launcher_feed.listeners == []


def test_stop_with_nothing_running_says_so(qapp):
    fake, host, process = window(running=False)
    fake._qcr_launcher_stop("e1", "main", {})
    assert owner_events(host) == [("succeeded", {"output": {"stopped": False, "run": None}})]
    assert process.calls == []


def test_stop_stops_any_run_writes_who_asked_and_ends_with_the_launcher(qapp):
    fake, host, process = window()
    fake.launcher_feed.begin("--run-tests=login", "flows", by="person")
    fake._qcr_launcher_stop("e1", "main", {"reason": "the user asked"})
    assert ("stop", (), {}) in process.calls, "a person's run too: which one is the conversation's call"
    assert fake.lines[-1]["text"] == "stop asked through QCR by agent:claude/s1: the user asked"
    assert owner_events(host) == [("accepted", {})]
    fake._qcr_launcher_ended(143)
    state, fields = owner_events(host)[-1]
    assert state == "succeeded" and fields["output"]["stopped"] is True
    assert fields["output"]["run"]["exit_code"] == 143 and fake._launcher_stops == []


def test_an_agent_listens_to_the_launcher_through_the_real_host_and_mcp(qapp, tmp_path):
    """MCP -> socket -> host -> owner_read -> this window's feed, with the read
    waiting until the launcher says something."""
    import hashlib
    import json
    import os
    import subprocess

    import pytest
    from PySide6.QtCore import QTimer

    from cms_gui import qcrhost
    from cms_gui.services import ServiceSupervisor
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    core_python = os.path.join(root, ".venv", "bin", "python")
    if not os.path.exists(core_python):
        pytest.skip("needs the core's .venv")
    fake, _recorder, _process = window()
    supervisor = ServiceSupervisor()
    host = qcrhost.QcrHost(supervisor)
    host.set_reader("launcher.status", fake._qcr_launcher_status)
    host.set_reader("launcher.output", fake._qcr_launcher_output)
    host.start([core_python, os.path.join(root, "session_launcher.py"), "--qcr-host", "--qcr-project=p",
                "--qcr-store=" + qcrhost.store_path(str(tmp_path)), "--qcr-socket-dir=" + str(tmp_path / "sock"),
                "--integrations-file=" + str(tmp_path / "integrations.json")], "p")
    try:
        pump(qapp, lambda: host.state == qcrhost.READY, 20)
        token = "b" * 64
        token_file = tmp_path / "token"
        token_file.write_text(token)
        os.chmod(token_file, 0o600)
        host._send({"type": "actor", "id": 901, "actor": "agent:test", "grants": [{"permission": "launcher.read"}]})
        host._send({"type": "agent_token", "id": 902, "actor": "agent:test",
                    "token_sha256": hashlib.sha256(token.encode()).hexdigest(), "expires_at": time.time() + 60})
        pump(qapp, lambda: False, 0.3)
        fake.launcher_feed.begin("--run-tests=login --env=Shop", "flows")
        after = fake.launcher_feed.last_seq
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                     "name": "qavector_read", "arguments": {"capability": "launcher.main.output",
                                                            "input": {"after": after, "wait_seconds": 10}}}}]
        mcp = subprocess.Popen([core_python, os.path.join(root, "session_launcher.py"), "--qcr-mcp",
                                "--socket=" + host.socket_path, "--token-file=" + str(token_file)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               cwd=root)
        mcp.stdin.write("\n".join(json.dumps(one) for one in lines) + "\n")
        mcp.stdin.close()
        QTimer.singleShot(800, lambda: fake.launcher_feed.log({"level": "ERROR", "session": "u1",
                                                                "text": "step 3 failed: Timeout 5000ms"}))
        started = time.monotonic()
        pump(qapp, lambda: mcp.poll() is not None, 30)        # the window keeps answering while it waits
        answer = json.loads(mcp.stdout.read().splitlines()[1])["result"]
        assert answer["isError"] is False, answer
        result = json.loads(answer["content"][0]["text"])["result"]
        assert [entry["text"] for entry in result["entries"]] == ["step 3 failed: Timeout 5000ms"]
        assert 0.7 < time.monotonic() - started < 8, "it waited for the line, not for the whole wait"
    finally:
        host.stop()
        supervisor.shutdown(1000)
