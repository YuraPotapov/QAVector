"""Offline candidate packaging rejects altered binaries and missing attribution."""

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "linux",reason="Linux DEB packaging; Windows runtime remains a stub")


@pytest.fixture
def packager(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    spec = importlib.util.spec_from_file_location("package_runtime_preview",ROOT / "packaging/package_runtime_preview.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def preview(tmp_path):
    directory = tmp_path / "preview"
    hashes = {}
    for component in ("core","gui"):
        path = directory / component / ("qavector-" + component)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"fixture only; never executed")
        path.chmod(0o755)
        hashes[component] = hashlib.sha256(path.read_bytes()).hexdigest()
        shutil.copytree(ROOT / "licenses",path.parent / "_internal/licenses")
    (directory / "BUILD-INFO.json").write_text(json.dumps({"binaries_sha256":hashes,
        "source_sha256":{"pyproject.toml":hashlib.sha256((ROOT / "pyproject.toml").read_bytes()).hexdigest()}}))
    return directory


@pytest.mark.parametrize("name", ["NOTICE.md","AUTHORS","COPYING"])
def test_missing_bubblewrap_material_blocks_candidate_before_archiving(preview, packager, monkeypatch, name):
    (preview / "core/_internal/licenses/bubblewrap" / name).unlink()
    monkeypatch.setattr(packager.subprocess,"run",lambda *a,**kw:pytest.fail("No archive or execution before license gate"))
    with pytest.raises((ValueError,OSError)):
        packager.package(preview,preview.parent / "candidate",preview.parent / "icons")
    assert not (preview.parent / "candidate").exists()


def test_changed_reviewed_binary_is_rejected(preview, packager):
    (preview / "core/qavector-core").write_bytes(b"changed since approval")
    with pytest.raises(ValueError,match="binary changed"):
        packager.inspect_preview(preview)


def test_existing_candidate_is_preserved(preview, packager):
    output = preview.parent / "existing"
    output.mkdir()
    marker = output / "keep"
    marker.write_bytes(b"reviewed previous candidate")
    with pytest.raises(ValueError,match="new output directory"):
        packager.package(preview,output,preview.parent / "icons")
    assert marker.read_bytes() == b"reviewed previous candidate"


def test_inventory_rejects_external_symlink(tmp_path, packager):
    root = tmp_path / "payload"
    root.mkdir()
    external = tmp_path / "outside"
    external.write_bytes(b"unapproved dependency")
    try:
        (root / "link").symlink_to(external)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    with pytest.raises(ValueError,match="symlinks must stay"):
        packager.inventory(root)


def test_inventory_detects_executable_mode_change(tmp_path, packager):
    source = tmp_path / "binary"
    source.write_bytes(b"pinned bytes")
    source.chmod(0o755)
    before = packager.inventory(tmp_path)
    source.chmod(0o644)
    after = packager.inventory(tmp_path)
    assert before["binary"]["sha256"] == after["binary"]["sha256"]
    assert before != after
