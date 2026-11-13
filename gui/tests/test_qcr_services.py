"""GUI owner DTO and explicit identity migration, using the real supervisor."""

import json

import pytest

from cms_gui import servicesfile as sf
from cms_gui.services import ServiceSupervisor


def write_legacy(path):
    document = {"version": 1, "future": {"keep": 1}, "projects": [{"name": "Group", "dir": "",
        "future_group": True, "runners": [{"name": "Web", "type": "shell",
        "settings": {"command": "never-launch SECRET", "env": {"TOKEN": "SECRET"}},
        "future_runner": 4}]}]}
    path.write_text(json.dumps(document))
    return document


def test_migration_is_explicit_idempotent_and_keeps_original_backup(tmp_path):
    path = tmp_path / "services.json"
    original = write_legacy(path)
    before = path.read_bytes()
    rows = sf.load(str(path))
    assert "qcr_id" not in rows[0].extra
    assert path.read_bytes() == before
    assert sf.migrate_qcr_ids(path) == 2
    assert (tmp_path / "services.json.bak").read_bytes() == before
    after = json.loads(path.read_text())
    assert after["future"] == original["future"]
    assert after["projects"][0]["runners"][0]["settings"] == original["projects"][0]["runners"][0]["settings"]
    assert sf.migrate_qcr_ids(path) == 0
    assert (tmp_path / "services.json.bak").read_bytes() == before
    rows = sf.load(str(path))
    group_id, service_id = rows[0].extra["qcr_id"], rows[0].runners[0].extra["qcr_id"]
    rows[0].name, rows[0].runners[0].name = "Renamed", "Renamed Web"
    sf.save(str(path), rows)
    loaded = sf.load(str(path))
    assert loaded[0].extra["qcr_id"] == group_id
    assert loaded[0].runners[0].extra["qcr_id"] == service_id
    copied = loaded[0].runners[0].duplicate()
    assert copied.extra["qcr_id"] != service_id
    assert copied.copy().extra["qcr_id"] == copied.extra["qcr_id"]
    group_copy = loaded[0].duplicate()
    assert group_copy.extra["qcr_id"] != group_id
    assert group_copy.runners[0].extra["qcr_id"] != service_id


def test_duplicate_or_invalid_import_identity_cannot_be_saved(tmp_path):
    path = tmp_path / "services.json"
    write_legacy(path)
    sf.migrate_qcr_ids(path)
    before = path.read_bytes()
    rows = sf.load(str(path))
    clone = rows[0].runners[0].copy()
    clone.name = "Imported copy"
    rows[0].runners.append(clone)
    with pytest.raises(sf.ServicesFileError, match="Duplicate QCR"):
        sf.save(str(path), rows)
    assert path.read_bytes() == before
    clone.extra["qcr_id"] = "invalid"
    with pytest.raises(sf.ServicesFileError, match="Invalid QCR"):
        sf.save(str(path), rows)


@pytest.mark.parametrize("target", ["backup", "source"])
def test_failed_backup_or_replace_retains_original(tmp_path, monkeypatch, target):
    import os
    path = tmp_path / "services.json"
    write_legacy(path)
    before = path.read_bytes()
    replace_file = os.replace
    def fail(source, destination):
        if (target == "backup" and str(destination).endswith(".bak")) or (
                target == "source" and str(destination) == str(path)):
            raise OSError("denied")
        return replace_file(source, destination)
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(sf.ServicesFileError):
        sf.migrate_qcr_ids(path)
    assert path.read_bytes() == before
    assert list(tmp_path.glob(".qcr-services-*")) == []
    assert list(tmp_path.glob(".qcr-backup-*")) == []


