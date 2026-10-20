"""Core protocol and GUI lifecycle; fake workers make no native isolation claims."""

import json
import hashlib
from pathlib import Path
import sys
import time

import pytest

from cms_gui import runnertypes, services, servicesfile as sf
from cms_gui.core import Core


def execution():
    return {"mode": "isolated", "deployment": "offline", "revision": "a" * 64}


def pump(qapp, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        qapp.processEvents(); time.sleep(0.01)
    qapp.processEvents()
    assert predicate()


@pytest.fixture
def core(tmp_path):
    script = tmp_path / "fixture_core.py"
    script.write_text('''import json, sys, time
packet=json.loads(sys.stdin.readline())
def emit(kind, **fields):
    print(json.dumps(dict(fields, kind=kind, protocol_version=1)), flush=True)
time.sleep(0.1)
emit("runtime.service.ready", provider="fixture", enforcement="verified", policy_digest="a"*64)
emit("runtime.service.output", stream="stdout", text='ready\\n{"kind":"runtime.service.error"}\\n')
json.loads(sys.stdin.readline())
emit("runtime.service.end", ok=True, cleanup_confirmed=True, exit_code=0)
''', encoding="utf-8")
    class Core:
        prefix = [sys.executable, str(script)]
        def is_configured(self):
            return True
        def spawn_dir(self):
            return str(tmp_path)
    return Core()


def test_isolated_service_waits_for_runtime_ready_streams_output_and_stops(qapp, tmp_path, core, dispose):
    row = sf.RunnerRow("Worker", "shell", settings={"command": "/inside/tool"}, extra={"execution": execution()})
    service = services.ServiceProcess("Demo", row, str(tmp_path))
    service.runtime_core = core
    try:
        assert service.start()
        assert service.status == runnertypes.STARTING
        pump(qapp, lambda: service.status == runnertypes.RUNNING and len(service.console()) == 2)
        assert service.console() == ["ready", '{"kind":"runtime.service.error"}']
        assert service.runtime_info["provider"] == "fixture"
        assert service.stop()
        pump(qapp, lambda: service.status == runnertypes.STOPPED)
        assert service.runtime_info["cleanup_confirmed"] is True
    finally:
        service.dispose()


@pytest.mark.parametrize("configuration", [None, {}, {"mode": "unknown"}])
def test_malformed_execution_configuration_never_falls_back_to_host(qapp, tmp_path, configuration):
    marker = tmp_path / "must-not-exist"
    row = sf.RunnerRow("Worker", "shell", settings={"command": '%s -c "open(\'%s\',\'w\').close()"' % (sys.executable, marker)},
                       extra={"execution": configuration})
    service = services.ServiceProcess("Demo", row)
    assert not service.start()
    assert service.status == runnertypes.FAILED and service._proc is None and not marker.exists()
    service.dispose()


def test_runtime_cleanup_not_confirmed_blocks_supervisor_automatic_restart(qapp, tmp_path):
    supervisor = services.ServiceSupervisor()
    row = sf.RunnerRow("Worker", "shell", settings={"command": "unused"})
    supervisor.sync([sf.ProjectRow("Demo", str(tmp_path), runners=[row])])
    service = supervisor.service("Demo", "Worker")
    service.runtime_info = {"cleanup_confirmed": False}
    supervisor._restarting.add(service.key)
    supervisor._service_changed(service.key, runnertypes.FAILED)
    qapp.processEvents()
    assert service._proc is None and service.key not in supervisor._restarting
    supervisor.dispose()


def test_unsaved_isolated_restart_keeps_the_existing_service_running(qapp, tmp_path, core):
    supervisor = services.ServiceSupervisor()
    row = sf.RunnerRow("Worker", "shell", settings={"command": "/inside/tool"}, extra={"execution": execution()})
    supervisor.sync([sf.ProjectRow("Demo", str(tmp_path), runners=[row])])
    service = supervisor.service("Demo", "Worker")
    service.runtime_core = core
    try:
        assert supervisor.start("Demo", "Worker")
        pump(qapp, lambda: service.status == runnertypes.RUNNING)
        service.launch_allowed = False
        worker = service._isolated
        assert not supervisor.restart("Demo", "Worker")
        assert not service.restart()
        assert service.status == runnertypes.RUNNING and service._isolated is worker
        assert "Save" in service.detail
    finally:
        supervisor.dispose()


def test_gui_to_frozen_core_launches_and_restarts_an_actual_isolated_service(qapp, tmp_path, request, monkeypatch):
    binary = request.config.getoption("--gui-runtime-frozen-core")
    if not binary:
        pytest.skip("Pass --gui-runtime-frozen-core for real GUI/core/kernel service acceptance")
    if sys.platform != "linux":
        pytest.skip("The native Windows backend has not been implemented")
    control, workspace = tmp_path / "control", tmp_path / "project"
    registry = control / "execution-deployments"; registry.mkdir(parents=True)
    workspace.mkdir(); (workspace / "approved").write_text("approved content")
    monkeypatch.setenv("CMS_HOME", str(control))
    monkeypatch.setenv("HOST_CREDENTIAL", "must-not-be-inherited")
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
    file_hash = hashlib.sha256(Path("/usr/bin/busybox").read_bytes()).hexdigest()
    target = {"target": "/bin/busybox", "sha256": file_hash, "executable": True}
    identity = "sha256:" + digest({"version": 1, "files": [target]})
    deployment = {"api_version": "qavector.io/v1", "kind": "RuntimeDeployment", "metadata": {"name": "offline"},
        "spec": {"provider": "linux-isolated", "working_directory": "/workspace",
            "policy": {"filesystem": [{"binding": "project", "access": "read_only"}], "environment": ["SERVICE_TOKEN"],
                       "resources": {"scratch_bytes": 65536}},
            "bindings": [{"name": "project", "source": str(workspace), "target": "/workspace"}],
            "backend_options": {"linux": {"state_directory": str(tmp_path / "state"),
                "image": {"identity": identity, "files": [dict(target, source="/usr/bin/busybox")]}}}}}
    revision = digest(deployment)
    (registry / "offline.json").write_text(json.dumps({"revision": revision, "deployment": deployment}))
    body = ('test -z "$HOST_CREDENTIAL" || exit 9; /bin/busybox cat approved; '
            '! (printf changed > approved) || exit 10; printf "gui-live %s\\n" "$SERVICE_TOKEN"; /bin/busybox sleep 20')
    import shlex
    row = sf.RunnerRow("Frozen worker", "shell", settings={"command": "/bin/busybox sh -c " + shlex.quote(body),
        "env": {"SERVICE_TOKEN": "synthetic-sensitive-token"}, "stop_grace": "0.2"},
        extra={"execution": {"mode": "isolated", "deployment": "offline", "revision": revision}})
    service = services.ServiceProcess("Demo", row, str(workspace))
    service.runtime_core = Core(script=binary, interpreter="")
    try:
        assert service.start()
        pump(qapp, lambda: service.status == runnertypes.RUNNING and any("gui-live [redacted]" in line for line in service.console()), 15)
        first = service.runtime_info["instance_id"]
        assert "synthetic-sensitive-token" not in "\n".join(service.console())
        assert "synthetic-sensitive-token" not in " ".join(service._isolated.process.arguments())
        assert service.restart()
        pump(qapp, lambda: service.status == runnertypes.RUNNING and service.runtime_info.get("instance_id") != first, 15)
        assert json.loads((tmp_path / "state" / first / "runtime.json").read_text())["state"] == "cleaned"
        assert not (tmp_path / "state" / first / "root").exists()
        assert service.stop()
        pump(qapp, lambda: service.status == runnertypes.STOPPED, 15)
        assert service.runtime_info["cleanup_confirmed"] is True
        assert (workspace / "approved").read_text() == "approved content"
    finally:
        service.dispose()
