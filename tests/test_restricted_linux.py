"""Real sandboxes for programs held to a project folder (Linux, opt-in).

Run with ``--linux-runtime-integration --linux-runtime-bundle-dir=DIR`` where DIR
is a built runtime (``core/_internal/runtime`` of a build). Every path here is a
synthetic tree under pytest's tmp_path: the home folder, its ``.ssh``, the
QAVector data the sandbox must not see.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from execution_runtime import restrictions as r
from execution_runtime.backends.linux import restricted

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def bundle(request):
    if not request.config.getoption("--linux-runtime-integration"):
        pytest.skip("Request --linux-runtime-integration to start real sandboxes")
    directory = request.config.getoption("--linux-runtime-bundle-dir")
    assert directory, "--linux-runtime-bundle-dir is required for restricted sandboxes"
    return directory


@pytest.fixture
def tree(tmp_path):
    home = tmp_path / "home"
    project = home / "projects/shop"
    sibling = home / "projects/other"
    for one in (project, sibling, home / ".ssh", home / "QAVector"):
        one.mkdir(parents=True)
    (project / "readme.txt").write_text("shop\n")
    (sibling / "private.txt").write_text("other")
    (home / ".ssh/id_test").write_text("KEY")
    (home / "QAVector/cyclesecrets.key").write_text("SECRET")
    return {"home": home, "project": project, "sibling": sibling,
            "secret": home / "QAVector/cyclesecrets.key"}


def launch(bundle, tree, layers, command, cwd=None, extra=(), timeout=30):
    launcher = restricted.RestrictedLauncher(bundle, protected=[str(tree["secret"])], home=str(tree["home"]))
    effective = r.resolve(layers, str(tree["project"]), home=str(tree["home"]))
    prepared = launcher.prepare(effective, ["/bin/sh", "-c", command], str(cwd or tree["project"]),
                                dict(os.environ, HOME=str(tree["home"])), list(extra))
    try:
        done = subprocess.run(prepared.argv, env=prepared.env, cwd=prepared.cwd, pass_fds=prepared.pass_fds,
                              capture_output=True, text=True, timeout=timeout)
    finally:
        prepared.close()
    return done.returncode, done.stdout + done.stderr


def test_this_computer_can_enforce_files_and_network(bundle):
    found = restricted.RestrictedLauncher(bundle).capabilities()
    assert found["available"], found["reason"]
    assert found["launcher"]["source"] == "bundled"


def test_a_read_only_project_is_readable_at_its_own_path_and_nothing_else_is(bundle, tree):
    code, output = launch(bundle, tree, [("project", {"files": "read_only"})],
        'pwd; cat readme.txt; echo x > new.txt; ls "$HOME" | wc -l; cat %s/private.txt; cat "$HOME/.ssh/id_test"'
        % tree["sibling"])
    lines = output.splitlines()
    assert lines[0] == str(tree["project"]) and lines[1] == "shop"
    assert "Read-only file system" in output
    assert "\n1\n" in "\n" + output + "\n"                 # only projects/, made to hold the mount
    assert "other" not in lines and "KEY" not in output
    assert not (tree["project"] / "new.txt").exists()


def test_read_write_writes_and_python_and_git_from_the_host_still_run(bundle, tree):
    code, output = launch(bundle, tree, [("project", {"files": "read_write"})],
        'echo made > new.txt && cat new.txt && python3 -c "print(2 + 2)" && git --version')
    assert code == 0, output
    assert (tree["project"] / "new.txt").read_text() == "made\n"
    assert "4" in output and "git version" in output


def test_no_file_access_hides_the_project_too(bundle, tree):
    code, output = launch(bundle, tree, [("project", {"files": "read_write", "switched_off": ["files"]})],
                          "ls %s 2>&1; echo done" % tree["project"], cwd="/")
    assert "No such file or directory" in output and "done" in output


def test_secrets_and_keys_stay_hidden_inside_a_granted_folder(bundle, tree):
    code, output = launch(bundle, tree, [("project", {"files": "read_only",
                          "folders": [{"path": str(tree["home"]), "access": "read_only"}]})],
                          'cat "%s"; ls "$HOME/.ssh" | wc -l; cat %s/private.txt' % (tree["secret"], tree["sibling"]))
    assert "SECRET" not in output and "KEY" not in output
    assert "other" in output                          # the granted home really is visible


def test_an_explicitly_granted_ssh_folder_is_not_masked(bundle, tree):
    code, output = launch(bundle, tree, [("project", {"files": "read_only",
                          "folders": [{"path": str(tree["home"] / ".ssh"), "access": "read_only"}]})],
                          'cat "$HOME/.ssh/id_test"')
    assert output.strip() == "KEY"


@pytest.fixture
def server():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    stop = threading.Event()

    def accept():
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
                connection.sendall(b"hello\n")
                connection.close()
            except OSError:
                pass

    worker = threading.Thread(target=accept, daemon=True)
    worker.start()
    yield listener.getsockname()[1]
    stop.set()
    worker.join(2)
    listener.close()


@pytest.mark.parametrize("network,reached", [("on", True), ("off", False)])
def test_network_on_reaches_the_host_and_off_reaches_nothing(bundle, tree, server, network, reached):
    script = ("python3 -c \"import socket; s=socket.create_connection(('127.0.0.1', %d), 3); print(s.recv(6).decode())\""
              % server)
    code, output = launch(bundle, tree, [("project", {"files": "read_only", "network": network})], script)
    assert ("hello" in output) is reached, output


def test_network_on_can_listen_on_a_port(bundle, tree):
    code, output = launch(bundle, tree, [("project", {"network": "on", "files": "read_only"})],
        "python3 -c \"import socket; s=socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(); print('listening')\"")
    assert "listening" in output, output


def test_memory_limit_stops_a_program_that_needs_more(bundle, tree):
    state = restricted.RestrictedLauncher(bundle).capabilities()["limits"]["memory_mb"]
    if not state["available"]:
        pytest.skip(state["reason"])
    code, output = launch(bundle, tree, [("project", {"files": "read_only", "limits": {"memory_mb": 128}})],
                          'python3 -c "b = bytearray(400 * 1024 * 1024); print(len(b))"')
    assert code != 0 and "419430400" not in output


def test_a_limit_this_computer_cannot_enforce_refuses_to_start(bundle, tree):
    state = restricted.RestrictedLauncher(bundle).capabilities()["limits"]["cpu_cores"]
    if state["available"]:
        pytest.skip("CPU limits are available here")
    with pytest.raises(Exception, match="CPU limits"):
        launch(bundle, tree, [("project", {"limits": {"cpu_cores": 1}})], "true")


def core(*arguments, stdin="", env=None, **kwargs):
    return subprocess.run([sys.executable, str(ROOT / "session_launcher.py")] + list(arguments),
                          input=stdin, capture_output=True, text=True, timeout=60, env=env, **kwargs)


def test_preview_reports_what_a_service_would_get_without_starting_it(bundle, tree):
    env = dict(os.environ, QAVECTOR_RUNTIME_DIR=bundle)
    request = {"project_folder": str(tree["project"]), "layers": [["project", {"files": "read_only", "network": "off"}]],
               "command": ["python3", "app.py"], "working_directory": str(tree["project"])}
    answer = json.loads(core("--restrictions-preview", stdin=json.dumps(request), env=env).stdout)
    assert answer["ok"] and answer["summary"] == "Network off · Project read-only"
    hidden = dict(request, working_directory=str(tree["sibling"]))
    answer = json.loads(core("--restrictions-preview", stdin=json.dumps(hidden), env=env).stdout)
    assert not answer["ok"] and answer["problems"][0]["code"] == "working_directory_hidden"


def test_the_exec_shim_becomes_the_service_and_stop_reaches_it(bundle, tree):
    script = ("import signal, sys, time\n"
              "def stop(*_):\n    open('stopped.txt', 'w').write('graceful'); sys.exit(0)\n"
              "signal.signal(signal.SIGTERM, stop)\nprint('ready', flush=True)\n"
              "while True: time.sleep(0.1)\n")
    request = json.dumps({"project_folder": str(tree["project"]), "layers": [["project", {"files": "read_write"}]]})
    process = subprocess.Popen([sys.executable, str(ROOT / "session_launcher.py"), "--restricted-exec=" + request,
                                "--", "python3", "-c", script], cwd=str(tree["project"]), stdout=subprocess.PIPE,
                               env=dict(os.environ, QAVECTOR_RUNTIME_DIR=bundle, HOME=str(tree["home"])),
                               start_new_session=True)
    try:
        assert process.stdout.readline().strip() == b"ready"
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(10)
    finally:
        if process.poll() is None:
            process.kill()
    deadline = time.monotonic() + 5
    while not (tree["project"] / "stopped.txt").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (tree["project"] / "stopped.txt").read_text() == "graceful"


def test_the_exec_shim_refuses_instead_of_running_unrestricted(bundle, tree):
    request = json.dumps({"project_folder": str(tree["project"]), "layers": [["project", {"limits": {"cpu_cores": 1}}]]})
    capabilities = restricted.RestrictedLauncher(bundle).capabilities()
    if capabilities["limits"]["cpu_cores"]["available"]:
        pytest.skip("CPU limits are available here")
    done = core("--restricted-exec=" + request, "--", "/bin/sh", "-c", "echo ran", cwd=str(tree["project"]),
                env=dict(os.environ, QAVECTOR_RUNTIME_DIR=bundle))
    assert done.returncode == 126 and "ran" not in done.stdout and "CPU limits" in done.stderr


def test_a_cycle_holds_its_steps_and_leaves_system_steps_alone(bundle, tree, tmp_path, monkeypatch):
    from cycle.executor import run_cycle
    from cycle.model import parse_cycle
    monkeypatch.setenv("QAVECTOR_RUNTIME_DIR", bundle)
    restricted.reset_capabilities()
    cycle = parse_cycle({"id": "held", "project_folder": "${vars.project}", "variables": {"project": str(tree["project"])},
        "restrictions": {"files": "read_only", "network": "off"},
        "steps": [{"id": "look", "plugin": "command.shell", "with": {"command": "pwd; cat readme.txt"}},
                  {"id": "write", "plugin": "command.shell", "with": {"command": "echo x > readme.txt"},
                   "needs": ["look"], "on_failure": "continue"},
                  {"id": "outside", "plugin": "command.shell", "system": True,
                   "with": {"command": "cat %s/private.txt" % tree["sibling"]}, "needs": ["look"]}]}, "held")
    result = run_cycle(cycle, str(tmp_path / "run"))
    assert result.steps["look"].status == "success"
    assert result.steps["look"].outputs["stdout_tail"].splitlines() == [str(tree["project"]), "shop"]
    assert result.steps["write"].status == "failed"
    assert (tree["project"] / "readme.txt").read_text() == "shop\n"
    assert result.steps["outside"].status == "success"
    assert result.steps["outside"].outputs["stdout_tail"] == "other"


def test_a_cycle_whose_project_folder_cannot_be_resolved_fails_the_step(tmp_path):
    from cycle.executor import run_cycle
    from cycle.model import parse_cycle
    cycle = parse_cycle({"id": "lost", "project_folder": "${vars.missing}", "restrictions": {"network": "off"},
        "steps": [{"id": "work", "plugin": "command.shell", "with": {"command": "echo ran"}}]}, "lost")
    result = run_cycle(cycle, str(tmp_path / "run"))
    assert result.steps["work"].status == "failed"
    assert "ran" not in (result.steps["work"].outputs or {}).get("stdout_tail", "")


def test_restricted_steps_hand_files_over_in_their_run_folder_and_only_read_its_record(bundle, tree, tmp_path, monkeypatch):
    from cycle.executor import run_cycle
    from cycle.model import parse_cycle
    monkeypatch.setenv("QAVECTOR_RUNTIME_DIR", bundle)
    restricted.reset_capabilities()
    cycle = parse_cycle({"id": "handoff", "project_folder": "${vars.project}", "variables": {"project": str(tree["project"])},
        "restrictions": {"files": "read_write", "network": "off"},
        "steps": [{"id": "make", "plugin": "command.shell", "with": {"command": "echo 3 failed > '${run.workspace}/tests.log'"}},
                  {"id": "read", "plugin": "command.shell", "needs": ["make"],
                   "with": {"command": "cat '${run.workspace}/tests.log'; cat '${run.workspace}/steps/make/stdout.log'"}},
                  {"id": "rewrite", "plugin": "command.shell", "needs": ["read"], "on_failure": "continue",
                   "with": {"command": "head -c 1 '${run.workspace}/metadata.json' && echo forged > '${run.workspace}/metadata.json'"}}]},
        "handoff")
    workspace = tmp_path / "run"
    result = run_cycle(cycle, str(workspace))
    assert result.steps["make"].status == "success", result.steps["make"].message
    assert result.steps["read"].status == "success", result.steps["read"].message
    assert result.steps["read"].outputs["stdout_tail"].splitlines()[0] == "3 failed"
    assert result.steps["rewrite"].status == "failed"
    assert result.steps["rewrite"].outputs["stdout_tail"] == "{"        # readable, not writable
    assert "forged" not in (workspace / "metadata.json").read_text()


def test_a_restricted_teams_step_posts_from_a_worker_inside_the_sandbox(bundle, tree, tmp_path, monkeypatch):
    import http.server
    from cycle.executor import run_cycle
    from cycle.model import parse_cycle
    monkeypatch.setenv("QAVECTOR_RUNTIME_DIR", bundle)
    restricted.reset_capabilities()
    received = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(202)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/hook" % server.server_address[1]
    try:
        cycle = parse_cycle({"id": "tell", "project_folder": "${vars.project}",
            "variables": {"project": str(tree["project"])}, "restrictions": {"files": "read_only"},
            "steps": [{"id": "facts", "plugin": "command.shell",
                       "with": {"command": "echo Release is ready > '${run.workspace}/note.md'"}},
                      {"id": "notify", "plugin": "teams.message", "needs": ["facts"],
                       "with": {"webhook": url, "text_file": "note.md"}},
                      {"id": "offline", "plugin": "teams.message", "needs": ["notify"], "on_failure": "continue",
                       "restrictions": {"network": "off"}, "with": {"webhook": url, "text": "never"}}]}, "tell")
        workspace = tmp_path / "run"
        result = run_cycle(cycle, str(workspace))
        assert result.steps["notify"].status == "success", result.steps["notify"].message
        assert len(received) == 1 and b"Release is ready" in received[0]
        assert (workspace / "steps/notify/worker.log").exists()     # it ran as a worker
        assert result.steps["offline"].status == "failed"
        assert "network is off for this step" in result.steps["offline"].message
    finally:
        server.shutdown()


def test_a_protected_link_inside_a_granted_folder_is_hidden_where_it_leads(bundle, tree):
    data = tree["home"] / "QAVector/cycle-runs"
    data.mkdir()
    (data / "run.json").write_text("RECORD")
    (tree["project"] / "cycle-runs").symlink_to(data)
    launcher = restricted.RestrictedLauncher(bundle, protected=[str(tree["project"] / "cycle-runs")],
                                             home=str(tree["home"]))
    effective = r.resolve([("project", {"files": "read_only", "folders": [
        {"path": str(tree["home"] / "QAVector"), "access": "read_only"}]})], str(tree["project"]), home=str(tree["home"]))
    prepared = launcher.prepare(effective, ["/bin/sh", "-c", "cat readme.txt; cat cycle-runs/run.json; cat %s/run.json" % data],
                                str(tree["project"]), dict(os.environ, HOME=str(tree["home"])))
    try:
        done = subprocess.run(prepared.argv, env=prepared.env, cwd=prepared.cwd, pass_fds=prepared.pass_fds,
                              capture_output=True, text=True, timeout=30)
    finally:
        prepared.close()
    assert "bwrap" not in done.stderr, done.stderr     # it starts at all
    assert done.stdout == "shop\n" and "RECORD" not in done.stdout
