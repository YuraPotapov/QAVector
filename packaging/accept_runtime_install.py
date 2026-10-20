#!/usr/bin/env python3
"""Real offline DEB lifecycle in an owned, disposable local Docker container."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

from acceptance.docker_host import command, local_docker, remove_owned, run_attached

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open("rb") as stream:
        result = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def package_version(path):
    if command(["dpkg-deb", "--field", str(path), "Package"]) != "qavector":
        raise ValueError("Only QAVector packages are accepted")
    return command(["dpkg-deb", "--field", str(path), "Version"])


def verify_dependencies(directory, provenance):
    if provenance.get("status", "passed") != "passed" or provenance.get("cleanup_errors"):
        raise ValueError("Dependency preparation did not complete successfully")
    if "sha256" in provenance:
        actual = {path.name: digest(path) for path in sorted(directory.glob("*.deb"))}
        if actual != provenance["sha256"]:
            raise ValueError("Public dependency archives differ from verified download provenance")


def accept(candidate, previous, dependencies, output, image):
    if sys.platform != "linux":
        raise ValueError("DEB acceptance targets Linux; Windows runtime remains a stub")
    if output.exists():
        raise ValueError("Choose a new acceptance output directory")
    local_docker()
    image_id = command(["docker", "image", "inspect", "--format", "{{.Id}}", image])
    provenance = json.loads((dependencies / "dependencies.json").read_text())
    if provenance["base_image_id"] != image_id:
        raise ValueError("Dependencies were prepared for another base image")
    verify_dependencies(dependencies, provenance)
    versions = {"previous": package_version(previous), "candidate": package_version(candidate)}
    if subprocess.run(["dpkg", "--compare-versions", versions["previous"], "lt", versions["candidate"]]).returncode:
        raise ValueError("Upgrade requires an actual earlier package version")
    output.mkdir(parents=True)
    output.chmod(0o770)
    token = uuid.uuid4().hex
    container = None
    report = {"status": "failed", "image_id": image_id, "network": "none", "privileged": False,
        "host_installation": False, "host_user_data_mounted": False, "provider_requests": "not_sent",
        "credential_access": "not_read", "dependencies_origin": provenance["origin"],
        "runner_capabilities": ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID"],
        "runner_script_sha256": digest(ROOT / "packaging/acceptance/install_runner.sh"),
        "versions": versions, "package_sha256": {"candidate": digest(candidate), "previous": digest(previous)},
        "dependency_sha256": {path.name: digest(path) for path in sorted(dependencies.glob("*.deb"))},
        "dependency_provenance_sha256": digest(dependencies / "dependencies.json"),
        "dependency_packages": len(list(dependencies.glob("*.deb"))), "container_removed": False}
    with tempfile.TemporaryDirectory(prefix="qavector-install-input-") as directory:
        payload = Path(directory)
        payload.chmod(0o755)
        shutil.copy2(candidate, payload / "candidate.deb")
        shutil.copy2(previous, payload / "previous.deb")
        shutil.copytree(dependencies, payload / "dependencies")
        shutil.copy2(ROOT / "packaging/acceptance/install_runner.sh", payload / "runner.sh")
        try:
            container = command(["docker", "create", "--pull=never", "--network=none", "--cap-drop=ALL",
                "--cap-add=CHOWN", "--cap-add=DAC_OVERRIDE", "--cap-add=FOWNER", "--cap-add=SETUID", "--cap-add=SETGID",
                "--security-opt=no-new-privileges", "--memory=1g", "--pids-limit=256", "--cpus=1",
                "--label", "io.qavector.acceptance=" + token, "--name", "qavector-install-" + token,
                "--group-add", str(os.getgid()),
                "--mount", "type=bind,src=" + str(payload) + ",dst=/qa/input,readonly",
                "--mount", "type=bind,src=" + str(output.resolve()) + ",dst=/qa/out",
                "--env", "QAVECTOR_OWNED_ACCEPTANCE=1", "--env", "QAVECTOR_PREVIOUS_VERSION=" + versions["previous"],
                "--env", "QAVECTOR_CANDIDATE_VERSION=" + versions["candidate"], "--entrypoint=/bin/bash", image_id, "/qa/input/runner.sh"])
            result = run_attached(container, output / "install.log", 420)
            report["exit_code"] = int(command(["docker", "inspect", "--format", "{{.State.ExitCode}}", container]))
            events_file = output / "events.tsv"
            report["events"] = [line.split("\t", 1) for line in events_file.read_text().splitlines()] if events_file.exists() else []
            if result.returncode or report["exit_code"] or not report["events"] or report["events"][-1] != ["acceptance", "passed"]:
                raise RuntimeError("Package lifecycle failed; see " + str(output / "install.log"))
            for stage in ("upgrade", "fresh"):
                if json.loads((output / ("runtime-" + stage + ".json")).read_text()).get("ok") is not True:
                    raise RuntimeError("Installed bundled runtime verification failed")
            report["status"] = "passed"
        finally:
            if container:
                cleanup = remove_owned([container], token)
                report["container_removed"] = bool(cleanup["removed"])
                report["cleanup_errors"] = cleanup["errors"]
                if cleanup["errors"]:
                    report["status"] = "failed"
            (output / "install-result.json").write_text(json.dumps(report, indent=2) + "\n")
            if container and cleanup["errors"]:
                raise RuntimeError("Owned container cleanup failed; see install-result.json")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("candidate", "previous", "dependencies", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--image", required=True, help="Already cached local base image; never pulled")
    args = parser.parse_args()
    result = accept(args.candidate, args.previous, args.dependencies, args.output, args.image)
    print(json.dumps({"status": result["status"], "versions": result["versions"]}))


if __name__ == "__main__":
    main()
