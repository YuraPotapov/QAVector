"""Trusted provisioning of one offline service environment, outside its workspace."""

import argparse
import json
from pathlib import Path
import sys

from execution_runtime import RuntimePolicyError
from execution_runtime.deployments import DeploymentRegistry, RuntimeDeployment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--busybox", required=True, help="Reviewed static BusyBox used as a workload image, not a launcher")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--state-directory", required=True, type=Path)
    parser.add_argument("--deployment-directory", type=Path)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--scratch-bytes", type=int, default=65536)
    parser.add_argument("--expected-revision")
    options = parser.parse_args()
    if sys.platform != "linux":
        parser.error("This example provisions the Linux workload image; the shared deployment API supports other providers.")
    from execution_runtime.backends.linux import RuntimeImage
    image = RuntimeImage.from_files([(options.busybox, "/bin/busybox", True)])
    document = {"api_version": "qavector.io/v1", "kind": "RuntimeDeployment",
        "metadata": {"name": options.name, "description": "Explicitly provisioned offline shell service"},
        "spec": {"provider": "linux-isolated", "working_directory": "/workspace", "shell": ["/bin/busybox", "sh", "-c"],
            "policy": {"filesystem": [{"binding": "project", "access": "read_write" if options.write else "read_only"}],
                       "resources": {"scratch_bytes": options.scratch_bytes}},
            "bindings": [{"name": "project", "source": str(options.workspace.absolute()), "target": "/workspace"}],
            "backend_options": {"linux": {"state_directory": str(options.state_directory.absolute()),
                "image": {"identity": image.identity, "files": [vars(one) for one in image.files]}}}}}
    deployment = DeploymentRegistry(options.deployment_directory).save(RuntimeDeployment(document), options.expected_revision)
    print(json.dumps({"name": deployment.name, "revision": deployment.digest,
                      "image_identity": image.identity, "network": "none"}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimePolicyError as exc:
        print(json.dumps({"ok": False, "problems": [exc.to_document()]}))
        raise SystemExit(2)
