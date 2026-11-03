import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from plugin_host import PluginRegistry
from qavector_sdk import ContractError
from qavector_sdk.versions import satisfies, version


def manifest(release="1.0.0", plugin_id="example.metrics"):
    path = Path(__file__).parents[1] / "examples/plugin_contracts/node-manifest.json"
    result = json.loads(path.read_text())
    result.update(version=release, plugin_id=plugin_id)
    return result


@pytest.fixture
def catalog(tmp_path):
    registry = PluginRegistry(tmp_path / "registry.db")
    yield registry
    registry.close()


def register(catalog, release="1.0.0", digest="a" * 64):
    catalog.register(manifest(release), digest, publisher_identity="verified-key-1")
    catalog.set_state("example.metrics", release, "active")


def test_numeric_versions_and_comparator_intersections():
    assert satisfies("1.10.0", ">1.9.0,<2.0.0")
    assert not satisfies("1.2.0", ">=1.10.0")
    assert satisfies("1.0.0", "==1.0.0")
    assert not satisfies("2.0.0", ">=1.0.0,<2.0.0")


@pytest.mark.parametrize("value", ["1.0", "01.0.0", "1.0.0-rc1", "1.0.0+build", "1.0.0\n", True])
def test_unsupported_version_syntax_is_rejected(value):
    with pytest.raises(ContractError):
        version(value)


@pytest.mark.parametrize("value", ["^1.0.0", "~=1.0.0", "1.0.0", ">=1.0.0, <2.0.0", "!=1.0.0", ">=1.0.0\n"])
def test_unsupported_range_syntax_is_rejected(value):
    with pytest.raises(ContractError):
        satisfies("1.0.0", value)


def test_registration_is_idempotent_but_digest_conflict_is_rejected(catalog):
    register(catalog)
    assert catalog.register(manifest(), "a" * 64, publisher_identity="verified-key-1") is False
    with pytest.raises(ContractError) as caught:
        catalog.register(manifest(), "b" * 64, publisher_identity="verified-key-1")
    assert caught.value.code == "PACKAGE_VERSION_CONFLICT"


def test_publisher_text_cannot_take_over_owned_ids(catalog):
    register(catalog)
    forged = manifest("1.1.0")
    forged["publisher"] = "same text means nothing"
    with pytest.raises(ContractError, match="identity"):
        catalog.register(forged, "b" * 64, publisher_identity="different-key")
    with pytest.raises(ContractError, match="another owner"):
        catalog.register(manifest(plugin_id="example.other"), "c" * 64, publisher_identity="verified-key-1")


def test_official_contribution_and_alias_ownership(catalog):
    official = manifest(plugin_id="qavector.metrics")
    official["contributions"][0]["id"] = "qavector.metrics.count"
    with pytest.raises(ContractError, match="Official"):
        catalog.register(official, "a" * 64, publisher_identity="untrusted")
    catalog.register(official, "a" * 64, publisher_identity="bundled", bundled=True,
                     aliases={"count": "qavector.metrics.count"})
    catalog.set_state("qavector.metrics", "1.0.0", "active")
    assert catalog.resolve("count")["contribution"]["id"] == "qavector.metrics.count"
    with pytest.raises(ContractError):
        catalog.register(manifest(), "b" * 64, publisher_identity="other", aliases={"count": "example.metrics.count"})


def test_incompatible_api_is_rejected_before_registration(catalog):
    incompatible = manifest()
    incompatible["compatibility"]["host_api"] = ">=2.0.0"
    with pytest.raises(ContractError) as caught:
        catalog.register(incompatible, "a" * 64, publisher_identity="verified-key-1")
    assert caught.value.code == "PLUGIN_API_INCOMPATIBLE"
    assert catalog.db.execute("SELECT COUNT(*) FROM packages").fetchone()[0] == 0


def test_active_update_preserves_durable_run_and_resume_lock(tmp_path):
    path = tmp_path / "registry.db"
    catalog = PluginRegistry(path)
    register(catalog)
    requirements = {"example.metrics.count": {"constraint": ">=1.0.0,<2.0.0"}}
    original = catalog.lock_run("run-1", requirements)
    register(catalog, "1.1.0", "b" * 64)
    assert catalog.lock_run("run-2", requirements)["bindings"]["example.metrics.count"]["version"] == "1.1.0"
    catalog.close()
    restored = PluginRegistry(path)
    assert restored.lock_run("run-1", requirements) == original
    restored.set_state("example.metrics", "1.0.0", "disabled")
    assert restored.lock_run("run-1", requirements) == original
    restored.close()


def test_no_silent_fallback_when_active_version_violates_constraint(catalog):
    register(catalog)
    register(catalog, "2.0.0", "b" * 64)
    with pytest.raises(ContractError):
        catalog.resolve("example.metrics.count")
    pin = {"plugin_id": "example.metrics", "version": "1.0.0", "digest": "a" * 64}
    assert catalog.resolve("example.metrics.count", pin=pin)["version"] == "1.0.0"
    with pytest.raises(ContractError, match="digest"):
        catalog.resolve("example.metrics.count", pin=dict(pin, digest="c" * 64))


@pytest.mark.parametrize("state", ["disabled", "quarantined", "incompatible"])
def test_unavailable_states_block_new_runs_even_with_exact_pin(catalog, state):
    register(catalog)
    catalog.set_state("example.metrics", "1.0.0", state)
    with pytest.raises(ContractError):
        catalog.resolve("example.metrics.count", pin={"plugin_id": "example.metrics", "version": "1.0.0", "digest": "a" * 64})


def test_entire_run_lock_rolls_back_on_missing_contribution(catalog):
    register(catalog)
    with pytest.raises(ContractError):
        catalog.lock_run("run", {"example.metrics.count": {"constraint": ">=1.0.0"},
                                 "example.missing": {"constraint": ">=1.0.0"}})
    assert catalog.db.execute("SELECT COUNT(*) FROM run_locks").fetchone()[0] == 0


def test_concurrent_identical_registration_serializes(tmp_path):
    path = tmp_path / "registry.db"
    PluginRegistry(path).close()

    def install(_):
        catalog = PluginRegistry(path)
        try:
            return catalog.register(manifest(), "a" * 64, publisher_identity="verified-key-1")
        finally:
            catalog.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(install, range(2))) == [False, True]
