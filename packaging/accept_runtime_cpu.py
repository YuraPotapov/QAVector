#!/usr/bin/env python3
"""Offline native acceptance in owned Docker scopes, without host delegation edits."""

import argparse
import ctypes.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

from acceptance.docker_host import command, local_docker, remove_owned, run_attached

ROOT = Path(__file__).resolve().parents[1]


def accept(bundle, output, image):
    if sys.platform != "linux":
        raise ValueError("Native acceptance targets Linux; Windows runtime remains a stub")
    if output.exists():
        raise ValueError("Choose a new acceptance output directory")
    local_docker()
    image_id = command(["docker","image","inspect","--format","{{.Id}}",image])
    if "cpu" not in Path("/sys/fs/cgroup/system.slice/cgroup.subtree_control").read_text().split():
        raise ValueError("Docker's existing parent has no CPU controller; host configuration is not changed")
    output.mkdir(parents=True)
    output.chmod(0o770)
    token = uuid.uuid4().hex
    created = []
    scope = None
    with tempfile.TemporaryDirectory(prefix="qavector-resource-input-") as directory:
        payload = Path(directory)
        payload.chmod(0o755)
        shutil.copytree(ROOT / "execution_runtime",payload / "source/execution_runtime",ignore=shutil.ignore_patterns("__pycache__","*.pyc"))
        shutil.copy2(ROOT / "runtime_paths.py",payload / "source/runtime_paths.py")
        shutil.copy2(ROOT / "packaging/acceptance/resource_runner.py",payload / "runner.py")
        shutil.copytree(bundle,payload / "runtime")
        shutil.copy2("/usr/bin/busybox",payload / "busybox")
        command(["cc","-static","-pthread","-O2","-o",str(payload / "probe"),str(ROOT / "packaging/acceptance/resource_probe.c")])
        library = ctypes.util.find_library("seccomp")
        listing = command(["/sbin/ldconfig","-p"])
        match = next((line.split("=>",1)[1].strip() for line in listing.splitlines() if library and library in line and "x86-64" in line),None)
        if not match:
            raise ValueError("Reviewed local libseccomp dependency unavailable")
        shutil.copy2(Path(match).resolve(),payload / "libseccomp.so.2")
        (output / "input-sha256.json").write_text(json.dumps({str(path.relative_to(payload)):
            hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(payload.rglob("*")) if path.is_file()},indent=2) + "\n")
        try:
            common = ["--pull=never","--network=none","--cap-drop=ALL","--security-opt=no-new-privileges",
                "--memory=512m","--pids-limit=128","--cpus=1","--label","io.qavector.acceptance=" + token]
            anchor = command(["docker","create",*common,"--read-only","--name","qavector-resource-anchor-" + token,
                "--entrypoint=python",image_id,"-c","import time; time.sleep(600)"])
            created.append(anchor)
            command(["docker","start",anchor])
            pid = int(command(["docker","inspect","--format","{{.State.Pid}}",anchor]))
            relative = Path("/proc/%d/cgroup" % pid).read_text().strip().split("0::",1)[1]
            scope = Path("/sys/fs/cgroup") / relative.lstrip("/")
            if scope.name != "docker-" + anchor + ".scope" or scope.parent != Path("/sys/fs/cgroup/system.slice"):
                raise ValueError("Owned Docker cgroup identity is not the expected systemd scope")
            runner = command(["docker","create",*common,"--name","qavector-resource-runner-" + token,
                "--pid=container:" + anchor,"--cgroupns=host","--group-add",str(os.getgid()),
                # Linux >=5.12 requires this for mapping parent UID 0. The
                # native workload still drops every capability in its probe.
                "--cap-add=SETFCAP",
                # Docker's masked /proc mounts prevent a nested user namespace
                # from mounting its own procfs. This affects only the owned PID
                # namespace; no host PID namespace or host /proc bind is used.
                "--security-opt=seccomp=unconfined","--security-opt=apparmor=unconfined",
                "--security-opt=systempaths=unconfined",
                "--mount","type=bind,src=" + str(scope) + ",dst=/sys/fs/cgroup",
                "--mount","type=bind,src=" + str(payload) + ",dst=/qa/input,readonly",
                "--mount","type=bind,src=" + str(output.resolve()) + ",dst=/qa/out",
                "--env","PYTHONPATH=/qa/input/source","--env","QAVECTOR_OWNED_ACCEPTANCE=1",
                "--entrypoint=/bin/sh",image_id,"-c",
                "cp /qa/input/libseccomp.so.2 /usr/local/lib/libseccomp.so.2 && ldconfig && exec python /qa/input/runner.py"])
            created.append(runner)
            result = run_attached(runner,output / "runner.log",180)
            exit_code = int(command(["docker","inspect","--format","{{.State.ExitCode}}",runner]))
            if result.returncode or exit_code:
                raise RuntimeError("Native runner failed; see " + str(output / "runner.log"))
        finally:
            cleanup = remove_owned(created,token)
            (output / "isolation.json").write_text(json.dumps({"image_id":image_id,"network":"none","host_pid_namespace":False,
                "privileged":False,"runner_capabilities":["SETFCAP (parent UID 0 mapping)"],"workload_capabilities":"all dropped",
                "runner_security_options":["no-new-privileges","seccomp=unconfined","apparmor=unconfined","systempaths=unconfined"],
                "cgroup_namespace":"host; only owned anchor scope mounted", "cgroup_bind":"owned anchor scope only",
                "containers_removed":len(cleanup["removed"]),"cleanup_errors":cleanup["errors"],
                "owned_scope_removed":not scope.exists() if scope else None,"host_controllers_reconfigured":False},indent=2) + "\n")
            if cleanup["errors"]:
                raise RuntimeError("Owned container cleanup failed; see isolation.json")
    if scope and scope.exists():
        raise RuntimeError("Owned scope survived container cleanup")
    return json.loads((output / "resource-result.json").read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--image",default="python:3.10-slim")
    options = parser.parse_args()
    result = accept(options.bundle,options.output,options.image)
    print(json.dumps({"status":result["status"],"cases":len(result["cases"])}))


if __name__ == "__main__":
    main()
