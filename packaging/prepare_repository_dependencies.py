#!/usr/bin/env python3
"""Acquire signed public-repository dependencies in an owned download container."""

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

from acceptance.docker_host import command, local_docker, remove_owned, run_attached
from acceptance.prepare_dependencies import FORMAT, database
from accept_runtime_install import digest, package_version

ROOT = Path(__file__).resolve().parents[1]


def prepare(candidate, previous, output, image, allow_downloads=False):
    if sys.platform != "linux":
        raise ValueError("Repository fixture targets Linux; Windows runtime remains a stub")
    if output.exists():
        raise ValueError("Choose a new dependency fixture directory")
    if not allow_downloads:
        raise ValueError("Explicit --allow-repository-downloads is required for the preparation phase")
    local_docker()
    image_id = command(["docker", "image", "inspect", "--format", "{{.Id}}", image])
    versions = {"candidate": package_version(candidate), "previous": package_version(previous)}
    output.mkdir(parents=True)
    output.chmod(0o770)
    token = uuid.uuid4().hex
    container = None
    report = {"status": "failed", "base_image_id": image_id, "origin": "signed public OS repositories; downloaded in clean base container",
        "network": "bridge for public dependency download only; acceptance remains network=none", "host_repacked_packages": False,
        "host_installation": False, "provider_requests": "not_sent", "credential_access": "not_read", "versions": versions,
        "input_sha256": {"candidate": digest(candidate), "previous": digest(previous),
            "runner": digest(ROOT / "packaging/acceptance/repository_runner.sh")}, "container_removed": False}
    with tempfile.TemporaryDirectory(prefix="qavector-repository-input-") as directory:
        payload = Path(directory)
        payload.chmod(0o755)
        for archive, name in ((candidate, "candidate.deb"), (previous, "previous.deb")):
            shutil.copy2(archive, payload / name)
        shutil.copy2(ROOT / "packaging/acceptance/repository_runner.sh", payload / "runner.sh")
        try:
            container = command(["docker", "create", "--pull=never", "--network=bridge", "--cap-drop=ALL",
                "--cap-add=CHOWN", "--cap-add=DAC_OVERRIDE", "--cap-add=FOWNER", "--cap-add=SETUID", "--cap-add=SETGID",
                "--security-opt=no-new-privileges", "--memory=512m", "--pids-limit=128", "--cpus=1",
                "--label", "io.qavector.acceptance=" + token, "--name", "qavector-repository-" + token,
                "--group-add", str(os.getgid()),
                "--mount", "type=bind,src=" + str(payload) + ",dst=/qa/input,readonly",
                "--mount", "type=bind,src=" + str(output.resolve()) + ",dst=/qa/out",
                "--env", "QAVECTOR_OWNED_ACCEPTANCE=1", "--entrypoint=/bin/bash", image_id, "/qa/input/runner.sh"])
            result = run_attached(container, output / "download.log", 600)
            exit_code = int(command(["docker", "inspect", "--format", "{{.State.ExitCode}}", container]))
            if result.returncode or exit_code or (output / "download-status.txt").read_text().strip() != "passed":
                raise RuntimeError("Public dependency download failed; see " + str(output / "download.log"))
            selected = {}
            hashes = {}
            for archive in sorted(output.glob("*.deb")):
                # dpkg-deb does not have db:Status-Status; normalize the metadata
                # for the same parser used by the offline dependency resolver.
                raw = command(["dpkg-deb", "--show", "--showformat=" + FORMAT.replace("${db:Status-Status}", "installed"), str(archive)], strip=False)
                entries = database(raw)
                if len(entries) != 1 or "qavector" in entries:
                    raise ValueError("Expected one public dependency package per archive")
                name, entry = next(iter(entries.items()))
                if name in selected:
                    raise ValueError("Multiple versions of a dependency require explicit resolution: " + name)
                selected[name] = entry
                hashes[archive.name] = digest(archive)
            if not selected:
                raise ValueError("No public dependencies were downloaded")
            report.update(status="passed", selected=selected, sha256=hashes,
                base_packages={name: entry["version"] for name, entry in database((output / "base-packages.tsv").read_text()).items()})
        finally:
            if container:
                cleanup = remove_owned([container], token)
                report["container_removed"] = bool(cleanup["removed"])
                report["cleanup_errors"] = cleanup["errors"]
                if cleanup["errors"]:
                    report["status"] = "failed"
            (output / "dependencies.json").write_text(json.dumps(report, indent=2) + "\n")
            if container and cleanup["errors"]:
                raise RuntimeError("Owned download container cleanup failed; see dependencies.json")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("candidate", "previous", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--image", required=True, help="Already cached standard OS image; never pulled")
    parser.add_argument("--allow-repository-downloads", action="store_true")
    args = parser.parse_args()
    result = prepare(args.candidate, args.previous, args.output, args.image, args.allow_repository_downloads)
    print(json.dumps({"status": result["status"], "packages": len(result["selected"])}))


if __name__ == "__main__":
    main()
