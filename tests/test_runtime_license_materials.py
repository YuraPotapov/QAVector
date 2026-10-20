"""Release gates must reject incomplete or misleading third-party materials."""

import importlib.util
import json
from pathlib import Path
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("runtime_license_gate", ROOT / "packaging/check_licenses.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


@pytest.fixture
def materials(tmp_path):
    return Path(shutil.copytree(ROOT / "licenses", tmp_path / "licenses"))


def test_verbatim_upstream_license_and_accurate_component_version_are_present(materials):
    entry = gate.verify_licenses(materials)["components"][0]
    assert entry["version"] == gate.source_lock()["version"]
    assert entry["license"] == "LGPL-2.1-or-later"


@pytest.mark.parametrize("name", ["bubblewrap/NOTICE.md", "bubblewrap/AUTHORS", "bubblewrap/COPYING"])
def test_release_is_blocked_if_mandatory_offline_materials_are_missing(materials, name):
    (materials / name).unlink()
    with pytest.raises((ValueError, OSError)):
        gate.verify_licenses(materials)


def test_altered_or_shortened_license_is_not_a_complete_license(materials):
    with (materials / "bubblewrap/COPYING").open("a", encoding="utf-8") as stream:
        stream.write("Altered license terms\n")
    with pytest.raises(ValueError, match="verbatim"):
        gate.verify_licenses(materials)


def test_attribution_cannot_be_replaced_by_qavector_authorship(materials):
    path = materials / "bubblewrap/NOTICE.md"
    path.write_text(path.read_text(encoding="utf-8").replace("Alexander Larsson", "QAVector"), encoding="utf-8")
    with pytest.raises(ValueError, match="Alexander Larsson"):
        gate.verify_licenses(materials)


def test_catalog_cannot_claim_a_different_component_version(materials):
    path = materials / "components.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    catalog["components"][0]["version"] = "0.0"
    path.write_text(json.dumps(catalog), encoding="utf-8")
    with pytest.raises(ValueError, match="version/license"):
        gate.verify_licenses(materials)


def test_bundled_library_cannot_be_omitted_from_attribution(materials):
    with pytest.raises(ValueError, match="libcap"):
        gate.verify_licenses(materials, require_libcap=True)
