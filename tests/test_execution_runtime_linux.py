"""Linux units and explicitly requested real namespace/security integration tests."""

import io
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import time

import pytest

if sys.platform != "linux":
    pytest.skip("Linux native backend", allow_module_level=True)

from execution_runtime import PolicyLayer, PolicyPatch, PolicyResolver, RuntimePolicyError
from execution_runtime.backends.base import ExecutionRequest
from execution_runtime.backends.linux import ApprovedLauncher, LinuxBackend, RuntimeImage, SystemBubblewrapBackend
from execution_runtime.backends.linux.cgroups import CgroupParent
from execution_runtime.backends.linux.filesystem import open_pinned
from execution_runtime.backends.linux.supervisor import LogCapture
from execution_runtime.runtime import ExecutionRuntime
from execution_runtime.backends.linux.recovery import identity_exited, process_identity, lock_file
from execution_runtime.backends.linux.scratch import scratch_size
from execution_runtime.backends.linux.secrets import secret_path


def test_image_copy_hash_detects_changed_dependency(tmp_path):
    source = tmp_path / "binary"
    source.write_bytes(b"approved")
    image = RuntimeImage.from_files([(source, "/bin/tool", True)])
    source.write_bytes(b"changed")
    with pytest.raises(RuntimePolicyError) as exc:
        image.materialize(tmp_path / "root")
    assert exc.value.code == "image_changed"


def test_approved_launcher_is_copied_privately_and_changed_content_is_rejected(tmp_path):
    source = tmp_path / "launcher"
    source.write_bytes(b"approved synthetic launcher")
    launcher = ApprovedLauncher.from_file(source)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    copied = Path(launcher.materialize(state))
    assert copied.read_bytes() == source.read_bytes() and copied.stat().st_ino != source.stat().st_ino
    copied.chmod(0o600)
    copied.write_bytes(b"changed")
    with pytest.raises(RuntimePolicyError) as exc:
        launcher.materialize(state)
    assert exc.value.code == "launcher_changed"


def test_changed_launcher_source_is_rejected_before_first_provisioning(tmp_path):
    source = tmp_path / "launcher"
    source.write_bytes(b"approved")
    launcher = ApprovedLauncher.from_file(source)
    source.write_bytes(b"replacement")
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    with pytest.raises(RuntimePolicyError) as exc:
        launcher.materialize(state)
    assert exc.value.code == "image_changed"


@pytest.mark.parametrize("size", [1, 4095, 4097, 2 ** 63, True])
def test_scratch_capacity_cannot_round_up_or_change_limit_semantics(size):
    with pytest.raises(RuntimePolicyError) as exc:
        scratch_size({"scratch_bytes": size})
    assert exc.value.code == "unsupported_scratch_limit"


def test_recovery_process_identity_detects_pid_reuse_without_signalling():
    identity = process_identity(os.getpid())
    assert not identity_exited(identity)
    assert identity_exited(dict(identity, start_ticks=identity["start_ticks"] + 1))
    assert identity_exited(dict(identity, boot_id="prior-boot"))


def test_lease_is_exclusive_and_refuses_symlinks(tmp_path):
    path = tmp_path / "instance.lock"
    lease = lock_file(path, create=True, nonblocking=True)
    try:
        with pytest.raises(BlockingIOError):
            lock_file(path, nonblocking=True)
        link = tmp_path / "link"
        link.symlink_to(path)
        with pytest.raises(OSError):
            lock_file(link, nonblocking=True)
    finally:
        os.close(lease)