def test_owner_snapshot_does_not_start_probe_or_expose_settings(qapp, tmp_path, monkeypatch):
    path = tmp_path / "services.json"
    write_legacy(path)
    supervisor = ServiceSupervisor()
    supervisor.sync(sf.load(str(path)))
    before = path.read_bytes()
    assert supervisor.qcr_snapshot("outer")["legacy_count"] == 1
    assert supervisor.qcr_snapshot("outer")["resources"] == []
    assert path.read_bytes() == before
    sf.migrate_qcr_ids(path)
    supervisor.sync(sf.load(str(path)))
    service = supervisor.service("Group", "Web")
    def forbidden(*args, **kwargs):
        pytest.fail("Read API must not execute or probe a service")
    monkeypatch.setattr(service, "start", forbidden)
    monkeypatch.setattr(service, "poll", forbidden)
    before = path.read_bytes()
    dto = supervisor.qcr_snapshot("outer")
    assert dto["project_id"] == "outer" and dto["legacy_count"] == 0
    assert dto["resources"][0]["properties"]["status"] == "stopped"
    assert "SECRET" not in json.dumps(dto)
    assert "settings" not in json.dumps(dto)
    assert path.read_bytes() == before
    assert service._proc is None
    supervisor.shutdown()


def test_dto_is_consumable_by_core_in_separate_environment(qapp, tmp_path):
    """GUI never imports QCR/core; the wire DTO is passed to a core interpreter."""
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    path = tmp_path / "services.json"
    write_legacy(path)
    sf.migrate_qcr_ids(path)
    supervisor = ServiceSupervisor()
    supervisor.sync(sf.load(str(path)))
    try:
        dto = supervisor.qcr_snapshot("outer")
        command = '''
import json, sys
from qcr import Authority, Grant, Registry, document
from qcr.adapters.services import ServiceAdapter
snapshot = json.load(sys.stdin)
authority = Authority()
caller = authority.issue("gui", [Grant("outer", "services.read")])
registry = Registry(authority)
ServiceAdapter(registry, "outer", lambda: snapshot).refresh()
resource = registry.describe_resource(caller, "outer", "Group/Web")
print(json.dumps(document(registry.get_properties(caller, resource.ref)["status"])))
'''
        core_python = root / ".venv/bin/python"
        if not core_python.exists():
            pytest.skip("Source core interpreter is not available")
        answer = subprocess.run([str(core_python), "-c", command], cwd=root, input=json.dumps(dto),
                                capture_output=True, text=True, timeout=20)
        assert answer.returncode == 0, answer.stderr
        assert json.loads(answer.stdout)["value"] == "stopped"
    finally:
        supervisor.shutdown()


def test_snapshot_follows_actual_service_process(qapp, tmp_path):
    import shlex
    import sys
    import time
    command = shlex.join([sys.executable, "-u", "-c", "import time; print('ready'); time.sleep(30)"])
    row = sf.RunnerRow(name="Web", type="shell", detach=False, settings={"command": command},
                       extra={"qcr_id": "a" * 32})
    supervisor = ServiceSupervisor()
    supervisor.sync([sf.ProjectRow(name="Group", runners=[row], extra={"qcr_id": "b" * 32})])
    try:
        assert supervisor.qcr_snapshot("outer")["resources"][0]["properties"]["status"] == "stopped"
        supervisor.start("Group", "Web")
        deadline = time.monotonic() + 5
        while supervisor.service("Group", "Web").status != "running" and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        observed = supervisor.qcr_snapshot("outer")["resources"][0]
        assert observed["properties"]["status"] == "running"
        assert observed["resource_id"] == "a" * 32
    finally:
        supervisor.shutdown()


def test_migration_does_not_follow_backup_symlink(tmp_path):
    path = tmp_path / "services.json"
    write_legacy(path)
    other = tmp_path / "unrelated.txt"
    other.write_text("untouched")
    backup = tmp_path / "services.json.bak"
    backup.symlink_to(other)
    assert sf.migrate_qcr_ids(path) == 2
    assert other.read_text() == "untouched"
    assert not backup.is_symlink()
