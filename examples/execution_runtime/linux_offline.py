"""Explicit trusted provisioning example; not an input format for untrusted nodes."""

import argparse
import json
from pathlib import Path
import sys

from execution_runtime import ExecutionRuntime, PolicyLayer, PolicyPatch, PolicyResolver, RuntimePolicyError
from execution_runtime.backends.base import ExecutionRequest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--busybox", required=True, help="Approved statically linked BusyBox binary")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--state-directory", required=True, type=Path, help="Private directory outside the workspace")
    parser.add_argument("--write", action="store_true", help="Grant RW instead of RO workspace access")
    parser.add_argument("--cgroup-parent", help="Explicit preconfigured delegated cgroup v2 parent")
    parser.add_argument("--scratch-bytes", type=int, help="Page-aligned ephemeral /tmp data capacity")
    parser.add_argument("--launcher", help="Explicit operator-approved Bubblewrap binary; privately copied and content-pinned")
    parser.add_argument("--bundle-directory", help="Development/staging runtime resources; installed app resolves its own bundle")
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("This provisioning example requires the Linux provider.")
    from execution_runtime.backends.linux import ApprovedLauncher, LinuxBackend, RuntimeImage

    image = RuntimeImage.from_files([(args.busybox, "/bin/busybox", True)])
    backend = LinuxBackend(image, args.state_directory, cgroup_parent=args.cgroup_parent,
        launcher=ApprovedLauncher.from_file(args.launcher) if args.launcher else None, bundle_directory=args.bundle_directory)
    restrictions = {"filesystem": [{"binding": "project", "access": "read_write" if args.write else "read_only"}]}
    if args.cgroup_parent:
        restrictions["resources"] = {"cpu_cores": 1, "tasks": 16,
            "memory": {"bytes": 67108864, "accounting": "charged"}}
    if args.scratch_bytes is not None:
        restrictions.setdefault("resources", {})["scratch_bytes"] = args.scratch_bytes
    effective = PolicyResolver().resolve([PolicyLayer("platform", PolicyPatch(restrictions))],
        [{"name": "project", "source": str(args.workspace.absolute()), "target": "/workspace"}],
        platform="linux", provider=backend.provider, runtime_root_identity=image.identity)
    runtime = ExecutionRuntime(backend)
    report = runtime.preflight(effective)
    if not report.ready:
        print(json.dumps(report.to_document(), indent=2))
        return 1
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    request = ExecutionRequest(effective, command or ["/bin/busybox", "ls", "-la", "/workspace"],
        "/workspace", timeout_seconds=args.timeout)
    handle = runtime.prepare(request)
    try:
        handle = runtime.start(handle)
        result = runtime.wait(handle)
        print(json.dumps(result, indent=2))
        return 0 if result["exit_code"] == 0 else 1
    finally:
        if runtime.inspect(handle)["state"] in ("starting", "running"):
            runtime.stop(handle, grace_seconds=0.5)
        runtime.cleanup(handle)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimePolicyError as error:
        print(json.dumps({"error": error.to_document()}), file=sys.stderr)
        sys.exit(2)
