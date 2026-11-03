"""Prepare fresh, offline test data and a Cycle using an explicit frozen core.

Host-side author tooling using JSON/CLI only; never import this from a plugin.
Nothing is installed or activated by this command. The selected core validates
and saves the deployment in the new demo data root.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess


def write_json(path, document):
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare(core, output):
    core, output = core.resolve(strict=True), output.absolute()
    if output.exists():
        raise ValueError("Choose a new --output directory to preserve existing demo data")
    descriptor_path = core.parent / "_internal/runtime/plugin-python.json"
    descriptor = json.loads(descriptor_path.read_text())
    if descriptor["target"] != "linux-x86_64-cp310":
        raise ValueError("The starter targets Linux x86_64 / CPython 3.10")
    image_files = []
    for item in descriptor["image"]["files"]:
        relative = PurePosixPath(item["source"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Expected local packaged runtime paths")
        source = descriptor_path.parent / item["source"]
        if hashlib.sha256(source.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("Managed runtime payload hash mismatch")
        image_files.append(dict(item, source=str(source)))
    output.mkdir(parents=True, mode=0o700)
    for name in ("data", "project", "cycles"):
        (output / name).mkdir(mode=0o700)
    deployment = {
        "api_version": "qavector.io/v1", "kind": "RuntimeDeployment",
        "metadata": {"name": "qavplug-starter", "description": "Offline authoring demo; no workspace or provider access"},
        "spec": {
            "provider": "linux-isolated",
            "policy": {"resources": {"scratch_bytes": 16 * 1024 ** 2}},
            "bindings": [{"name": "project", "source": str(output / "project"), "target": "/workspace"}],
            "working_directory": "/",
            "backend_options": {"linux": {"state_directory": str(output / "runtime-state"),
                "image": {"identity": descriptor["image"]["identity"], "files": image_files}}},
        },
    }
    write_json(output / "deployment.json", deployment)
    env = dict(os.environ, CMS_HOME=str(output / "data"))
    process = subprocess.run([str(core), "--runtime-deployment-save=qavplug-starter",
        "--from=" + str(output / "deployment.json")], env=env, cwd=output,
        check=True, capture_output=True, text=True, timeout=30)
    saved = json.loads(process.stdout)
    if saved.get("ok") is not True:
        raise ValueError("Frozen core rejected the demo deployment")
    manifest = json.loads((Path(__file__).resolve().parents[1] / "manifest.json").read_text())
    version = "==" + manifest["version"]
    cycle = {
        "id": "qavplug_starter_demo", "name": "QAVplug Starter demo",
        "execution": {"runtime": {"mode": "isolated", "deployment": "qavplug-starter",
            "revision": saved["revision"], "project_directory": str(output / "project")}},
        "agent_profiles": {"agent.demo": {"backend_ref": "example.starter.echo", "backend_version": version,
            "limits": {"wall_time_seconds": 30}, "backend_options": {"prefix": "Demo: "}}},
        "steps": [
            {"id": "count", "plugin": "example.starter.count", "timeout": 30,
             "with": {"plugin_version": version, "config": {"threshold": 2}, "inputs": {"values": [1, 3, 5]}}},
            {"id": "echo", "plugin": "agent", "timeout": 30,
             "with": {"agent_profile": "agent.demo", "task": "Hello QAVector",
                "output_schema": {"type": "object", "properties": {"answer": {"type": "string"}},
                                  "required": ["answer"], "additionalProperties": False}}},
        ],
    }
    # JSON is a YAML subset understood by QAVector's existing Cycle loader.
    write_json(output / "cycles/qavplug_starter_demo.yaml", cycle)
    print(json.dumps({"core": str(core), "data_root": str(output / "data"),
        "cycles_directory": str(output / "cycles"), "deployment_revision": saved["revision"],
        "image_identity": descriptor["image"]["identity"], "installed": False}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", type=Path, required=True, help="Actual frozen qavector-core executable")
    parser.add_argument("--output", type=Path, required=True, help="New demo directory")
    args = parser.parse_args()
    prepare(args.core, args.output)


if __name__ == "__main__":
    main()
