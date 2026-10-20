"""Run only in the disposable runner with its own cgroup bind, never on host."""

import json
import os
from pathlib import Path
import time
import traceback

from execution_runtime import PolicyLayer, PolicyPatch, PolicyResolver
from execution_runtime.backends.base import ExecutionRequest
from execution_runtime.backends.linux import LinuxBackend, RuntimeImage


def main():
    group = Path("/sys/fs/cgroup")
    assert os.environ.get("QAVECTOR_OWNED_ACCEPTANCE") == "1"
    assert {"cpu","memory","pids"} <= set((group / "cgroup.controllers").read_text().split())
    control = group / "control"
    control.mkdir()
    # The PID namespace is shared only with the owned anchor. Empty its scope
    # before enabling controllers; runner itself lives in a different scope.
    for pid in (group / "cgroup.procs").read_text().split():
        (control / "cgroup.procs").write_text(pid)
    assert not (group / "cgroup.procs").read_text().strip()
    (group / "cgroup.subtree_control").write_text("+cpu +memory +pids")
    parent = group / "runtime"
    parent.mkdir()
    (parent / "cgroup.subtree_control").write_text("+cpu +memory +pids")
    image = RuntimeImage.from_files([("/qa/input/busybox","/bin/busybox",True),("/qa/input/probe","/bin/probe",True)])
    backend = LinuxBackend(image,"/tmp/native-state",cgroup_parent=str(parent),bundle_directory="/qa/input/runtime")
    reports = []
    def run(kind, resources):
        effective = PolicyResolver().resolve([PolicyLayer("platform",PolicyPatch({"resources":resources}))],
            platform="linux",provider=backend.provider,runtime_root_identity=image.identity)
        report = backend.preflight(effective)
        assert report.ready, report.to_document()
        handle = backend.prepare(ExecutionRequest(effective,("/bin/probe",kind),"/",timeout_seconds=10))
        job = backend._jobs[handle.instance_id]
        cgroup = job.group.path
        applied = {name:(cgroup / name).read_text().strip() for name in ("cpu.max","memory.max","pids.max")}
        try:
            handle = backend.start(handle)
            result = backend.wait(handle)
        finally:
            if backend.inspect(handle)["state"] not in ("prepared","exited","failed","cleaned"):
                backend.stop(handle,time.monotonic())
            backend.cleanup(handle)
        assert not cgroup.exists(), "Resource cgroup survived cleanup"
        usage = result["resource_history"][-1]["usage"]
        if kind == "cpu":
            assert result["exit_code"] == 0 and usage["cpu"]["nr_throttled"] > 0
            assert 0 < usage["cpu"]["usage_usec"] < 1000000, usage
            assert applied["cpu.max"] == "10000 100000"
        elif kind == "memory":
            assert result["reason"] == "memory_limit" and usage["memory_events"]["oom_kill"] > 0
            assert applied["memory.max"] == "33554432"
        else:
            assert result["exit_code"] == 0 and "errno=11" in result["stdout_tail"]
            assert usage["pids_events"]["max"] > 0 and applied["pids.max"] == "16"
        reports.append({"workload":kind,"resources":resources,"applied":applied,"usage":usage,
            "reason":result["reason"],"cgroup_removed":True})
    try:
        run("cpu",{"cpu_cores":0.1,"tasks":16})
        run("memory",{"memory":{"bytes":33554432,"accounting":"charged"},"tasks":16})
        run("processes",{"tasks":16})
        run("threads",{"tasks":16})
        run("cpu",{"cpu_cores":0.1,"memory":{"bytes":33554432,"accounting":"charged"},"tasks":16})
    finally:
        backend.shutdown(time.monotonic() + 5)
        Path("/qa/out/instance-diagnostics.json").write_text(json.dumps(
            [backend.inspect(job.token) for job in backend._jobs.values()],indent=2) + "\n")
    assert not list(parent.glob("qavector-*"))
    result = {"status":"passed","kernel":"real host kernel in isolated owned container scopes", "kernel_release":os.uname().release,
        "cases":reports,"provider_requests":"not_sent","credential_access":"not_read"}
    Path("/qa/out/resource-result.json").write_text(json.dumps(result,indent=2) + "\n")
    print(json.dumps({"status":"passed","cases":len(reports)}),flush=True)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        Path("/qa/out/resource-error.txt").write_text(traceback.format_exc())
        raise
