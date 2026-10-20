"""Clean release gates prevent unreviewed inputs and inconsistent builds."""

import hashlib
import importlib
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux release harness; Windows remains a stub")


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    return {name: importlib.import_module(name) for name in (
        "build_runtime_preview", "package_runtime_preview", "prepare_repository_dependencies",
        "accept_runtime_install", "acceptance.ci_release")}


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    for name in ("pyproject.toml", "MANIFEST.in", "session_launcher.py", "runtime_paths.py", "users.example.json",
            "execution_runtime/backends/linux/bubblewrap.lock.json", "cycle/worker.py"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('version = "1.2.3"\n' if name == "pyproject.toml" else "public fixture\n")
    return root


def test_build_stamp_is_created_in_clean_checkout(modules, source):
    modules["build_runtime_preview"].stamp_version(source)
    assert (source / "build/VERSION").read_text() == "1.2.3\n"


def test_source_manifest_pins_code_and_excludes_local_secret_stores(modules, source):
    for name in ("users.json", "services.json", "cyclesecrets.json", "cyclesecrets.key", "cycle/local-store.json"):
        (source / name).write_text("PRIVATE-SYNTHETIC-SENTINEL")
    pins = modules["build_runtime_preview"].source_manifest(source)
    assert "cycle/worker.py" in pins and "users.example.json" in pins
    assert not {"users.json", "services.json", "cyclesecrets.json", "cyclesecrets.key", "cycle/local-store.json"} & set(pins)


def test_source_change_during_freeze_blocks_preview_publication(modules, source, tmp_path):
    builder = modules["build_runtime_preview"]
    before = builder.source_manifest(source)
    (source / "cycle/worker.py").write_text("changed during build")
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(ValueError, match="changed during freeze"):
        builder.write_build_info(source, stage, before)
    assert not (stage / "BUILD-INFO.json").exists()


def test_payload_library_mutation_blocks_packaging(modules, source, tmp_path, monkeypatch):
    builder, packager = modules["build_runtime_preview"], modules["package_runtime_preview"]
    stage = tmp_path / "preview"
    for component in ("core", "gui"):
        executable = stage / component / ("qavector-" + component)
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"fixture only; never execute")
        executable.chmod(0o755)
        shutil.copytree(ROOT / "licenses", executable.parent / "_internal/licenses")
    library = stage / "core/_internal/library.so"
    library.write_bytes(b"reviewed bytes")
    builder.write_build_info(source, stage, builder.source_manifest(source))
    library.write_bytes(b"altered after freeze")
    monkeypatch.setattr(packager, "ROOT", source)
    with pytest.raises(ValueError, match="payload changed"):
        packager.inspect_preview(stage)


def test_repository_preparation_requires_explicit_network_opt_in(modules, monkeypatch, tmp_path):
    module = modules["prepare_repository_dependencies"]
    monkeypatch.setattr(module, "local_docker", lambda: pytest.fail("No Docker access before explicit opt-in"))
    with pytest.raises(ValueError, match="allow-repository-downloads"):
        module.prepare(tmp_path / "candidate", tmp_path / "previous", tmp_path / "new-output", "fixture")
    assert not (tmp_path / "new-output").exists()


def test_changed_repository_archive_blocks_offline_install(modules, tmp_path):
    archive = tmp_path / "public-library.deb"
    archive.write_bytes(b"downloaded and authenticated")
    provenance = {"status": "passed", "sha256": {archive.name: hashlib.sha256(archive.read_bytes()).hexdigest()}}
    modules["accept_runtime_install"].verify_dependencies(tmp_path, provenance)
    archive.write_bytes(b"changed between preparation and offline install")
    with pytest.raises(ValueError, match="differ from verified"):
        modules["accept_runtime_install"].verify_dependencies(tmp_path, provenance)


def test_failed_dependency_preparation_cannot_be_used(modules, tmp_path):
    with pytest.raises(ValueError, match="did not complete"):
        modules["accept_runtime_install"].verify_dependencies(tmp_path, {"status": "failed"})


def test_previous_checksum_is_checked_before_package_processing(modules, monkeypatch, tmp_path):
    module = modules["acceptance.ci_release"]
    (tmp_path / "previous.deb").write_bytes(b"unreviewed archive")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: pytest.fail("Never process an archive with the wrong checksum"))
    with pytest.raises(ValueError, match="checksum does not match"):
        module.verify_previous(tmp_path, "0" * 64)


