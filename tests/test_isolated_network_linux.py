"""Isolated networks for real (Linux, opt-in): the acceptance criteria of
docs/QAVector_Network_Isolation_and_Port_Publishing.md, through the same
``--restricted-exec`` / ``--network-ports`` commands the GUI uses.

Run with ``--linux-runtime-integration --linux-runtime-bundle-dir=DIR`` where DIR
is a built runtime that includes pasta and pesto. The internet check also needs
``QAVECTOR_TEST_INTERNET=1``: it fetches https://example.com.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import http.server
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER = r'''
import http.server, os, signal, sys
name, port = sys.argv[1], int(sys.argv[2])
def bye(signum, frame):
    open(os.path.join(os.getcwd(), "stopped-" + name), "w").write("cleanly")
    sys.exit(0)
signal.signal(signal.SIGINT, bye)
class Hello(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(name.encode())
    def log_message(self, *args): pass
http.server.HTTPServer(("127.0.0.1", port), Hello).serve_forever()
'''
PROBE = r'''
import os, sys, time, urllib.request
time.sleep(0.5)
for url in sys.argv[1:]:
    try:
        print(url, urllib.request.urlopen(url, timeout=6).status)
    except Exception as exc:
        print(url, "FAILED", type(exc).__name__)
'''


@pytest.fixture(scope="module")
def bundle(request):
    if not request.config.getoption("--linux-runtime-integration"):
        pytest.skip("Request --linux-runtime-integration to start real sandboxes")
    directory = request.config.getoption("--linux-runtime-bundle-dir")
    assert directory, "--linux-runtime-bundle-dir is required for isolated networks"
    if not (Path(directory) / "bin/pasta").is_file():
        pytest.skip("This runtime has no pasta; build one with packaging/build_bubblewrap.py")
    return directory


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "project"
    folder.mkdir()
    (folder / "serve.py").write_text(SERVER)
    (folder / "probe.py").write_text(PROBE)
    return folder


class Service:
    """One restricted program started the way the GUI starts it."""

    def __init__(self, bundle, project, name, layers, command):
        self.project, self.name = project, name
        self.state = project.parent / ("state-%s.json" % name)
        request = {"project_folder": str(project), "layers": layers, "network_state": str(self.state)}
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / "session_launcher.py"), "--restricted-exec=" + json.dumps(request), "--"] + command,
            cwd=project, env=dict(os.environ, QAVECTOR_RUNTIME_DIR=bundle), start_new_session=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def publications(self, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.state.is_file():
                return json.loads(self.state.read_text())["publications"]
            if self.process.poll() is not None:
                raise AssertionError("did not start: " + self.process.stdout.read())
            time.sleep(0.1)
        raise AssertionError("no network state")

    def stop(self):
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGINT)      # what Stop does
        try:
            return self.process.wait(10)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            return self.process.wait()


def ports(bundle, state, op, publication=None):
    done = subprocess.run([sys.executable, str(ROOT / "session_launcher.py"), "--network-ports"],
                          input=json.dumps({"state": str(state), "op": op, "publication": publication}),
                          capture_output=True, text=True, env=dict(os.environ, QAVECTOR_RUNTIME_DIR=bundle))
    return json.loads(done.stdout)


def get(port, host="127.0.0.1", tries=40):
    for _ in range(tries):
        try:
            return urllib.request.urlopen("http://%s:%d/" % (host, port), timeout=2).read().decode()
        except OSError:
            time.sleep(0.1)
    return "unreachable"


def pasta_running(bundle):
    found = 0
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            if (os.path.join(bundle, "bin/pasta")).encode() in Path("/proc/%s/cmdline" % pid).read_bytes():
                found += 1
        except OSError:
            pass
    return found


ISOLATED = ["project", {"network": "isolated", "files": "read_write"}]


def test_two_services_share_an_internal_port_and_each_publication_reaches_only_its_own(bundle, project):
    a = Service(bundle, project, "a", [ISOLATED, ["service", {"publish": [
        {"internal_port": 8069}, {"internal_port": 8069, "host_port": 18190}]}]], ["python3", "serve.py", "A", "8069"])
    b = Service(bundle, project, "b", [ISOLATED, ["service", {"publish": [{"internal_port": 8069}]}]],
                ["python3", "serve.py", "B", "8069"])
    try:
        mine, theirs = a.publications(), b.publications()
        auto = [one["host_port"] for one in mine if one.get("auto")][0]
        assert get(auto) == "A" and get(18190) == "A"                            # 1, 2, 5
        assert get(theirs[0]["host_port"]) == "B" and theirs[0]["host_port"] != auto   # 3
        assert get(8069, tries=3) == "unreachable"                              # nothing on the host's 8069
    finally:
        assert a.stop() is not None and b.stop() is not None
    assert (project / "stopped-A").is_file() and (project / "stopped-B").is_file()      # Stop let them finish
    time.sleep(1.5)
    assert not a.state.exists() and not b.state.exists() and pasta_running(bundle) == 0


def test_publishing_and_unpublishing_while_it_runs_does_not_restart_it(bundle, project):
    service = Service(bundle, project, "live", [ISOLATED], ["python3", "serve.py", "L", "8069"])
    try:
        assert service.publications() == []
        added = ports(bundle, service.state, "add", {"internal_port": 8069, "host_port": "auto"})
        port = added["changed"]["host_port"]
        assert added["ok"] and get(port) == "L"
        pid = service.process.pid
        removed = ports(bundle, service.state, "remove", {"host_port": port})
        assert removed["ok"] and removed["publications"] == [] and get(port, tries=5) == "unreachable"
        assert ports(bundle, service.state, "remove", {"host_port": port})["ok"]      # idempotent
        assert service.process.poll() is None and service.process.pid == pid          # 4
    finally:
        service.stop()


def test_a_loopback_publication_is_not_reachable_from_the_network(bundle, project):
    service = Service(bundle, project, "lan", [ISOLATED, ["service", {"publish": [{"internal_port": 8069}]}]],
                      ["python3", "serve.py", "N", "8069"])
    try:
        port = service.publications()[0]["host_port"]
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("192.0.2.1", 9))
        lan = probe.getsockname()[0]
        probe.close()
        assert get(port) == "N" and get(port, lan, tries=3) == "unreachable"   # 6
    finally:
        service.stop()


def test_an_explicit_port_that_is_taken_refuses_to_start_and_says_so(bundle, project):
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    try:
        service = Service(bundle, project, "busy", [ISOLATED, ["service", {"publish": [
            {"internal_port": 8069, "host_port": taken.getsockname()[1]}]}]], ["python3", "serve.py", "X", "8069"])
        assert service.process.wait(15) == 126
        assert "in use" in service.process.stdout.read()
    finally:
        taken.close()


def _host_server():
    class Hello(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass
    for port in range(23000, 23100):            # not ephemeral, like a database's
        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Hello)
        except OSError:
            continue
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server
    pytest.skip("no free port for the host service")


@pytest.mark.parametrize("allowed", [True, False])
def test_services_on_this_computer_only_when_allowed(bundle, project, allowed):
    server = _host_server()
    url = "http://127.0.0.1:%d/" % server.server_address[1]
    try:
        layers = [ISOLATED, ["service", {"host_loopback": allowed}]]
        service = Service(bundle, project, "loop", layers, ["python3", "probe.py", url])
        out = service.process.communicate(timeout=30)[0]
        assert (url + " 200" in out) is allowed, out
    finally:
        server.shutdown()


@pytest.mark.skipif(os.environ.get("QAVECTOR_TEST_INTERNET") != "1", reason="set QAVECTOR_TEST_INTERNET=1 to fetch example.com")
@pytest.mark.parametrize("allowed", [True, False])
def test_the_internet_only_when_allowed(bundle, project, allowed):
    layers = [ISOLATED, ["service", {"outbound": allowed}]]
    service = Service(bundle, project, "web", layers, ["python3", "probe.py", "https://example.com/"])
    out = service.process.communicate(timeout=40)[0]
    assert ("https://example.com/ 200" in out) is allowed, out


def test_host_sockets_stay_out_when_the_network_is_not_the_hosts(bundle, project):
    bus = "/run/user/%d" % os.getuid()
    if not os.path.isdir(bus):
        pytest.skip("no session runtime folder here")
    look = ["sh", "-c", "ls %s >/dev/null 2>&1 && echo visible || echo hidden" % bus]
    for network, expected in (("off", "hidden"), ("isolated", "hidden"), ("on", "visible")):
        layers = [["project", {"network": network}]]       # Files: no restrictions
        service = Service(bundle, project, "sock-" + network, layers, look)
        assert service.process.communicate(timeout=20)[0].strip().splitlines()[-1] == expected, network


def test_a_hard_kill_leaves_no_publication_and_no_pasta_behind(bundle, project):
    service = Service(bundle, project, "crash", [ISOLATED, ["service", {"publish": [{"internal_port": 8069}]}]],
                      ["python3", "serve.py", "C", "8069"])
    port = service.publications()[0]["host_port"]
    assert get(port) == "C"
    os.killpg(service.process.pid, signal.SIGKILL)          # QAVector itself gone, no clean-up run
    service.process.wait(10)
    time.sleep(1.5)
    assert get(port, tries=3) == "unreachable" and pasta_running(bundle) == 0       # 9
    # The state file outlived it, and says so: a reader sees nothing running.
    from execution_runtime.backends.linux.isolated import load_state
    from execution_runtime.errors import RuntimePolicyError
    with pytest.raises(RuntimePolicyError, match="not running"):
        load_state(str(service.state))