def test_private_runtime_state_rejects_group_or_world_access(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    backend = LinuxBackend(state_directory=state)
    with pytest.raises(RuntimePolicyError) as exc:
        backend._state()
    assert exc.value.code == "unsafe_state_directory"


def test_image_is_a_private_copy_without_host_links_or_special_permission_bits(tmp_path):
    source = tmp_path / "binary"
    source.write_bytes(b"approved")
    source.chmod(0o4755)
    image = RuntimeImage.from_files([(source, "/bin/tool", True)])
    image.materialize(tmp_path / "root")
    copied = tmp_path / "root/bin/tool"
    assert copied.read_bytes() == b"approved"
    assert copied.stat().st_ino != source.stat().st_ino
    assert copied.stat().st_mode & 0o7777 == 0o555
    source.write_bytes(b"new version")
    assert copied.read_bytes() == b"approved"


def test_source_pinning_rejects_symlink_in_any_component(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "file").write_text("content")
    (tmp_path / "link").symlink_to(allowed, target_is_directory=True)
    with pytest.raises(OSError):
        open_pinned(str(tmp_path / "link/file"))


@pytest.mark.parametrize("limits", [{"processes": 10}, {"threads": 10}, {"scratch_bytes": 100},
                                    {"cpu_percent": 10}, {"tasks": 3}, {"cpu_cores": 0.0001},
                                    {"memory": {"bytes": 33554432, "accounting": "resident"}},
                                    {"memory": {"bytes": 33554432, "accounting": "committed"}}])
def test_cgroup_controller_refuses_different_resource_semantics(limits):
    with pytest.raises(RuntimePolicyError):
        CgroupParent.settings(limits)


def test_fake_control_files_cannot_be_presented_as_kernel_cgroups(tmp_path):
    (tmp_path / "memory.max").write_text("100")
    with pytest.raises(RuntimePolicyError) as exc:
        CgroupParent(str(tmp_path))
    assert exc.value.code == "invalid_cgroup_parent"


def test_log_secrets_split_at_read_boundary_do_not_leak_to_artifact_or_tail(tmp_path):
    class Chunks:
        def __init__(self):
            self.blocks = iter([b"prefix sec", b"ret-v", b"alue suffix\n", b""])
        def read(self, count):
            return next(self.blocks)
        def close(self):
            pass
    capture = LogCapture(Chunks(), tmp_path / "log", ["secret-value"])
    capture.start()
    capture.join(2)
    assert (tmp_path / "log").read_bytes() == b"prefix [redacted] suffix\n"
    assert b"secret-value" not in capture.tail


def test_live_log_line_does_not_wait_for_secret_length_or_process_exit(tmp_path):
    output = io.BytesIO()
    capture = LogCapture(io.BytesIO(), tmp_path / "unused", ["a-long-sensitive-token"])
    assert capture._consume(output, b"worker-ready\n") == b""
    assert output.getvalue() == b"worker-ready\n"


def test_json_escaped_unicode_credentials_are_redacted_across_read_boundaries(tmp_path):
    value = 'token-\u043a\u043b\u044e\u0447-"quote"-\\slash\nend'
    encoded = json.dumps(value, ensure_ascii=True)[1:-1].encode()
    class Chunks:
        def __init__(self):
            self.blocks = iter([b'prefix ' + encoded[:12], encoded[12:27], encoded[27:] + b' suffix', b''])
        def read(self, _count):
            return next(self.blocks)
        def close(self):
            pass
    capture = LogCapture(Chunks(), tmp_path / "json.log", [value])
    capture.start()
    capture.join(2)
    assert (tmp_path / "json.log").read_bytes() == b"prefix [redacted] suffix"
    assert encoded not in capture.tail


def test_large_output_is_drained_but_artifact_size_is_bounded(tmp_path):
    capture = LogCapture(io.BytesIO(b"x" * 100000), tmp_path / "log", limit=1024)
    capture.start()
    capture.join(2)
    assert capture.truncated and (tmp_path / "log").stat().st_size == 1024
    assert len(capture.tail) <= 8192


@pytest.mark.parametrize("chunks,redactions,expected", [
    ([b"abcab", b"cd", b""], ["abcd"], b"abc[redacted]"),
    ([b"abc", b"d", b""], ["ab", "abcd"], b"[redacted]"),
    ([b"aaaa", b"aaaa", b""], ["a", "redacted"], b"[redacted]" * 8),
])
def test_stream_redaction_handles_overlapping_matches_without_reprocessing_markers(tmp_path, chunks, redactions, expected):
    class Chunks:
        def __init__(self):
            self.blocks = iter(chunks)
        def read(self, count):
            return next(self.blocks)
        def close(self):
            pass
    capture = LogCapture(Chunks(), tmp_path / "log", redactions)
    capture.start()
    capture.join(2)
    assert (tmp_path / "log").read_bytes() == expected


def test_cgroup_limits_use_explicit_charged_memory_and_task_semantics():
    assert CgroupParent.settings({"cpu_cores": 0.5, "tasks": 16,
        "memory": {"bytes": 33554432, "accounting": "charged"}}) == {
        "cpu.max": "50000 100000", "pids.max": "16", "memory.max": "33554432",
        "memory.swap.max": "0", "memory.oom.group": "1"}
    with pytest.raises(RuntimePolicyError):
        CgroupParent.settings({"cpu_cores": 1e308})


@pytest.fixture(scope="module")
def native(request, tmp_path_factory):
    if not (request.config.getoption("--linux-runtime-integration")
            or request.config.getoption("--linux-runtime-resource-integration")
            or request.config.getoption("--linux-runtime-scratch-integration")):
        pytest.skip("Request --linux-runtime-integration to require real OS probes")
    assert (request.config.getoption("--linux-runtime-bundle-dir") or request.config.getoption("--linux-runtime-launcher")
            or shutil.which("bwrap")) and shutil.which("gcc") and Path("/usr/bin/busybox").exists(), "Native prerequisites missing"
    root = tmp_path_factory.mktemp("linux-runtime-native")
    source = root / "probe.c"
    source.write_text(r'''
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <time.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>
static void *sleeper(void *unused) { sleep(2); return NULL; }
static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return t.tv_sec+t.tv_nsec/1e9; }
int main(int argc, char **argv) {
  if (argc > 1 && !strcmp(argv[1], "socket")) {
    errno=0; int fd=socket(atoi(argv[2]), SOCK_STREAM, 0);
    printf("socket=%d errno=%d\n",fd,errno); if(fd>=0) close(fd); return 0;
  }
  if (argc > 1 && !strcmp(argv[1], "unshare")) {
    errno=0; long r=syscall(SYS_unshare,0x10000000);
    printf("unshare=%ld errno=%d\n",r,errno); return 0;
  }
  if (argc > 1 && !strcmp(argv[1], "fds")) {
    int leaked=0; for(int i=3;i<4096;i++) if(fcntl(i,F_GETFD)>=0) {printf("fd=%d\n",i); leaked++;}
    printf("extra=%d\n",leaked); return 0;
  }
  if (argc > 1 && !strcmp(argv[1], "cpu")) {
    double end=now()+1.5; volatile unsigned long value=1;
    while(now()<end) for(int i=0;i<10000;i++) value=value*1664525+1013904223;
    return 0;
  }
  if (argc > 1 && !strcmp(argv[1], "memory")) {
    for(int i=0;i<128;i++) {
      volatile char *p=malloc(1024*1024); if(!p) return 3;
      for(int j=0;j<1024*1024;j+=4096) p[j]=1;
    } return 4;
  }
  if (argc > 1 && !strcmp(argv[1], "processes")) {
    int count=0, error=0;
    for(int i=0;i<32;i++) { pid_t p=fork(); if(p<0) {error=errno;break;} if(!p) {sleep(2);_exit(0);} count++; }
    printf("created=%d errno=%d\n",count,error); while(wait(NULL)>0) {} return 0;
  }
  if (argc > 1 && !strcmp(argv[1], "threads")) {
    pthread_t threads[32]; int count=0, error=0;
    for(int i=0;i<32;i++) {int r=pthread_create(&threads[i],NULL,sleeper,NULL); if(r) {error=r;break;} count++;}
    printf("created=%d errno=%d\n",count,error); for(int i=0;i<count;i++) pthread_join(threads[i],NULL); return 0;
  }
  return 2;
}
''')
    executable = root / "probe"
    subprocess.run(["gcc", "-static", "-pthread", "-O2", "-o", str(executable), str(source)], check=True, capture_output=True)
    image = RuntimeImage.from_files([("/usr/bin/busybox", "/bin/busybox", True), (executable, "/bin/probe", True)])
    launcher_path = request.config.getoption("--linux-runtime-launcher")
    bundle_directory = request.config.getoption("--linux-runtime-bundle-dir")
    launcher = ApprovedLauncher.from_file(launcher_path or shutil.which("bwrap")) if not bundle_directory else None
    backend = LinuxBackend(image, root / "state", launcher=launcher, bundle_directory=bundle_directory)
    capabilities = backend.capabilities()
    failure = [(one.name, one.reason) for one in capabilities.features if one.name == "runtime_execution" and one.status != "verified"]
    assert not failure, failure
    yield backend, root
    for job in list(backend._jobs.values()):
        if job.state != "cleaned":
            if job.process is not None and not job.done.is_set():
                backend.stop(job.token, time.monotonic())
            backend.cleanup(job.token)


def policy(backend, workspace=None, access="read_only", **settings):
    raw = dict(settings)
    bindings = []
    if workspace is not None:
        raw["filesystem"] = [{"binding": "project", "access": access}]
        bindings = [{"name": "project", "source": str(workspace), "target": "/workspace"}]
    return PolicyResolver().resolve([PolicyLayer("platform", PolicyPatch(raw))], bindings,
        platform="linux", provider=backend.provider, runtime_root_identity=backend.image.identity)


def run(native, argv, workspace=None, access="read_only", timeout=3, env_values=None, **settings):
    backend, _ = native
    effective = policy(backend, workspace, access, **settings)
    request = ExecutionRequest(effective, argv, "/workspace" if workspace is not None else "/",
                               environment=env_values or {}, timeout_seconds=timeout)
    prepared = backend.prepare(request)
    handle = backend.start(prepared)
    result = backend.wait(handle)
    backend.cleanup(handle)
    return result


def test_native_scoped_secrets_are_read_only_ephemeral_and_redacted(native):
    backend, _ = native
    name, value = "vault/project/key", "synthetic-sensitive-token-\u043a\u043b\u044e\u0447"
    effective = policy(backend, secrets=[name], environment=["API_TOKEN"])
    script = ('/bin/busybox cat "$1"; printf "\\n"; /bin/busybox cat "$1" >&2; '
              '! (printf overwritten > "$1") && ! /bin/busybox rm "$1" && /bin/probe fds')
    request = ExecutionRequest(effective, ("/bin/busybox", "sh", "-c", script, "test", secret_path(name)), "/",
        timeout_seconds=3, secret_values={name: value}, environment={"API_TOKEN": "synthetic-environment-token"})
    handle = backend.prepare(request)
    job = backend._get(handle)
    assert not job.request.secret_values.names
    assert all(value.encode() not in file.read_bytes() for file in job.directory.rglob("*") if file.is_file())
    try:
        handle = backend.start(handle)
        assert value not in repr(job.process.args)
        assert "synthetic-environment-token" not in repr(job.process.args)
        result = backend.wait(handle)
        assert result["exit_code"] == 0 and "extra=0" in result["stdout_tail"], result
        assert "[redacted]" in result["stdout_tail"] and "[redacted]" in result["stderr_tail"]
        assert value not in json.dumps(result, ensure_ascii=False)
        assert result["secret_files"] == [{"name": name, "path": secret_path(name)}]
    finally:
        backend.cleanup(handle)
    assert not (job.directory / "root").exists()
    assert not job.secret_redactions and not any(capture.redactions for capture in job.captures)
    assert all(value.encode() not in file.read_bytes() for file in job.directory.rglob("*") if file.is_file())
    other = run(native, ("/bin/busybox", "sh", "-c", '! /bin/busybox cat "$1"', "test", secret_path(name)))
    assert other["exit_code"] == 0 and value not in other["stdout_tail"]


def test_native_missing_scoped_value_cannot_be_treated_as_a_delivered_secret(native):
    backend, _ = native
    with pytest.raises(RuntimePolicyError) as exc:
        backend.prepare(ExecutionRequest(policy(backend, secrets=["missing"]), ("/bin/busybox", "true"), "/"))
    assert exc.value.code == "secret_inputs_missing"


def test_cancelled_preparation_releases_secret_fds_without_writing_payload_to_disk(native):
    import fcntl
    backend, _ = native
    handle = backend.prepare(ExecutionRequest(policy(backend, secrets=["key"]), ("/bin/busybox", "true"), "/",
        secret_values={"key": "not-written-to-disk"}))
    job = backend._get(handle)
    fd = job.secret_mounts[0][0]
    with pytest.raises(OSError):
        os.write(fd, b"changed")
    backend.cleanup(handle)
    with pytest.raises(OSError):
        fcntl.fcntl(fd, fcntl.F_GETFD)
    assert not job.secret_redactions


def test_rejected_launch_is_durably_audited_without_argv_or_environment_values(native):
    backend, _ = native
    runtime = ExecutionRuntime(backend, audit=True)
    request = ExecutionRequest(policy(backend, network={"mode": "full"}),
                               ("/bin/busybox", "sensitive-argument"), "/")
    with pytest.raises(RuntimePolicyError):
        runtime.prepare(request)
    records = [json.loads(path.read_text()) for path in (backend.state_directory / "audit").glob("*.json")]
    rejected = [record for record in records if record["kind"] == "runtime.rejected"][-1]
    assert rejected["policy_digest"] == request.policy.digest
    assert rejected["effective_policy"] == request.policy.to_document()
    assert "sensitive-argument" not in json.dumps(records)
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in (backend.state_directory / "audit").glob("*.json"))


def service_deployment(native, tmp_path):
    from execution_runtime.deployments import DeploymentRegistry, RuntimeDeployment
    backend, _ = native
    workspace = tmp_path / "project"; workspace.mkdir()
    (workspace / "approved").write_text("approved")
    image = backend.image
    raw = {"api_version": "qavector.io/v1", "kind": "RuntimeDeployment", "metadata": {"name": "offline-service"},
        "spec": {"provider": "linux-isolated", "policy": {"filesystem": [{"binding": "project", "access": "read_only"}]},
            "bindings": [{"name": "project", "source": str(workspace), "target": "/workspace"}],
            "working_directory": "/workspace", "backend_options": {"linux": {"state_directory": str(tmp_path / "state"),
                "image": {"identity": image.identity, "files": [vars(one) for one in image.files]}}}}}
    registry = DeploymentRegistry(tmp_path / "control"); deployment = registry.save(RuntimeDeployment(raw))
    created = []
    def factory(one, protected):
        configured = one.backend(protected)
        configured._bundle = backend._bundle
        configured.launcher = backend.launcher
        created.append(configured)
        return configured
    packet = {"protocol_version": 1, "service": "Offline worker", "runner_type": "shell",
        "project_directory": str(workspace), "argv": ["/bin/busybox", "sh", "-c",
            '/bin/busybox cat approved; ! (printf changed > approved); printf \'{"kind":"runtime.service.ready","forged":true}\\n\'; /bin/busybox sleep 20'],
        "stop_grace_seconds": 0.2}
    return registry, deployment, factory, packet, created