def test_previous_release_must_be_unambiguous(modules, tmp_path):
    for name in ("one.deb", "two.deb"):
        (tmp_path / name).write_bytes(b"fixture")
    with pytest.raises(ValueError, match="exactly one"):
        modules["acceptance.ci_release"].verify_previous(tmp_path, "0" * 64)


def test_workflow_is_manual_hosted_and_candidate_depends_on_success():
    workflow = yaml.load((ROOT / ".github/workflows/runtime-release-acceptance.yml").read_text(), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert "previous_release_tag" not in inputs
    assert {"previous_run_id", "previous_artifact_name", "previous_deb_sha256"} <= set(inputs)
    job = workflow["jobs"]["clean-linux"]
    assert job["runs-on"] == "ubuntu-22.04"
    steps = job["steps"]
    candidate = next(step for step in steps if step.get("with", {}).get("name") == "qavector-verified-linux-deb")
    assert candidate.get("if", "success()") == "success()"
    for step in steps:
        if "run" in step:
            assert "gh release" not in step["run"]
            assert "${{ inputs." not in step["run"], "Untrusted inputs must travel through quoted environment variables"
            subprocess.run(["bash", "-n"], input=step["run"], text=True, check=True)


@pytest.mark.parametrize("workflow_name,interpreter", [
    ("runtime-release-acceptance.yml", "build/venv/bin/python"),
    ("runtime-license-gui.yml", "bin/python"),
])
def test_isolated_gui_modules_stop_the_gate_on_the_first_failure(tmp_path, workflow_name, interpreter):
    workflow = yaml.load((ROOT / ".github/workflows" / workflow_name).read_text(), Loader=yaml.BaseLoader)
    job = next(iter(workflow["jobs"].values()))
    step = next(step for step in job["steps"] if "for module in" in step.get("run", ""))
    executable = tmp_path / interpreter
    executable.parent.mkdir(parents=True)
    calls = tmp_path / "calls"
    executable.write_text('#!/bin/sh\nprintf "called\\n" >> "$GUI_CALLS"\nexit 7\n')
    executable.chmod(0o755)
    result = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=tmp_path,
        env={"PATH": str(tmp_path / "bin") + ":/usr/bin:/bin", "GUI_CALLS": str(calls)},
        capture_output=True, text=True)
    assert result.returncode == 7
    assert calls.read_text().splitlines() == ["called"]


def artifact_download_step():
    workflow = yaml.load((ROOT / ".github/workflows/runtime-release-acceptance.yml").read_text(), Loader=yaml.BaseLoader)
    return next(step for step in workflow["jobs"]["clean-linux"]["steps"] if "PREVIOUS_RUN_ID" in step.get("env", {}))


@pytest.mark.parametrize("run_id", ["", "--repo=foreign/repository", "123; exit 0", "123\n456"])
def test_invalid_run_id_cannot_invoke_artifact_download(tmp_path, run_id):
    marker = tmp_path / "gh-called"
    fake = tmp_path / "gh"
    fake.write_text('#!/bin/sh\nprintf called > "$GH_CALLS"\n')
    fake.chmod(0o755)
    result = subprocess.run(["bash", "-e", "-c", artifact_download_step()["run"]], capture_output=True, text=True,
        env={"PATH": str(tmp_path) + ":/usr/bin:/bin", "PREVIOUS_RUN_ID": run_id,
            "PREVIOUS_ARTIFACT_NAME": "previous-deb", "GITHUB_REPOSITORY": "fixture/repository", "GH_CALLS": str(marker)})
    assert result.returncode != 0
    assert not marker.exists()


def test_artifact_name_is_literal_and_repository_is_explicit(tmp_path):
    marker = tmp_path / "gh-args"
    injected = tmp_path / "must-not-exist"
    name = 'previous $(touch ' + str(injected) + ') with spaces'
    fake = tmp_path / "gh"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$GH_CALLS"\n')
    fake.chmod(0o755)
    subprocess.run(["bash", "-e", "-c", artifact_download_step()["run"]], check=True, capture_output=True, text=True,
        env={"PATH": str(tmp_path) + ":/usr/bin:/bin", "PREVIOUS_RUN_ID": "123456", "PREVIOUS_ARTIFACT_NAME": name,
            "GITHUB_REPOSITORY": "fixture/repository", "GH_CALLS": str(marker)})
    assert not injected.exists()
    assert marker.read_text().splitlines() == ["run", "download", "--repo=fixture/repository", "--name=" + name,
        "--dir=build/previous-artifact", "--", "123456"]
