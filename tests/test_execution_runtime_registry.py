"""Registry history, concurrent edits, corruption and safe JSON diagnostics."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from execution_runtime._json import MAX_DOCUMENT_BYTES, read_document
from execution_runtime.errors import RuntimePolicyError
from execution_runtime.registry import EnvironmentProfile, EnvironmentRegistry


def profile(description="first", name="project-development"):
    return EnvironmentProfile({"api_version": "qavector.io/v1", "kind": "ExecutionEnvironment",
                               "metadata": {"name": name, "description": description},
                               "spec": {"provider": "auto", "policy": {"network": {"mode": "none"}}}})


def test_read_only_list_does_not_create_a_registry(tmp_path):
    root = tmp_path / "profiles"
    assert EnvironmentRegistry(root).list() == ()
    assert not root.exists()


def test_revisions_are_immutable_and_pin_old_profile_content(tmp_path):
    registry = EnvironmentRegistry(tmp_path)
    first = registry.save(profile())
    second = registry.save(profile("second"), expected_revision=first.revision)
    assert first.revision != second.revision
    assert registry.load(first.profile.name, first.revision) == first
    assert registry.load(first.profile.name) == second
    assert registry.list() == (second,)
    assert not (tmp_path / first.profile.name / ".writer-lock").exists()


def test_updating_requires_revision_and_stale_editor_cannot_overwrite(tmp_path):
    registry = EnvironmentRegistry(tmp_path)
    first = registry.save(profile())
    with pytest.raises(RuntimePolicyError) as exc:
        registry.save(profile("second"))
    assert exc.value.code == "revision_required"
    second = registry.save(profile("second"), expected_revision=first.revision)
    with pytest.raises(RuntimePolicyError) as exc:
        registry.save(profile("third"), expected_revision=first.revision)
    assert exc.value.code == "revision_conflict"
    assert registry.load(first.profile.name) == second


def test_repeated_identical_save_is_idempotent_but_checks_explicit_expected_revision(tmp_path):
    registry = EnvironmentRegistry(tmp_path)
    first = registry.save(profile())
    assert registry.save(profile()) == first
    with pytest.raises(RuntimePolicyError):
        registry.save(profile(), expected_revision="b" * 64)


def test_concurrent_writers_do_not_lose_updates(tmp_path):
    registry = EnvironmentRegistry(tmp_path)
    first = registry.save(profile())

    def save(description):
        try:
            return registry.save(profile(description), expected_revision=first.revision)
        except RuntimePolicyError as exc:
            assert exc.code in ("registry_busy", "revision_conflict")
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(save, ("second", "third")))
    successful = [one for one in outcomes if one is not None]
    assert len(successful) == 1
    assert registry.load(first.profile.name) == successful[0]
    assert registry.load(first.profile.name, first.revision) == first


def test_partial_revision_write_does_not_publish_a_new_head(tmp_path, monkeypatch):
    from execution_runtime import registry as module
    registry = EnvironmentRegistry(tmp_path)
    first = registry.save(profile())
    real_atomic = module._atomic

    def fail_head(path, document):
        if path.name == "current.json":
            raise OSError("simulated storage failure")
        real_atomic(path, document)

    monkeypatch.setattr(module, "_atomic", fail_head)
    with pytest.raises(OSError):
        registry.save(profile("second"), expected_revision=first.revision)
    assert registry.load(first.profile.name) == first
    assert not (tmp_path / first.profile.name / ".writer-lock").exists()


def test_tampered_revision_is_rejected_and_not_repaired_by_save(tmp_path):
    registry = EnvironmentRegistry(tmp_path)
    first = registry.save(profile())
    path = tmp_path / first.profile.name / (first.revision + ".json")
    document = json.loads(path.read_text())
    document["profile"]["spec"]["policy"]["network"]["mode"] = "full"
    path.write_text(json.dumps(document))
    for action in (lambda: registry.load(first.profile.name), lambda: registry.save(profile("second"), first.revision)):
        with pytest.raises(RuntimePolicyError) as exc:
            action()
        assert exc.value.code == "revision_mismatch"


@pytest.mark.parametrize("name", ["../private", "UPPER", "", "con", "nul", "com1", "a/b", "a\\b", "a:stream"])
def test_profile_names_are_portable_and_cannot_traverse(name, tmp_path):
    with pytest.raises(RuntimePolicyError):
        EnvironmentRegistry(tmp_path).load(name)


@pytest.mark.parametrize("revision", ["../private", "", "a" * 63, "A" * 64, "a" * 64 + "/extra"])
def test_revision_cannot_select_a_path_outside_profile(revision, tmp_path):
    with pytest.raises(RuntimePolicyError):
        EnvironmentRegistry(tmp_path).load("project-development", revision)


def test_symlinked_revision_cannot_be_followed(tmp_path):
    registry = EnvironmentRegistry(tmp_path / "profiles")
    first = registry.save(profile())
    path = registry.directory / first.profile.name / (first.revision + ".json")
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_bytes(path.read_bytes())
    path.unlink()
    try:
        path.symlink_to(elsewhere)
    except OSError:
        pytest.skip("symlink creation unavailable; native Windows reparse tests remain required")
    with pytest.raises(RuntimePolicyError) as exc:
        registry.load(first.profile.name)
    assert exc.value.code == "unsafe_registry_path"


@pytest.mark.parametrize("text,code", [('{"token":"secret-value", "token":1}', "duplicate_key"),
                                      ('{"x":NaN}', "invalid_number"),
                                      ('{"token":"secret-value",}', "invalid_json"),
                                      ('x' * (MAX_DOCUMENT_BYTES + 1), "document_too_large")])
def test_input_diagnostics_never_echo_json_contents(tmp_path, text, code):
    path = tmp_path / "input.json"
    path.write_text(text)
    with pytest.raises(RuntimePolicyError) as exc:
        read_document(path)
    assert exc.value.code == code
    assert "secret-value" not in str(exc.value.to_document())


def test_profile_schema_does_not_accept_raw_credentials():
    raw = profile().to_document()
    raw["spec"]["policy"]["secrets"] = {"API_KEY": "secret-value"}
    with pytest.raises(RuntimePolicyError) as exc:
        EnvironmentProfile(raw)
    assert "secret-value" not in str(exc.value.to_document())
