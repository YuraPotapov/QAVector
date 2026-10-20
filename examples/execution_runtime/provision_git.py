"""Provision local Git workers from an explicitly curated native image manifest.

No tools are installed or discovered. The image must already pin the interpreter,
stdlib/libraries, Git helpers and the exact QAVector worker source artifact.
"""

import argparse
import json
from pathlib import Path

from execution_runtime._json import read_document
from execution_runtime.deployments import DeploymentRegistry, RuntimeDeployment
from execution_runtime.errors import RuntimePolicyError
from execution_runtime.workers import GIT_CONTRACT, git_artifact


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-source", action="store_true", help="Print the core-owned Linux worker source and SHA-256, then exit")
    parser.add_argument("--name")
    parser.add_argument("--image", type=Path, help="Reviewed JSON object containing identity and pinned files")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--state-directory", type=Path)
    parser.add_argument("--deployment-directory", type=Path)
    parser.add_argument("--worker-entrypoint", default="/qavector/workers/git_checkout_linux.py")
    parser.add_argument("--python-in-image", help="Absolute approved interpreter path inside the image")
    parser.add_argument("--git-in-image", help="Absolute approved Git executable path inside the image")
    parser.add_argument("--expected-revision")
    options = parser.parse_args(argv)
    if options.worker_source:
        source, digest = git_artifact("linux")
        print(json.dumps(dict(contract=GIT_CONTRACT, source=str(source), sha256=digest)))
        return 0
    if any(getattr(options, field) is None for field in ("name", "image", "workspace", "repository", "state_directory", "python_in_image", "git_in_image")):
        parser.error("Provide name, image, workspace, repository, state-directory, python-in-image and git-in-image.")
    image = read_document(options.image)
    worker = dict(contract=GIT_CONTRACT, entrypoint=options.worker_entrypoint,
                  argv=[options.python_in_image, "-I", "-B", options.worker_entrypoint], git=options.git_in_image)
    deployment = RuntimeDeployment({"api_version": "qavector.io/v1", "kind": "RuntimeDeployment", "metadata": {"name": options.name}, "spec": {
        "provider": "linux-isolated", "workers": {"git.checkout": worker}, "working_directory": "/workspace",
        "policy": {"filesystem": [{"binding": "project", "access": "read_write"}, {"binding": "repository", "access": "read_only"}]},
        "bindings": [{"name": "project", "source": str(options.workspace.absolute()), "target": "/workspace"},
                     {"name": "repository", "source": str(options.repository.absolute()), "target": "/repository"}],
        "backend_options": {"linux": {"state_directory": str(options.state_directory.absolute()), "image": image}}}})
    # Validate the approved image contract without launching a workload.
    from cycle.runtime_git import GitCheckoutWorker
    backend = deployment.backend()
    policy = deployment.resolve(backend, str(options.workspace.absolute()))
    GitCheckoutWorker.validate(deployment, backend, policy)
    saved = DeploymentRegistry(options.deployment_directory).save(deployment, options.expected_revision)
    print(json.dumps(dict(name=saved.name, revision=saved.digest, worker=GIT_CONTRACT, network="none")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimePolicyError as exc:
        print(json.dumps({"ok": False, "problems": [exc.to_document()]}))
        raise SystemExit(2)
