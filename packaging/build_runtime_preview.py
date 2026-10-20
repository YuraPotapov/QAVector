#!/usr/bin/env python3
"""Build a side-by-side GUI/core preview without replacing installed applications."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from check_licenses import verify_licenses, verify_runtime
from package_runtime_preview import digest, inventory


def source_manifest(root):
    """Pin application code and public resources, never local user data."""
    files = {root / name for name in ("pyproject.toml", "MANIFEST.in", "session_launcher.py", "runtime_paths.py", "users.example.json")}
    for directory in ("execution_runtime", "cycle", "domain", "engine", "adapters", "gui/cms_gui", "extensions", "packaging/pyinstaller"):
        for path in (root / directory).rglob("*"):
            if path.is_file() and path.suffix in (".py", ".js", ".qml", ".spec"):
                files.add(path)
    for directory in ("licenses", "gui/cms_gui/assets"):
        files.update(path for path in (root / directory).rglob("*") if path.is_file() and "__pycache__" not in path.parts)
    files.add(root / "execution_runtime/backends/linux/bubblewrap.lock.json")
    return {path.relative_to(root).as_posix(): digest(path) for path in sorted(files)}


def write_build_info(root, stage, before):
    if source_manifest(root) != before:
        raise ValueError("Application sources changed during freeze; refusing inconsistent preview")
    info = {"preview_format": 1, "source_sha256": before,
        "binaries_sha256": {component: digest(stage / component / ("qavector-" + component)) for component in ("core", "gui")},
        "payload": {component: inventory(stage / component) for component in ("core", "gui")},
        "provider_requests": "not_sent", "credential_access": "not_read", "windows": "interfaces/stubs only"}
    (stage / "BUILD-INFO.json").write_text(json.dumps(info, indent=2) + "\n")
    return info


def stamp_version(root):
    match = re.search(r'^version\s*=\s*"([^"]+)"', (root / "pyproject.toml").read_text(), re.M)
    if not match:
        raise ValueError("Missing project version")
    path = root / "build/VERSION"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(match.group(1) + "\n")


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-distribution", type=Path, required=True,
                        help="Already built, pinned Bubblewrap distribution; no system fallback")
    parser.add_argument("--output", type=Path, required=True, help="New output directory (must not exist)")
    parser.add_argument("--python", type=Path, default=root / "build/venv/bin/python")
    options = parser.parse_args()
    output = options.output.absolute()
    runtime = options.runtime_distribution.absolute()
    if output.exists():
        parser.error("Output already exists; choose a new directory to preserve previous builds.")
    if not sys.platform.startswith("linux"):
        parser.error("This preview builder targets Linux; Windows runtime remains a stub.")
    verify_licenses(root / "licenses")
    verify_runtime(runtime)
    output.parent.mkdir(parents=True, exist_ok=True)
    stamp_version(root)
    before = source_manifest(root)
    # Publish by rename on the output filesystem, including sandbox/CI mounts.
    with tempfile.TemporaryDirectory(prefix="qavector-preview-build-", dir=output.parent) as temporary:
        work = Path(temporary)
        for component in ("core", "gui"):
            env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(work / (component + "-config")))
            subprocess.run([str(options.python.absolute()), "-m", "PyInstaller", "--clean", "--noconfirm",
                "--distpath", str(work / "dist"), "--workpath", str(work / (component + "-work")),
                str(root / "packaging/pyinstaller" / (component + ".spec"))], cwd=root, env=env, check=True)
        stage = work / "preview"
        stage.mkdir()
        for component in ("core", "gui"):
            shutil.move(str(work / "dist" / component), stage / component)
            shutil.copytree(runtime / "licenses", stage / component / "_internal/licenses", dirs_exist_ok=True)
        bundled = stage / "core/_internal/runtime"
        shutil.copytree(runtime, bundled)
        for component in ("core", "gui"):
            subprocess.run([str(options.python.absolute()), str(root / "packaging/check_licenses.py"),
                "--runtime", str(bundled), "--licenses", str(stage / component / "_internal/licenses")], check=True)
        with tempfile.TemporaryDirectory(prefix="qavector-preview-smoke-") as smoke_home:
            env = dict(os.environ, CMS_HOME=smoke_home)
            core = stage / "core/qavector-core"
            result = subprocess.run([str(core), "--runtime-bundle-info"], capture_output=True, text=True, env=env, check=True)
            payload = json.loads(result.stdout)
            if payload.get("ok") is not True:
                raise ValueError("Frozen core did not verify its bundled runtime")
            subprocess.run([str(core), "--runtime-deployment-list"], env=env, check=True)
        launcher = stage / "launch-qavector.sh"
        launcher.write_text('#!/bin/sh\nset -eu\n'
            'qavector_preview_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
            'export CMS_CORE_EXE="$qavector_preview_dir/core/qavector-core"\n'
            'exec "$qavector_preview_dir/gui/qavector-gui" "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
        (stage / "README.txt").write_text("QAVector Execution Runtime GUI preview\n\n"
            "Start: ./launch-qavector.sh\n"
            "Execution environments: Configure sidebar; also available from Edit service and Cycle properties.\n"
            "Third-party licenses: Help > About > Third-Party Licenses (offline).\n"
            "The launcher selects the included core. Existing application data follows the normal QAVector settings.\n"
            "For a separate data directory set CMS_HOME before starting.\n"
            "This portable preview does not replace /opt/qavector or desktop launchers.\n"
            "Windows implementation remains interfaces/stubs.\n", encoding="utf-8")
        write_build_info(root, stage, before)
        if output.exists():
            raise ValueError("Output appeared during build; existing output is preserved")
        stage.rename(output)
    print("Preview ready: " + str(output / "launch-qavector.sh"))


if __name__ == "__main__":
    main()
