"""Bundled resources are authoritative; invalid/missing artifacts never select host tools."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

if sys.platform != "linux":
    pytest.skip("Native Linux bundle integrity", allow_module_level=True)

from execution_runtime import PolicyLayer, PolicyPatch, PolicyResolver, RuntimePolicyError
from execution_runtime.backends.linux import BundledBubblewrapBackend, RuntimeImage, SystemBubblewrapBackend
from execution_runtime.backends.linux.bundled import BundledLauncher, source_lock
from execution_runtime.backends.registry import BackendRegistry


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    pin = source_lock()
    pin["source_sha256"] = hashlib.sha256(b"synthetic source fixture").hexdigest()
    pin["license_file_sha256"] = hashlib.sha256(b"license").hexdigest()
    monkeypatch.setattr("execution_runtime.backends.linux.bundled.source_lock", lambda: pin)
    contents = {"bin/bwrap": b"synthetic executable", "lib/libcap.so.2": b"synthetic library",
        "licenses/bubblewrap-COPYING": b"license", "licenses/bubblewrap-LICENSE": b"license",
        "licenses/libcap-copyright": b"license", "manifests/sbom.cdx.json": b"{}",
        "licenses/components.json": b"{}", "licenses/bubblewrap/NOTICE.md": b"notice",
        "licenses/bubblewrap/COPYING": b"license", "licenses/bubblewrap/AUTHORS": b"authors",
        "licenses/libcap/NOTICE.md": b"notice", "licenses/libcap/COPYRIGHT": b"license",
        "sources/check_licenses.py": b"synthetic license check",
        "sources/runtime-build-requirements.txt": b"synthetic tools",
        "sources/build_bubblewrap.py": b"synthetic build script", "sources/bubblewrap.lock.json": json.dumps(pin).encode(),
        "sources/bubblewrap-%s.tar.xz" % pin["version"]: b"synthetic source fixture"}
    versions = {"schema_version": 1, "target": pin["target"], "linkage": pin["linkage"],
        "bubblewrap": {key: pin[key] for key in ("version", "upstream_commit", "source_url", "source_sha256")}}
    contents["manifests/versions.json"] = json.dumps(versions).encode()
    for name, content in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o755 if name == "bin/bwrap" else 0o644)
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in contents.items()}
    path = root / "manifests/checksums.json"
    path.write_text(json.dumps({"schema_version": 1, "files": hashes}))
    path.chmod(0o644)
    for directory in [root] + [path for path in root.rglob("*") if path.is_dir()]:
        directory.chmod(0o755)
    return root


def test_application_resolver_never_uses_the_user_data_root(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime_paths.app_root", lambda: str(tmp_path / "application"))
    monkeypatch.setenv("CMS_HOME", str(tmp_path / "untrusted-user-data"))
    assert BundledLauncher().directory == tmp_path / "application/runtime"


def test_default_and_explicit_system_provider_are_distinct():
    assert isinstance(BackendRegistry().select(host="linux"), BundledBubblewrapBackend)
    system = BackendRegistry().select("linux-system-isolated", host="linux")
    assert isinstance(system, SystemBubblewrapBackend)
    assert system.provider == "linux-system-isolated"


def test_missing_bundle_does_not_search_path_or_execute_system_bwrap(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Bundled backend must not consult or execute system bwrap")
    monkeypatch.setattr(shutil, "which", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    backend = BundledBubblewrapBackend(bundle_directory=tmp_path / "missing")
    with pytest.raises(RuntimePolicyError) as exc:
        backend._binary()
    assert exc.value.code == "bundled_runtime_unavailable"
    info = backend.capabilities().observations.to_document()["bwrap"]
    assert info["source"] == "bundled" and info["integrity"] == "unavailable"


def test_valid_metadata_is_not_a_claim_of_kernel_isolation(bundle):
    assert BundledLauncher(bundle).inspect()["integrity"] == "verified"
    backend = BundledBubblewrapBackend(bundle_directory=bundle)
    policy = PolicyResolver().resolve([PolicyLayer("platform", PolicyPatch({}))],
        platform="linux", provider=backend.provider)
    assert not backend.preflight(policy).ready


@pytest.mark.parametrize("name", ["bin/bwrap", "lib/libcap.so.2", "licenses/bubblewrap-COPYING",
                                 "licenses/bubblewrap/NOTICE.md", "licenses/bubblewrap/AUTHORS",
                                 "manifests/sbom.cdx.json", "sources/bubblewrap-0.13.1.tar.xz"])
def test_tampered_resource_is_refused_before_execution(bundle, name):
    (bundle / name).write_bytes(b"changed")
    with pytest.raises(RuntimePolicyError) as exc:
        BundledLauncher(bundle).inspect()
    assert exc.value.code == "bundle_integrity_failed"


@pytest.mark.parametrize("mode", [0o644, 0o4755, 0o2755])
def test_launcher_must_be_executable_and_unprivileged(bundle, mode):
    (bundle / "bin/bwrap").chmod(mode)
    with pytest.raises(RuntimePolicyError) as exc:
        BundledLauncher(bundle).inspect()
    assert exc.value.code == "bundle_not_executable"


def test_missing_library_does_not_fall_back_to_the_host_library(bundle):
    (bundle / "lib/libcap.so.2").unlink()
    with pytest.raises(RuntimePolicyError) as exc:
        BundledLauncher(bundle).inspect()
    assert exc.value.code == "bundled_runtime_unavailable"


def test_missing_notice_cannot_be_removed_from_the_integrity_manifest(bundle):
    path = bundle / "manifests/checksums.json"
    record = json.loads(path.read_text())
    del record["files"]["licenses/bubblewrap/NOTICE.md"]
    path.write_text(json.dumps(record))
    with pytest.raises(RuntimePolicyError) as exc:
        BundledLauncher(bundle).inspect()
    assert exc.value.code == "bundle_manifest_incomplete"


def test_symlink_to_a_host_resource_is_not_an_integrity_checked_bundle_entry(bundle, tmp_path):
    destination = bundle / "lib/libcap.so.2"
    content = destination.read_bytes()
    destination.unlink()
    host = tmp_path / "host-library"
    host.write_bytes(content)
    destination.symlink_to(host)
    with pytest.raises(RuntimePolicyError):
        BundledLauncher(bundle).inspect()


def test_manifest_cannot_redirect_integrity_checks_outside_the_bundle(bundle):
    path = bundle / "manifests/checksums.json"
    checksums = json.loads(path.read_text())
    checksums["files"]["../outside"] = "a" * 64
    path.write_text(json.dumps(checksums))
    with pytest.raises(RuntimePolicyError) as exc:
        BundledLauncher(bundle).inspect()
    assert exc.value.code == "bundle_manifest_invalid"


def test_metadata_version_cannot_select_an_unreviewed_source_pin(bundle):
    path = bundle / "manifests/versions.json"
    record = json.loads(path.read_text())
    record["bubblewrap"]["version"] = "0.0"
    path.write_text(json.dumps(record))
    with pytest.raises(RuntimePolicyError) as exc:
        BundledLauncher(bundle).inspect()
    assert exc.value.code == "bundle_manifest_mismatch"


def test_actual_executable_version_is_checked_separately_from_file_presence(bundle, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=b"bubblewrap 0.0\n"))
    with pytest.raises(RuntimePolicyError) as exc:
        BundledLauncher(bundle).executable()
    assert exc.value.code == "bundle_version_mismatch"