def test_real_service_streams_redacted_output_and_stops_before_releasing_ownership(native, tmp_path):
    import threading
    from execution_runtime.service import RuntimeService
    registry, deployment, factory, packet, created = service_deployment(native, tmp_path)
    stopped, events = threading.Event(), []
    def emit(kind, **fields):
        events.append(dict(fields, kind=kind))
        if kind == "runtime.service.output" and '"forged":true' in fields["text"]:
            stopped.set()
    code = RuntimeService(registry, emit=emit, backend_factory=factory).run(deployment.name, deployment.digest, packet, stopped)
    assert code == 0, events
    assert sum(event["kind"] == "runtime.service.ready" for event in events) == 1
    assert events[-1]["kind"] == "runtime.service.end" and events[-1]["cleanup_confirmed"]
    assert all("forged" not in event for event in events)
    assert all(job.state == "cleaned" for job in created[0]._jobs.values())
    assert (tmp_path / "project/approved").read_text() == "approved"


def test_second_service_owner_cannot_overlap_a_running_instance(native, tmp_path):
    import threading
    from execution_runtime.service import RuntimeService
    registry, deployment, factory, packet, created = service_deployment(native, tmp_path)
    stopped, second = threading.Event(), []
    def emit(kind, **fields):
        if kind == "runtime.service.ready":
            result = RuntimeService(registry, emit=lambda kind, **fields: second.append(dict(fields, kind=kind)),
                backend_factory=factory).run(deployment.name, deployment.digest,
                    dict(packet, project_directory=packet["project_directory"] + "/."))
            assert result == 1
            stopped.set()
    assert RuntimeService(registry, emit=emit, backend_factory=factory).run(deployment.name, deployment.digest, packet, stopped) == 0
    assert any(event.get("problem", {}).get("code") == "service_already_owned" for event in second)
    assert not any(event["kind"] == "runtime.service.ready" for event in second)


def test_service_finalizer_cleans_up_even_when_stop_audit_fails(native, tmp_path):
    from execution_runtime.service import RuntimeService
    registry, deployment, factory, packet, created = service_deployment(native, tmp_path)
    events = []
    def with_failed_stop_audit(one, protected):
        backend = factory(one, protected)
        original = backend.record_event
        def record(event):
            if event["kind"] == "runtime.stopped":
                raise RuntimePolicyError("audit_unavailable", "Cannot persist stop audit.")
            original(event)
        backend.record_event = record
        return backend
    def emit(kind, **fields):
        events.append(dict(fields, kind=kind))
        if kind == "runtime.service.ready":
            raise OSError("UI disconnected")
    code = RuntimeService(registry, emit=emit, backend_factory=with_failed_stop_audit).run(
        deployment.name, deployment.digest, packet)
    assert code == 1
    assert events[-1]["cleanup_confirmed"]
    assert all(job.state == "cleaned" for job in created[0]._jobs.values())
    assert not list((tmp_path / "state").glob("*/root"))


@pytest.mark.parametrize("cancel", ["natural", "stop", "eof"])
def test_frozen_core_owns_real_service_and_confirms_cleanup_before_exit(native, tmp_path, request, cancel):
    core = request.config.getoption("--runtime-frozen-core")
    if not core:
        pytest.skip("Pass --runtime-frozen-core for packaged service acceptance")
    registry, deployment, _, packet, _ = service_deployment(native, tmp_path)
    packet["argv"] = ["/bin/busybox", "sh", "-c", 'printf "packaged-service-ok\\n"; /bin/busybox sleep ' + ("0.1" if cancel == "natural" else "20")]
    env = dict(os.environ, CMS_HOME=str(tmp_path / "user-data"), PATH="/qavector-no-system-bwrap")
    process = subprocess.Popen([core, "--runtime-service=" + deployment.name,
        "--runtime-service-revision=" + deployment.digest, "--runtime-deployments-dir=" + str(registry.directory)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, bufsize=0)
    records = []
    try:
        process.stdin.write((json.dumps(packet) + "\n").encode()); process.stdin.flush()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
                break
            raw = process.stdout.readline()
            if not raw:
                break
            record = json.loads(raw); records.append(record)
            if cancel == "stop" and record["kind"] == "runtime.service.ready":
                process.stdin.write(b'{"op":"stop"}\n'); process.stdin.flush()
            if cancel == "eof" and record["kind"] == "runtime.service.ready":
                process.stdin.close()
            if record["kind"] == "runtime.service.end":
                break
        code = process.wait(5)
        assert code == 0, (records, process.stderr.read().decode())
        assert records[-1]["kind"] == "runtime.service.end" and records[-1]["cleanup_confirmed"], records
        assert records[-1]["ok"], records
        assert all(record["protocol_version"] == 1 for record in records)
        assert not list((tmp_path / "state").glob("*/root"))
    finally:
        if process.poll() is None:
            process.kill(); process.wait(5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def test_frozen_service_recovers_crashed_owner_before_starting_a_replacement(native, tmp_path, request):
    from execution_runtime.backends.linux.recovery import identity_exited
    core = request.config.getoption("--runtime-frozen-core")
    if not core:
        pytest.skip("Pass --runtime-frozen-core for packaged service crash acceptance")
    registry, deployment, _, packet, _ = service_deployment(native, tmp_path)
    command = [core, "--runtime-service=" + deployment.name, "--runtime-service-revision=" + deployment.digest,
               "--runtime-deployments-dir=" + str(registry.directory)]
    processes = []
    def launch():
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=dict(os.environ, CMS_HOME=str(tmp_path / "user-data"), PATH="/qavector-no-system-bwrap"), bufsize=0)
        processes.append(process)
        process.stdin.write((json.dumps(packet) + "\n").encode()); process.stdin.flush()
        return process
    def until(process, kind):
        records, deadline = [], time.monotonic() + 10
        while time.monotonic() < deadline:
            if not select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
                break
            raw = process.stdout.readline()
            if not raw:
                break
            records.append(json.loads(raw))
            if records[-1]["kind"] == kind:
                return records[-1]
        pytest.fail("Missing %s after events %r" % (kind, records))
    try:
        owner = launch()
        first = until(owner, "runtime.service.ready")["instance_id"]
        previous = tmp_path / "state" / first
        metadata = json.loads((previous / "runtime.json").read_text())
        owner.kill(); owner.wait(5)
        deadline = time.monotonic() + 5
        while not identity_exited(metadata["recovery"]["init"]) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert identity_exited(metadata["recovery"]["init"])
        replacement = launch()
        second = until(replacement, "runtime.service.ready")["instance_id"]
        assert second != first
        assert not (previous / "root").exists()
        recovered = json.loads((previous / "runtime.json").read_text())
        assert recovered["state"] == "cleaned" and recovered["reason"] == "supervisor_lost"
        replacement.stdin.write(b'{"op":"stop"}\n'); replacement.stdin.flush()
        assert until(replacement, "runtime.service.end")["cleanup_confirmed"]
        assert replacement.wait(5) == 0
        assert not list((tmp_path / "state").glob("*/root"))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill(); process.wait(5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()


@pytest.fixture(scope="module")
def git_workload_image(native, tmp_path_factory):
    from runtime_git_image import git_image
    return git_image(tmp_path_factory.mktemp("git-workload-image"), native[0].image)


def git_deployment(native, git_workload_image, tmp_path, delayed=False):
    from execution_runtime.deployments import DeploymentRegistry, RuntimeDeployment
    image, worker = git_workload_image
    source, project = tmp_path / "repository", tmp_path / "project"
    source.mkdir(); project.mkdir()
    git_path = shutil.which("git")
    def git(*args):
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        return subprocess.run([git_path, "-C", str(source), "-c", "user.name=Runtime Test", "-c",
            "user.email=runtime@example.invalid", "-c", "commit.gpgsign=false", *args], check=True,
            text=True, capture_output=True, env=env).stdout.strip()
    git("init"); git("checkout", "-b", "main")
    (source / "code.txt").write_text("native-git-checkout\n")
    git("add", "."); git("commit", "-m", "native fixture")
    revision = git("rev-parse", "HEAD")
    worker = dict(worker)
    if delayed:
        wrapper = tmp_path / "delayed-git"
        wrapper.write_text('#!/bin/busybox sh\nfor arg in "$@"; do\n if [ "$arg" = clone ]; then\n  printf "git-worker-waiting\\n" >&2\n  /bin/busybox sleep 20\n fi\ndone\nexec ' + worker["git"] + ' "$@"\n')
        wrapper.chmod(0o700)
        image = RuntimeImage.from_files([(one.source, one.target, one.executable) for one in image.files] +
                                        [(wrapper, "/qavector/delayed-git", True)])
        worker["git"] = "/qavector/delayed-git"
    raw = {"api_version": "qavector.io/v1", "kind": "RuntimeDeployment", "metadata": {"name": "local-git"}, "spec": {
        "provider": "linux-isolated", "working_directory": "/workspace", "workers": {"git.checkout": worker},
        "policy": {"filesystem": [{"binding": "project", "access": "read_write"}, {"binding": "repository", "access": "read_only"}]},
        "bindings": [{"name": "project", "source": str(project), "target": "/workspace"},
                     {"name": "repository", "source": str(source), "target": "/repository"}],
        "backend_options": {"linux": {"state_directory": str(tmp_path / "state"),
            "image": {"identity": image.identity, "files": [vars(one) for one in image.files]}}}}}
    registry = DeploymentRegistry(tmp_path / "control")
    deployment = registry.save(RuntimeDeployment(raw))
    def factory(one, protected):
        backend = one.backend(protected)
        backend._bundle, backend.launcher = native[0]._bundle, native[0].launcher
        return backend
    return registry, deployment, factory, source, project, revision


@pytest.mark.parametrize("ending", ["natural", "stop", "timeout"])
def test_native_git_checkout_and_shell_share_runtime_policy_and_confirm_workspace_cleanup(native, git_workload_image, tmp_path, monkeypatch, ending):
    from cycle import executor, model, runtime as cycle_runtime
    from cycle.bus import EventObserver
    from cycle.context import CancelToken
    from execution_runtime.registry import EnvironmentRegistry
    registry, deployment, factory, source, project, revision = git_deployment(native, git_workload_image, tmp_path, ending != "natural")
    original = cycle_runtime.CycleRuntime
    monkeypatch.setattr(cycle_runtime, "CycleRuntime", lambda *args, **kw: original(*args, **kw,
        deployments=registry, profiles=EnvironmentRegistry(tmp_path / "profiles"), backend_factory=factory))
    token, waiting = CancelToken(), []
    def event(kind, **fields):
        if kind == "cycle.step.log" and any("git-worker-waiting" in line for line in fields["lines"]):
            waiting.append(True)
            if ending == "stop":
                token.set("stop Git checkout")
    raw = {"id": "git-runtime", "execution": {"runtime": {"mode": "isolated", "deployment": deployment.name,
        "revision": deployment.digest, "project_directory": str(project)}}, "steps": [
        {"id": "checkout", "plugin": "git.checkout", "timeout": 2 if ending == "timeout" else 15,
         "with": {"repository": "/repository", "branch": "main", "path": "source/checkout"}},
        {"id": "inspect", "plugin": "command.shell", "needs": ["checkout"], "runtime": {"overrides": {
            "filesystem": [{"binding": "project", "access": "read_only"}, {"binding": "repository", "access": "read_only"}]}},
         "with": {"dir": "${steps.checkout.outputs.workspace}", "command": ["/bin/busybox", "sh", "-ec",
             '/bin/busybox cat code.txt; ! (printf wrong > code.txt); test -z "$HOST_GIT_SECRET"; /bin/probe socket 2']}}]}
    run = executor.run_cycle(model.parse_cycle(raw, "git-runtime"), str(tmp_path / "run"), cancel=token,
        environment={"HOST_GIT_SECRET": "must-not-leak"}, observer=EventObserver(sink=event))
    step = run.steps["checkout"]
    assert step.status == {"natural": "success", "stop": "cancelled", "timeout": "timeout"}[ending], step.message
    assert step.runtime["cleanup_confirmed"] and step.runtime["workspace_cleanup_confirmed"], step.runtime
    assert step.runtime["cleanup_instance_id"] != step.runtime["instance_id"]
    assert not list(project.rglob(".qavector-checkout-*"))
    assert (source / "code.txt").read_text() == "native-git-checkout\n"
    assert not list((tmp_path / "state").glob("*/root"))
    if ending == "natural":
        assert run.ok and step.outputs["commit"] == revision
        assert step.outputs["workspace"] == "/workspace/source/checkout"
        assert (project / "source/checkout/code.txt").read_text() == "native-git-checkout\n"
        assert "socket=-1 errno=1" in run.steps["inspect"].outputs["stdout_tail"]
    else:
        assert waiting and not (project / "source/checkout").exists()


@pytest.mark.parametrize("ending", ["natural", "timeout", "stop"])
def test_cycle_shell_uses_real_isolation_and_cleans_before_reporting(native, tmp_path, monkeypatch, ending):
    import threading
    from cycle import executor, model, runtime as cycle_runtime
    from cycle.bus import EventObserver
    from cycle.context import CancelToken
    from execution_runtime.registry import EnvironmentRegistry
    registry, deployment, factory, packet, created = service_deployment(native, tmp_path)
    original = cycle_runtime.CycleRuntime
    monkeypatch.setattr(cycle_runtime, "CycleRuntime", lambda *args, **kw: original(*args, **kw,
        deployments=registry, profiles=EnvironmentRegistry(tmp_path / "profiles"), backend_factory=factory))
    token, live = CancelToken(), threading.Event()
    workspace = tmp_path / "run-control"
    workspace.mkdir()
    (workspace / "control-secret").write_text("must-not-be-visible")
    script = ('/bin/busybox cat approved; ! (printf changed > approved); '
              'test -z "$HOST_ONLY"; test ! -e "' + str(workspace / "control-secret") + '"; '
              '/bin/probe socket 2; printf "cycle-native-ok\\n"; '
              '/bin/busybox sleep ' + ("0.1" if ending == "natural" else "20"))
    definition = model.parse_cycle({"id": "runtime", "execution": {"runtime": {
        "mode": "isolated", "deployment": deployment.name, "revision": deployment.digest,
        "project_directory": packet["project_directory"]}}, "steps": [{"id": "a", "plugin": "command.shell",
        "timeout": 1 if ending == "timeout" else 10,
        "with": {"command": ["/bin/busybox", "sh", "-ec", script]}}]}, "runtime")
    def event(kind, **fields):
        if kind == "cycle.step.stage" and fields["phase"] == "runtime" and fields["status"] == "running":
            live.set()
        if ending == "stop" and kind == "cycle.step.log" and any("cycle-native-ok" in one for one in fields["lines"]):
            token.set("test stop")
    result = executor.run_cycle(definition, str(workspace), cancel=token,
        environment={"HOST_ONLY": "host-credential"}, observer=EventObserver(sink=event))
    step = result.steps["a"]
    assert live.is_set(), step.message
    assert step.status == {"natural": "success", "timeout": "timeout", "stop": "cancelled"}[ending], step.message
    assert step.runtime["enforcement"] == "verified" and step.runtime["cleanup_confirmed"]
    assert "cycle-native-ok" in step.outputs["stdout_tail"] and "socket=-1 errno=1" in step.outputs["stdout_tail"]
    assert (Path(packet["project_directory"]) / "approved").read_text() == "approved"
    assert not list((tmp_path / "state").glob("*/root"))
    receipts = [json.loads(path.read_text()) for path in (tmp_path / "state" / "audit").glob("*.json")]
    assert {one["kind"] for one in receipts} >= {"runtime.started", "runtime.exited", "runtime.cleaned"}
    assert "host-credential" not in json.dumps(receipts)
    for artifact in step.artifacts:
        assert (workspace / artifact.path).is_file()


def test_frozen_git_worker_artifact_and_heterogeneous_cycle(native, git_workload_image, tmp_path, request):
    from execution_runtime.deployments import DeploymentRegistry
    core = request.config.getoption("--runtime-frozen-core")
    if not core:
        pytest.skip("Pass --runtime-frozen-core for packaged Git worker acceptance")
    _, deployment, _, source, project, revision = git_deployment(native, git_workload_image, tmp_path)
    home, cycles = tmp_path / "user-data", tmp_path / "cycles"
    DeploymentRegistry(home / "execution-deployments").save(deployment)
    cycles.mkdir()
    raw = {"id": "git-runtime", "execution": {"runtime": {"mode": "isolated", "deployment": deployment.name,
        "revision": deployment.digest, "project_directory": str(project)}}, "steps": [
        {"id": "checkout", "plugin": "git.checkout", "timeout": 15, "with": {"repository": "/repository", "branch": "main"}},
        {"id": "inspect", "plugin": "command.shell", "needs": ["checkout"], "with": {
            "command": ["/bin/busybox", "cat", "code.txt"], "dir": "${steps.checkout.outputs.workspace}"}}]}
    (cycles / "git-runtime.yaml").write_text(json.dumps(raw))
    env = dict(os.environ, CMS_HOME=str(home), PATH="/no-system-bwrap")
    done = subprocess.run([core, "--cycle-run=git-runtime", "--cycles-dir=" + str(cycles), "--events=-"],
                          capture_output=True, text=True, timeout=30, env=env)
    events = [json.loads(line) for line in done.stdout.splitlines()]
    assert done.returncode == 0, (events, done.stderr)
    completed = {one["step"]: one for one in events if one["kind"] == "cycle.step.end"}
    assert completed["checkout"]["outputs"]["commit"] == revision
    assert completed["checkout"]["runtime"]["workspace_cleanup_confirmed"]
    assert "native-git-checkout" in completed["inspect"]["outputs"]["stdout_tail"]
    assert not list(project.rglob(".qavector-checkout-*"))
    preview = subprocess.run([core, "--runtime-service-policy=" + deployment.name, "--runtime-service-revision=" + deployment.digest],
        input=json.dumps({"project_directory": str(project)}), capture_output=True, text=True, timeout=15, env=env)
    assert json.loads(preview.stdout)["workers"]["git.checkout"]["available"]


def agent_deployment(native, git_workload_image, tmp_path, credentials=False):
    from execution_runtime.deployments import RuntimeDeployment
    from runtime_git_image import review_image
    registry, deployment, factory, source, project, _ = git_deployment(native, git_workload_image, tmp_path)
    image, worker = review_image(git_workload_image[0], git_workload_image[1]["argv"][0])
    raw = deployment.to_document()
    raw["spec"]["workers"]["agent.review"] = worker
    raw["spec"]["policy"]["resources"] = {"scratch_bytes": 4 * 1024 * 1024}
    if credentials:
        raw["spec"]["policy"]["secrets"] = ["PROVIDER_TOKEN", "OTHER_TOKEN"]
        raw["spec"]["workers"]["agent.review"]["credentials"] = {"api_key": "PROVIDER_TOKEN"}
    raw["spec"]["backend_options"]["linux"]["image"] = {"identity": image.identity, "files": [vars(one) for one in image.files]}
    deployment = registry.save(RuntimeDeployment(raw), deployment.digest)
    test_code = 'import pathlib, unittest\nclass Fixture(unittest.TestCase):\n def test_checkout(self):\n  self.assertEqual(pathlib.Path("code.txt").read_text(), "native-git-checkout\\n")\n'
    if credentials:
        test_code += '  self.assertFalse(pathlib.Path(' + repr(secret_path("PROVIDER_TOKEN")) + ').exists())\n'
    (source / "test_fixture.py").write_text(test_code)
    for args in (("add", "test_fixture.py"), ("commit", "-m", "test command fixture")):
        subprocess.run(["git", "-C", str(source), "-c", "user.name=Runtime Test", "-c", "user.email=runtime@example.invalid",
                        "-c", "commit.gpgsign=false", *args], capture_output=True, check=True)
    revision = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    return registry, deployment, factory, source, project, revision


def agent_workflow(deployment, project, control, ending="natural"):
    ro = {"filesystem": [{"binding": "project", "access": "read_only"}, {"binding": "repository", "access": "read_only"}]}
    interpreter = deployment.to_document()["spec"]["workers"]["agent.review"]["argv"][0]
    document = {"id": "agent-runtime", "execution": {"runtime": {"mode": "isolated", "deployment": deployment.name,
        "revision": deployment.digest, "project_directory": str(project)}}, "steps": [
        {"id": "checkout", "plugin": "git.checkout", "with": {"repository": "/repository", "branch": "main"}},
        {"id": "review", "plugin": "agent.review", "needs": ["checkout"], "runtime": {"overrides": ro},
         # The deadline includes native worker/Python startup. Allow that setup
         # on a loaded CI runner, then expire during the fixture's 20 s wait.
         "timeout": 5 if ending == "timeout" else 15, "with": {"framework": "runtime",
            "model": "local-fixture" if ending == "natural" else "delayed-fixture", "task": "Review local fixture evidence.",
            "repository": "${steps.checkout.outputs.workspace}", "files": ["source/checkout/code.txt"], "scope": True, "verdict": True,
            "inputs": {"native_checks": True, "control_directory": str(control)}}},
        {"id": "tests", "plugin": "command.shell", "needs": ["review"], "runtime": {"overrides": ro}, "with": {
            "dir": "${steps.checkout.outputs.workspace}", "command": [interpreter, "-I", "-B", "-m", "unittest", "discover", "-s", ".", "-p", "test_fixture.py"]}}]}
    if deployment.to_document()["spec"]["workers"]["agent.review"].get("credentials"):
        document["variables"] = {"PROVIDER_TOKEN": {"secret": True}, "OTHER_TOKEN": {"secret": True}}
        document["steps"][0]["runtime"] = {"overrides": {"secrets": []}}
        document["steps"][1]["runtime"]["overrides"] = dict(ro, secrets=["PROVIDER_TOKEN"])
        document["steps"][1]["with"]["inputs"].update(native_credentials=True, denied_credential_path=secret_path("OTHER_TOKEN"))
        document["steps"][2]["runtime"]["overrides"] = dict(ro, secrets=[])
    return document


@pytest.mark.parametrize("ending", ["natural", "stop", "timeout"])
@pytest.mark.parametrize("credentials", [False, True], ids=["no-credentials", "scoped-credentials"])
def test_native_agent_review_and_test_command_follow_git_with_read_only_policy(native, git_workload_image, tmp_path, monkeypatch, ending, credentials):
    from cycle import executor, model, runtime as cycle_runtime
    from cycle.bus import EventObserver
    from cycle.context import CancelToken
    from execution_runtime.registry import EnvironmentRegistry
    registry, deployment, factory, source, project, revision = agent_deployment(native, git_workload_image, tmp_path, credentials)
    original = cycle_runtime.CycleRuntime
    monkeypatch.setattr(cycle_runtime, "CycleRuntime", lambda *a, **kw: original(*a, **kw, deployments=registry,
        profiles=EnvironmentRegistry(tmp_path / "profiles"), backend_factory=factory))
    control = tmp_path / "run-control"
    token, waiting, logs = CancelToken(), [], []
    def event(kind, **fields):
        if kind == "cycle.step.log":
            logs.extend(fields["lines"])
            if any("review-provider-waiting" in one for one in fields["lines"]):
                waiting.append(True)
                if ending == "stop":
                    token.set("Stop isolated review")
    sensitive = 'provider-secret-\u043a\u043b\u044e\u0447-"quote"-\\slash\nend'
    run = executor.run_cycle(model.parse_cycle(agent_workflow(deployment, project, control, ending), "agent-runtime"),
        str(control), cancel=token, environment={"HOST_AGENT_SECRET": "never-forward"}, observer=EventObserver(sink=event),
        secrets={"PROVIDER_TOKEN": sensitive, "OTHER_TOKEN": "unrelated-provider-secret"})
    step = run.steps["review"]
    assert step.status == {"natural": "success", "stop": "cancelled", "timeout": "timeout"}[ending], step.message
    assert step.runtime["cleanup_confirmed"] and step.runtime["enforcement"] == "verified"
    assert run.steps["checkout"].outputs["commit"] == revision
    assert not list((tmp_path / "state").glob("*/root"))
    assert not (project / "source/checkout/must-not-write").exists()
    assert (source / "code.txt").read_text() == "native-git-checkout\n"
    if credentials:
        assert step.runtime["credential_references"] == {"api_key": "PROVIDER_TOKEN"}
        combined = json.dumps(logs) + json.dumps(step.outputs)
        assert sensitive not in combined and json.dumps(sensitive)[1:-1] not in combined
        assert "unrelated-provider-secret" not in combined
        assert any("review-native-credentials-ok" in line for line in logs)
        for directory in (control, tmp_path / "state"):
            for path in directory.rglob("*"):
                if path.is_file():
                    raw = path.read_bytes()
                    assert sensitive.encode() not in raw and json.dumps(sensitive)[1:-1].encode() not in raw
    if ending == "natural":
        assert run.ok and run.steps["tests"].outputs["exit_code"] == 0
        assert any("review-native-boundary-ok" in one for one in logs)
        assert step.outputs["ready"] is True and step.outputs["out_of_scope_count"] == 1
        assert (control / step.outputs["report_path"]).is_file()
        assert any("Ran 1 test" in one for one in logs)
    else:
        assert waiting and not run.steps["tests"].outputs


@pytest.mark.parametrize("credentials", [False, True], ids=["no-credentials", "scoped-credentials"])
def test_frozen_core_runs_git_agent_review_and_real_unit_test(native, git_workload_image, tmp_path, request, credentials):
    from execution_runtime.deployments import DeploymentRegistry
    core = request.config.getoption("--runtime-frozen-core")
    if not core:
        pytest.skip("Pass --runtime-frozen-core for packaged agent worker acceptance")
    _, deployment, _, _, project, _ = agent_deployment(native, git_workload_image, tmp_path, credentials)
    home, cycles = tmp_path / "user-data", tmp_path / "cycles"
    DeploymentRegistry(home / "execution-deployments").save(deployment)
    cycles.mkdir()
    raw = agent_workflow(deployment, project, home / "runs")
    (cycles / "agent-runtime.yaml").write_text(json.dumps(raw))
    env = dict(os.environ, CMS_HOME=str(home), PATH="/no-system-bwrap", HOST_AGENT_SECRET="never-forward")
    if credentials:
        secret_input = tmp_path / "secret-input.json"
        fd = os.open(secret_input, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"value": "frozen-provider-sensitive-token"}, stream)
            saved = subprocess.run([core, "--cycle-secret-set=agent-runtime:PROVIDER_TOKEN", "--from=" + str(secret_input)],
                                   env=env, capture_output=True, text=True, timeout=15)
            assert saved.returncode == 0, (saved.stdout, saved.stderr)
        finally:
            secret_input.unlink()
    done = subprocess.run([core, "--cycle-run=agent-runtime", "--cycles-dir=" + str(cycles), "--events=-"],
                          capture_output=True, text=True, timeout=30, env=env)
    events = [json.loads(line) for line in done.stdout.splitlines()]
    assert done.returncode == 0, (events, done.stderr)
    completed = {one["step"]: one for one in events if one["kind"] == "cycle.step.end"}
    assert completed["review"]["outputs"]["ready"] is True
    assert completed["review"]["runtime"]["cleanup_confirmed"]
    assert completed["tests"]["outputs"]["exit_code"] == 0
    assert "never-forward" not in done.stdout
    assert "frozen-provider-sensitive-token" not in done.stdout
    if credentials:
        assert completed["review"]["runtime"]["credential_references"] == {"api_key": "PROVIDER_TOKEN"}
        assert "[redacted]" in completed["review"]["outputs"]["summary"]


def test_frozen_cycle_command_runs_with_bundled_runtime_and_offline_policy(native, tmp_path, request):
    from execution_runtime.deployments import DeploymentRegistry
    core = request.config.getoption("--runtime-frozen-core")
    if not core:
        pytest.skip("Pass --runtime-frozen-core for packaged Cycle acceptance")
    _, deployment, _, packet, _ = service_deployment(native, tmp_path)
    home = tmp_path / "user-data"
    DeploymentRegistry(home / "execution-deployments").save(deployment)
    directory = tmp_path / "cycles"
    directory.mkdir()
    raw = {"id": "runtime", "execution": {"runtime": {"mode": "isolated", "deployment": deployment.name,
        "revision": deployment.digest, "project_directory": packet["project_directory"]}}, "steps": [{
        "id": "a", "plugin": "command.shell", "timeout": 10, "with": {"command": ["/bin/busybox", "sh", "-ec",
        '/bin/probe socket 2; /bin/busybox cat approved; test -z "$HOST_ONLY"; printf "frozen-cycle-ok\\n"']}}]}
    (directory / "runtime.yaml").write_text(json.dumps(raw))
    finished = subprocess.run([core, "--cycle-run=runtime", "--cycles-dir=" + str(directory), "--events=-"],
        capture_output=True, text=True, timeout=30, env=dict(os.environ, CMS_HOME=str(home),
            HOST_ONLY="never-forward-to-cycle", PATH="/no-system-bwrap"))
    events = [json.loads(line) for line in finished.stdout.splitlines()]
    assert finished.returncode == 0, (events, finished.stderr)
    ended = next(one for one in events if one["kind"] == "cycle.step.end")
    assert ended["runtime"]["enforcement"] == "verified" and ended["runtime"]["cleanup_confirmed"]
    assert "frozen-cycle-ok" in ended["outputs"]["stdout_tail"]
    assert "socket=-1 errno=1" in ended["outputs"]["stdout_tail"]
    assert "never-forward-to-cycle" not in finished.stdout
    assert not list((tmp_path / "state").glob("*/root"))


def test_native_project_boundary_and_absolute_symlink_escape(native, tmp_path):
    first, second = tmp_path / "project-a", tmp_path / "project-b"
    first.mkdir(); second.mkdir()
    (first / "allowed").write_text("allowed content")
    (second / "private").write_text("synthetic private content")
    (first / "escape").symlink_to(second / "private")
    result = run(native, ("/bin/busybox", "sh", "-c",
        '/bin/busybox cat allowed; ! /bin/busybox cat "$1"; ! /bin/busybox cat escape', "test", str(second / "private")), first)
    assert result["exit_code"] == 0, result
    assert "allowed content" in result["stdout_tail"]
    assert "synthetic private content" not in result["stdout_tail"]


def test_native_read_only_and_read_write_are_actually_enforced(native, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    result = run(native, ("/bin/busybox", "sh", "-c", "printf blocked > output"), project)
    assert result["exit_code"] != 0 and not (project / "output").exists()
    result = run(native, ("/bin/busybox", "sh", "-c", "printf allowed > output"), project, "read_write")
    assert result["exit_code"] == 0 and (project / "output").read_text() == "allowed"


def test_native_private_root_proc_and_tmp_are_read_only(native):
    result = run(native, ("/bin/busybox", "sh", "-c",
        '! printf bad > /bin/busybox; ! printf bad > /tmp/new; ! printf bad > /proc/sys/kernel/hostname'))
    assert result["exit_code"] == 0, result


def test_native_bundled_launch_never_searches_for_system_bwrap(native, monkeypatch):
    backend, _ = native
    if backend.launcher is not None:
        pytest.skip("This acceptance case requires bundled runtime resources")
    original = shutil.which
    def lookup(command, *args, **kwargs):
        if command == "bwrap":
            pytest.fail("Bundled execution must not resolve a system launcher")
        return original(command, *args, **kwargs)
    monkeypatch.setattr(shutil, "which", lookup)
    monkeypatch.setenv("PATH", "/qavector-no-system-tools")
    result = run(native, ("/bin/busybox", "echo", "bundled workload"))
    assert result["exit_code"] == 0 and result["stdout_tail"] == "bundled workload\n"


def test_native_system_provider_is_explicit_and_keeps_the_same_offline_guarantees(native, tmp_path):
    if not shutil.which("bwrap"):
        pytest.skip("Optional system provider is not installed on clean bundled acceptance host")
    original, _ = native
    backend = SystemBubblewrapBackend(original.image, tmp_path / "system-state")
    assert backend.provider != original.provider
    result = run((backend, tmp_path), ("/bin/busybox", "echo", "explicit system provider"))
    assert result["exit_code"] == 0 and result["provider"] == "linux-system-isolated"
    metadata = json.loads(Path(result["artifacts"][0]).read_text())
    assert metadata["provider"] == "linux-system-isolated"


def test_native_host_proc_and_control_files_are_not_visible(native):
    result = run(native, ("/bin/busybox", "sh", "-c",
        '! /bin/busybox cat /proc/$1/environ; ! /bin/busybox ls /home; ! /bin/busybox ls /sys/fs/cgroup',
        "test", str(os.getpid())))
    assert result["exit_code"] == 0, result


@pytest.mark.parametrize("family", [1, 2, 10])
def test_native_unix_ipv4_and_ipv6_sockets_are_denied(native, family):
    result = run(native, ("/bin/probe", "socket", str(family)))
    assert result["exit_code"] == 0
    assert "socket=-1 errno=1" in result["stdout_tail"]


def test_native_cannot_create_a_new_user_namespace(native):
    result = run(native, ("/bin/probe", "unshare"))
    assert "unshare=-1 errno=1" in result["stdout_tail"]


def test_native_does_not_inherit_host_fds_or_host_environment(native, tmp_path, monkeypatch):
    monkeypatch.setenv("QAVECTOR_HOST_CREDENTIAL", "synthetic-host-value")
    fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    os.set_inheritable(fd, True)
    try:
        result = run(native, ("/bin/probe", "fds"))
        assert "extra=0" in result["stdout_tail"], result
        env = run(native, ("/bin/busybox", "env"))
        assert "synthetic-host-value" not in env["stdout_tail"]
        assert "QAVECTOR_HOST_CREDENTIAL" not in env["stdout_tail"]
    finally:
        os.close(fd)


def test_native_env_values_are_explicit_and_redacted_in_logs_and_metadata(native):
    result = run(native, ("/bin/busybox", "sh", "-c", 'printf "%s" "$VISIBLE"'),
                 env_values={"VISIBLE": "explicit-value"}, environment=["VISIBLE"])
    assert result["stdout_tail"] == "[redacted]"
    for path in result["artifacts"]:
        assert "explicit-value" not in Path(path).read_text()


def test_native_source_replacement_after_prepare_uses_the_pinned_directory(native, tmp_path):
    backend, _ = native
    project = tmp_path / "project"
    project.mkdir()
    (project / "value").write_text("approved")
    request = ExecutionRequest(policy(backend, project), ("/bin/busybox", "cat", "/workspace/value"), "/", timeout_seconds=3)
    prepared = backend.prepare(request)
    project.rename(tmp_path / "original")
    project.mkdir()
    (project / "value").write_text("replacement")
    handle = backend.start(prepared)
    result = backend.wait(handle)
    backend.cleanup(handle)
    assert result["stdout_tail"] == "approved"


def test_native_timeout_stops_a_waiting_workload(native):
    started = time.monotonic()
    result = run(native, ("/bin/busybox", "sleep", "30"), timeout=0.15)
    assert result["reason"] == "timeout" and time.monotonic() - started < 5


def test_native_logs_are_available_while_the_process_is_running(native):
    backend, _ = native
    request = ExecutionRequest(policy(backend), ("/bin/busybox", "sh", "-c",
        'printf "live output"; /bin/busybox sleep 30'), "/", timeout_seconds=5)
    handle = backend.start(backend.prepare(request))
    try:
        for _ in range(100):
            result = backend.inspect(handle)
            if "live output" in result["stdout_tail"]:
                break
            time.sleep(0.02)
        assert "live output" in result["stdout_tail"] and result["state"] == "running", result
    finally:
        backend.stop(handle, time.monotonic())
        backend.cleanup(handle)


def test_native_offline_service_stop_and_restart_have_no_overlapping_instance(native, tmp_path):
    backend, _ = native
    project = tmp_path / "project"
    project.mkdir()
    script = "/bin/busybox setsid /bin/busybox sleep 30 & printf ready > /workspace/ready; /bin/busybox sleep 30"
    request = ExecutionRequest(policy(backend, project, "read_write"), ("/bin/busybox", "sh", "-c", script), "/", timeout_seconds=8)
    runtime = ExecutionRuntime(backend)
    handle = runtime.start(runtime.prepare(request))
    for _ in range(100):
        if (project / "ready").exists():
            break
        time.sleep(0.02)
    assert (project / "ready").exists()
    job = backend._get(handle)
    init_fd = os.dup(job.init_pidfd)
    try:
        replacement = runtime.restart(handle, request, grace_seconds=0.1)
        assert select.select([init_fd], [], [], 0)[0]
        assert replacement.instance_id != handle.instance_id
        assert runtime.inspect(handle)["state"] == "cleaned"
        runtime.stop(replacement, 0.1)
        runtime.cleanup(replacement)
    finally:
        os.close(init_fd)


def test_native_quota_request_without_delegation_does_not_launch(native):
    backend, _ = native
    effective = policy(backend, resources={"tasks": 16})
    before = len(backend._jobs)
    assert not backend.preflight(effective).ready
    with pytest.raises(RuntimePolicyError):
        backend.prepare(ExecutionRequest(effective, ("/bin/busybox", "true"), "/"))
    assert len(backend._jobs) == before


@pytest.mark.parametrize("settings", [{"network": {"mode": "full"}}, {"tools": ["git"]},
                                    {"integrations": [{"service": "git", "resource": "repo", "actions": ["read"]}]},
                                    {"resources": {"scratch_bytes": 1}},
                                    {"resources": {"memory": {"bytes": 33554432, "accounting": "resident"}}}])
def test_native_unsupported_guarantees_refuse_before_workload_launch(native, settings):
    backend, _ = native
    before = len(backend._jobs)
    effective = policy(backend, **settings)
    assert not backend.preflight(effective).ready
    with pytest.raises(RuntimePolicyError):
        backend.prepare(ExecutionRequest(effective, ("/bin/busybox", "true"), "/"))
    assert len(backend._jobs) == before


def test_native_failed_payload_exec_is_reported_and_cleanup_is_idempotent(native):
    backend, _ = native
    request = ExecutionRequest(policy(backend), ("/missing-program",), "/", timeout_seconds=3)
    handle = backend.start(backend.prepare(request))
    result = backend.wait(handle)
    assert result["exit_code"] != 0 and result["reason"] == "launch_failed", result
    backend.cleanup(handle)
    backend.cleanup(handle)


def test_native_prepared_execution_can_be_cancelled_without_starting(native):
    backend, _ = native
    prepared = backend.prepare(ExecutionRequest(policy(backend), ("/bin/busybox", "true"), "/"))
    backend.cleanup(prepared)
    assert backend.wait(prepared)["reason"] == "preparation_cancelled"
    assert backend._get(prepared).process is None
    with pytest.raises(RuntimePolicyError) as exc:
        backend.start(prepared)
    assert exc.value.code == "prepared_execution_consumed"


def test_native_workspace_source_symlink_is_rejected_before_start(native, tmp_path):
    backend, _ = native
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "link"
    link.symlink_to(actual, target_is_directory=True)
    before = len(backend._jobs)
    with pytest.raises(RuntimePolicyError):
        backend.prepare(ExecutionRequest(policy(backend, link), ("/bin/busybox", "true"), "/"))
    assert len(backend._jobs) == before


def test_native_workspace_cannot_expose_control_plane_state(native):
    backend, root = native
    before = len(backend._jobs)
    with pytest.raises(RuntimePolicyError) as exc:
        backend.prepare(ExecutionRequest(policy(backend, root), ("/bin/busybox", "true"), "/"))
    assert exc.value.code == "protected_path"
    assert len(backend._jobs) == before


@pytest.mark.parametrize("source", ["/proc", "/sys/fs/cgroup", "/dev/shm"])
def test_native_host_kernel_control_and_ipc_filesystems_cannot_be_workspace_grants(native, source):
    backend, _ = native
    before = len(backend._jobs)
    with pytest.raises(RuntimePolicyError) as exc:
        backend.prepare(ExecutionRequest(policy(backend, Path(source), "read_write"),
            ("/bin/busybox", "true"), "/"))
    assert exc.value.code == "protected_path" and len(backend._jobs) == before


def test_native_documented_provisioning_example(native, tmp_path, monkeypatch, capsys):
    from examples.execution_runtime.linux_offline import main
    project = tmp_path / "project"
    project.mkdir()
    state = tmp_path / "state"
    monkeypatch.setattr(sys, "argv", ["linux_offline", "--busybox=/usr/bin/busybox",
        "--workspace=" + str(project), "--state-directory=" + str(state),
        "--bundle-directory=" + str(native[0]._bundle.directory)] +
        (["--launcher=" + native[0].launcher.image.files[0].source] if native[0].launcher else []) +
        ["--", "/bin/busybox", "echo", "example success"])
    assert main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["exit_code"] == 0 and result["stdout_tail"] == "example success\n"
    assert not (state / result["instance_id"] / "root").exists()


def test_native_supervisor_startup_exception_terminates_the_process(native, monkeypatch):
    backend, _ = native
    prepared = backend.prepare(ExecutionRequest(policy(backend), ("/bin/busybox", "sleep", "30"), "/"))
    def fail(capture):
        raise RuntimeError("simulated thread startup failure")
    monkeypatch.setattr(LogCapture, "start", fail)
    with pytest.raises(RuntimePolicyError) as exc:
        backend.start(prepared)
    assert exc.value.code == "supervision_failed"
    job = backend._get(prepared)
    assert job.process.poll() is not None and job.done.is_set()
    backend.cleanup(prepared)


def test_native_recovery_does_not_touch_an_active_instance(native):
    backend, _ = native
    prepared = backend.prepare(ExecutionRequest(policy(backend), ("/bin/busybox", "sleep", "30"), "/", timeout_seconds=5))
    handle = backend.start(prepared)
    try:
        # Keep only the trusted launcher's inherited lease: prove the kernel
        # lock remains held independently of the host Python descriptor.
        job = backend._get(handle)
        os.close(job.lease_fd)
        job.lease_fd = None
        other = LinuxBackend(backend.image, backend.state_directory, launcher=backend.launcher, bundle_directory=backend._bundle.directory)
        result = other.recover()
        assert handle.instance_id in result["active"]
        assert (backend.state_directory / handle.instance_id / "root").is_dir()
    finally:
        backend.stop(handle, time.monotonic())
        backend.cleanup(handle)


@pytest.mark.parametrize("phase", ["prepared", "running"])
def test_native_sigkill_supervisor_terminates_descendants_and_recovers_artifacts(native, tmp_path, phase):
    original, _ = native
    project, state = tmp_path / "project", tmp_path / "state"
    project.mkdir()
    launcher_path = original.launcher.image.files[0].source if original.launcher else None
    resources = {"scratch_bytes": 16384} if original._scratch_result[0] else {}
    script = r'''
import json, sys, time
from pathlib import Path
from execution_runtime import PolicyLayer, PolicyPatch, PolicyResolver
from execution_runtime.backends.base import ExecutionRequest
from execution_runtime.backends.linux import ApprovedLauncher, LinuxBackend, RuntimeImage
project, state, phase, launcher, resources, bundle = json.loads(sys.argv[1])
image = RuntimeImage.from_files([('/usr/bin/busybox','/bin/busybox',True)])
backend = LinuxBackend(image, state, launcher=ApprovedLauncher.from_file(launcher) if launcher else None, bundle_directory=bundle)
effective = PolicyResolver().resolve([PolicyLayer('platform',PolicyPatch({'resources': resources,
    'filesystem':[{'binding':'project','access':'read_write'}]}))],
    [{'name':'project','source':project,'target':'/workspace'}],platform='linux',provider=backend.provider,
    runtime_root_identity=image.identity)
command = '/bin/busybox setsid /bin/busybox sleep 30 & printf ready > /workspace/ready; /bin/busybox sleep 30'
handle = backend.prepare(ExecutionRequest(effective,('/bin/busybox','sh','-c',command),'/'))
if phase == 'running':
    handle = backend.start(handle)
    end = time.monotonic()+5
    while time.monotonic()<end:
        metadata = json.loads((Path(state)/handle.instance_id/'runtime.json').read_text())
        if metadata['recovery']['init'] and (Path(project)/'ready').exists(): break
        time.sleep(.01)
else:
    metadata = json.loads((Path(state)/handle.instance_id/'runtime.json').read_text())
print(json.dumps({'id':handle.instance_id,'init':metadata['recovery']['init']}),flush=True)
while True: time.sleep(1)
'''
    host = subprocess.Popen([sys.executable, "-c", script, json.dumps([str(project), str(state), phase, launcher_path, resources, str(original._bundle.directory)])],
        cwd=str(Path(__file__).resolve().parents[1]), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    init_fd = None
    try:
        assert select.select([host.stdout], [], [], 10)[0], "Supervisor did not publish readiness"
        line = host.stdout.readline()
        assert line, host.communicate(timeout=2)
        info = json.loads(line)
        if phase == "running":
            assert info["init"] is not None
            init_fd = os.pidfd_open(info["init"]["pid"])
        host.kill()
        host.wait(timeout=3)
        if init_fd is not None:
            assert select.select([init_fd], [], [], 5)[0], "PID namespace survived host SIGKILL"
        backend = LinuxBackend(original.image, state, launcher=original.launcher)
        result = backend.recover()
        assert info["id"] in result["recovered"], result
        directory = state / info["id"]
        metadata = json.loads((directory / "runtime.json").read_text())
        assert metadata["state"] == "cleaned" and not (directory / "root").exists()
        assert metadata["reason"] == ("supervisor_lost" if phase == "running" else "preparation_abandoned")
        assert not backend.recover()["recovered"]
    finally:
        if host.poll() is None:
            host.kill()
        host.communicate(timeout=3)
        if init_fd is not None:
            os.close(init_fd)


def test_native_shutdown_cleans_started_and_prepared_instances_and_refuses_new_work(native, tmp_path):
    original, _ = native
    backend = LinuxBackend(original.image, tmp_path / "state", launcher=original.launcher, bundle_directory=original._bundle.directory)
    runtime = ExecutionRuntime(backend)
    request = ExecutionRequest(policy(backend), ("/bin/busybox", "sleep", "30"), "/")
    prepared = runtime.prepare(request)
    running = runtime.start(runtime.prepare(request))
    runtime.shutdown(grace_seconds=0)
    assert runtime.inspect(prepared)["state"] == runtime.inspect(running)["state"] == "cleaned"
    runtime.shutdown(grace_seconds=0)
    with pytest.raises(RuntimePolicyError) as exc:
        runtime.prepare(request)
    assert exc.value.code == "runtime_closed"


def test_native_cleanup_releases_lease_and_can_retry_metadata_after_io_failure(native, monkeypatch):
    backend, _ = native
    prepared = backend.prepare(ExecutionRequest(policy(backend), ("/bin/busybox", "true"), "/"))
    original = backend._persist
    def fail(job):
        raise OSError("simulated artifact storage failure")
    monkeypatch.setattr(backend, "_persist", fail)
    with pytest.raises(OSError):
        backend.cleanup(prepared)
    assert backend._get(prepared).lease_fd is None
    monkeypatch.setattr(backend, "_persist", original)
    backend.cleanup(prepared)
    record = json.loads((backend.state_directory / prepared.instance_id / "runtime.json").read_text())
    assert record["state"] == "cleaned"


def test_recovery_retains_an_incomplete_launch_instead_of_guessing_process_ownership(tmp_path):
    from execution_runtime.backends.linux.recovery import new_instance
    from execution_runtime.registry import _atomic
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    identity = "a" * 32
    directory, lease = new_instance(state, identity, "b" * 64, "sha256:root", None)
    record = json.loads((directory / "runtime.json").read_text())
    record["state"] = "launching"
    record["recovery"]["owner"]["start_ticks"] += 1
    _atomic(directory / "runtime.json", record)
    os.close(lease)
    root = directory / "root"
    root.mkdir()
    result = LinuxBackend(state_directory=state).recover()
    assert result["retained"] == [{"instance_id": identity, "reason": "termination_unconfirmed"}]
    assert root.is_dir()


@pytest.fixture(scope="module")
def scratch_native(request, native):
    if not request.config.getoption("--linux-runtime-scratch-integration"):
        pytest.skip("Explicit scratch quota integration was not requested")
    backend, _ = native
    report = backend.preflight(policy(backend, resources={"scratch_bytes": 16384}))
    assert report.ready, report.to_document()
    return native


def test_native_scratch_quota_is_aggregate_reusable_and_keeps_runtime_root_read_only(scratch_native):
    script = ('/bin/busybox dd if=/dev/zero of=/tmp/first bs=4096 count=2 && '
        '/bin/busybox dd if=/dev/zero of=/tmp/second bs=4096 count=2 && '
        '! /bin/busybox dd if=/dev/zero of=/tmp/overflow bs=4096 count=1 && '
        '/bin/busybox rm /tmp/first && /bin/busybox dd if=/dev/zero of=/tmp/reuse bs=4096 count=2 && '
        '! printf bad > /bin/busybox')
    result = run(scratch_native, ("/bin/busybox", "sh", "-c", script), resources={"scratch_bytes": 16384})
    assert result["exit_code"] == 0, result
    assert result["scratch"]["capacity_bytes"] == 16384
    assert "No space left on device" in result["stderr_tail"]


def test_native_scratch_quota_is_shared_with_descendant_processes(scratch_native):
    script = ('/bin/busybox dd if=/dev/zero of=/tmp/first bs=4096 count=3 && '
        '/bin/busybox sh -c "/bin/busybox dd if=/dev/zero of=/tmp/child bs=4096 count=2"; '
        'test $? -ne 0 && test "$(/bin/busybox stat -c %s /tmp/child)" = 4096')
    result = run(scratch_native, ("/bin/busybox", "sh", "-c", script), resources={"scratch_bytes": 16384})
    assert result["exit_code"] == 0, result


def test_native_scratch_data_never_carries_over_to_the_next_instance(scratch_native):
    first = run(scratch_native, ("/bin/busybox", "sh", "-c", "printf private > /tmp/previous"), resources={"scratch_bytes": 16384})
    second = run(scratch_native, ("/bin/busybox", "sh", "-c", "test ! -e /tmp/previous"), resources={"scratch_bytes": 16384})
    assert first["exit_code"] == second["exit_code"] == 0


def test_native_scratch_has_no_unbounded_fallback_with_an_old_launcher(native):
    backend, _ = native
    if backend._scratch_result[0]:
        assert backend.preflight(policy(backend, resources={"scratch_bytes": 16384})).ready
        return  # Positive enforcement is required separately by the scratch acceptance flag.
    before = len(backend._jobs)
    with pytest.raises(RuntimePolicyError) as exc:
        backend.prepare(ExecutionRequest(policy(backend, resources={"scratch_bytes": 16384}),
            ("/bin/busybox", "true"), "/"))
    assert exc.value.code == "isolation_unavailable" and len(backend._jobs) == before


@pytest.fixture(scope="module")
def resource_native(request, native):
    if not request.config.getoption("--linux-runtime-resource-integration"):
        pytest.skip("Explicit cgroup pressure integration was not requested")
    parent = request.config.getoption("--linux-runtime-cgroup-parent")
    assert parent, "Resource integration requires --linux-runtime-cgroup-parent; no host reconfiguration is performed"
    original, root = native
    backend = LinuxBackend(original.image, root / "resource-state", cgroup_parent=parent, launcher=original.launcher, bundle_directory=original._bundle.directory)
    # Each test's prepare/preflight requires its own controls. One missing
    # controller must not prevent proving independent memory/task enforcement.
    # The strict CPU test still fails without CPU delegation; no skip fallback.
    report = backend.preflight(policy(backend))
    assert report.ready, report.to_document()
    yield backend, root
    for job in backend._jobs.values():
        if job.state != "cleaned":
            if job.process is not None and not job.done.is_set():
                backend.stop(job.token, time.monotonic())
            backend.cleanup(job.token)


def test_native_cgroup_cpu_throttling_and_usage(resource_native):
    result = run(resource_native, ("/bin/probe", "cpu"), timeout=8,
                 resources={"cpu_cores": 0.1, "tasks": 16})
    cpu = result["resource_history"][-1]["usage"]["cpu"]
    assert result["exit_code"] == 0, result
    assert cpu["nr_throttled"] > 0 and 0 < cpu["usage_usec"] < 1000000, cpu


def test_native_cpu_request_cannot_bypass_missing_controller(resource_native):
    backend, _ = resource_native
    requested = policy(backend, resources={"cpu_cores":0.1})
    report = backend.preflight(requested)
    if report.ready:
        assert next(one for one in report.checks if one.name == "limit_cpu_cores").status == "verified"
        return  # Positive throttling is required separately on a CPU-delegated runner.
    before = len(backend._jobs)
    with pytest.raises(RuntimePolicyError) as error:
        backend.prepare(ExecutionRequest(requested,("/bin/probe","cpu"),"/",timeout_seconds=3))
    assert error.value.code == "isolation_unavailable" and len(backend._jobs) == before


def test_native_cgroup_memory_oom_is_reported(resource_native):
    result = run(resource_native, ("/bin/probe", "memory"), timeout=8,
        resources={"tasks": 16, "memory": {"bytes": 33554432, "accounting": "charged"}})
    assert result["reason"] == "memory_limit", result
    assert result["resource_history"][-1]["usage"]["memory_events"]["oom_kill"] > 0


@pytest.mark.parametrize("kind", ["processes", "threads"])
def test_native_cgroup_tasks_budget_includes_processes_and_threads(resource_native, kind):
    result = run(resource_native, ("/bin/probe", kind), timeout=8, resources={"tasks": 16})
    assert result["exit_code"] == 0 and "errno=11" in result["stdout_tail"], result
    assert result["resource_history"][-1]["usage"]["pids_events"]["max"] > 0
