"""Context packages (ADR-006 D7): what one agent hands another, and what it never does.

Unit tests of qcr.context, then the same through a delegation in the real host.
"""

import json
import os
import subprocess
import time

import pytest

from qcr import QCRError
from qcr import context as packages
from test_qcr_delegation import ACTOR, World, _codex_turns


def _build(folder, **kwargs):
    fields = dict(goal="Fix it", criteria=["passes"], summary="Selector moved.",
                  constraints={"mode": "read"}, folder=str(folder), files=(), artifacts=(),
                  provenance={"actor": "a", "session": "s"}, clock=lambda: 1000.0)
    fields.update(kwargs)
    return packages.build(**fields)


def test_files_are_named_by_path_and_hash_inside_the_folder(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "login.py").write_text("print('x')\n")
    document = _build(tmp_path, files=["src/login.py", "./src/login.py"])
    assert document["files"] == [{"path": "src/login.py", "bytes": 11,
                                  "sha256": packages._sha256_file(str(tmp_path / "src" / "login.py"))}]
    assert document["git"] == {"head": None} and document["provenance"]["packaged_at"] == 1000.0
    for outside in ("../elsewhere.txt", "/etc/hostname", "missing.py"):
        with pytest.raises(QCRError):
            _build(tmp_path, files=[outside])


def test_a_link_out_of_the_folder_is_refused(tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("x")
    folder = tmp_path / "work"
    folder.mkdir()
    os.symlink(secret, folder / "link.txt")
    with pytest.raises(QCRError, match="inside"):
        _build(folder, files=["link.txt"])


def test_git_head_is_recorded_for_a_repository(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a.txt").write_text("a")
    subprocess.run(["git", "-C", str(tmp_path), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "commit", "-qm", "one"], check=True)
    assert len(_build(tmp_path)["git"]["head"]) == 40


@pytest.mark.parametrize("field, value", [
    ("summary", "token is sk-" + "a" * 30),
    ("goal", "use -----BEGIN RSA PRIVATE KEY----- here"),
    ("criteria", ["header has eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4fw"]),
    ("summary", "the password is Hunter2Hunter2"),
])
def test_secrets_are_refused_by_field_never_shown(tmp_path, field, value):
    with pytest.raises(QCRError) as caught:
        _build(tmp_path, known_secrets=["Hunter2Hunter2", "short"], **{field: value})
    assert caught.value.code == "secret_in_context"
    assert "Hunter2" not in str(caught.value) and "sk-" not in str(caught.value)


def test_short_known_secrets_do_not_refuse_ordinary_words(tmp_path):
    assert _build(tmp_path, summary="the build is short", known_secrets=["short"])


def test_size_and_count_limits(tmp_path):
    with pytest.raises(QCRError) as caught:
        _build(tmp_path, summary="x" * (packages.MAX_BYTES + 1))
    assert caught.value.code == "limit_exceeded"
    with pytest.raises(QCRError):
        _build(tmp_path, files=["f%d" % n for n in range(packages.MAX_FILES + 1)])


def test_kept_once_immutable_and_checked_on_load(tmp_path):
    document = _build(tmp_path)
    where = tmp_path / "context"
    ref = packages.keep(document, str(where))
    assert ref == packages.reference(document) and packages.keep(document, str(where)) == ref
    assert oct(os.stat(where / (ref + ".json")).st_mode & 0o777) == "0o600"
    assert packages.load(ref, str(where)) == json.loads(json.dumps(document))
    (where / (ref + ".json")).write_text(json.dumps(dict(document, goal="Something else")))
    with pytest.raises(QCRError) as caught:
        packages.load(ref, str(where))
    assert caught.value.code == "integrity"
    for bad in ("../x", "a" * 63):
        with pytest.raises(QCRError):
            packages.load(bad, str(where))


def test_freshness_names_what_changed_or_went(tmp_path):
    (tmp_path / "a.py").write_text("1")
    (tmp_path / "b.py").write_text("2")
    document = _build(tmp_path, files=["a.py", "b.py"])
    (tmp_path / "a.py").write_text("changed")
    (tmp_path / "b.py").unlink()
    assert packages.freshness(document, str(tmp_path)) == ["a.py", "b.py"]
    told = packages.render(document, sender="Claude", mode="read", changed=["a.py"])
    assert "a.py" in told and "(changed since the task was given)" in told


# -- through a delegation ------------------------------------------------------------
@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "codex.jsonl"))
    made = []

    def make(**kwargs):
        one = World(tmp_path, **kwargs)
        made.append(one)
        return one
    yield make
    for one in made:
        one.close()


def test_the_child_is_told_the_package_and_the_output_names_it(world, tmp_path):
    w = world(others="approve")
    (w.folder / "login.py").write_text("old\n")
    execution_id = w.delegate(files=["login.py"])["execution"]["execution_id"]
    card = next(one for one in w.sent if one.get("type") == "agent_approval")
    assert card["files"] == 1 and len(card["context_package"]) == 64
    # The file changes while the person decides: the child is told so.
    (w.folder / "login.py").write_text("new\n")
    w.host._on_agent_answer({"execution_id": execution_id, "approve": True, "by": "you"})
    done = w.wait(execution_id)
    assert done["state"] == "succeeded", done
    assert done["output"]["context_package"] == card["context_package"]
    assert done["output"]["stale_files"] == ["login.py"]
    told = [row for row in _codex_turns(tmp_path) if row.get("method") == "turn/start"][0]["params"]["input"][0]["text"]
    assert "login.py" in told and "(changed since the task was given)" in told
    kept = packages.load(card["context_package"], w.host.context_dir)
    assert kept["provenance"]["actor"] == ACTOR and kept["constraints"]["qavector_access"] is False


def test_artifacts_must_be_the_delegators_own_runs(world):
    w = world()
    first = w.wait(w.delegate(goal="Run it")["execution"]["execution_id"])
    second = w.wait(w.delegate(goal="Check it", artifacts=[first["execution_id"]])["execution"]["execution_id"])
    assert second["state"] == "succeeded", second
    kept = packages.load(second["output"]["context_package"], w.host.context_dir)
    assert kept["artifacts"][0]["execution_id"] == first["execution_id"]
    assert kept["artifacts"][0]["state"] == "succeeded" and "Answer:" in kept["artifacts"][0]["output_excerpt"]
    w.grant(actor="agent:codex/other", connection="codex", targets=("claude",))
    stolen = w.host.handle_agent("agent:codex/other", {"type": "invoke", "capability": "agents.claude.delegate",
                                                       "input": {"goal": "x", "artifacts": [first["execution_id"]]}})
    ended = w.wait(stolen["execution"]["execution_id"], actor="agent:codex/other")
    assert ended["error"]["code"] == "not_found"


def test_a_secret_in_the_task_is_refused_before_anything_is_written(world, tmp_path):
    w = world(others="approve")
    refused = w.delegate(summary="use sk-" + "b" * 32)
    assert not refused["ok"] and refused["error"]["code"] == "secret_in_context"
    # Not even the journal has it: the Engine never recorded the request.
    assert w.host.store.list("p") == []
    assert not [one for one in w.sent if one.get("type") == "agent_approval"]
    assert not _codex_turns(tmp_path)
    assert not os.path.isdir(w.host.context_dir) or not os.listdir(w.host.context_dir)


def test_known_secret_values_come_from_the_secrets_store(world, tmp_path):
    from cycle import secrets as secret_store
    path = str(tmp_path / "secrets.json")
    secret_store.set_secret("deploy", "TOKEN", "s3cr3t-value-123", path)
    w = world()
    w.host._secrets_path = path
    refused = w.delegate(summary="the token is s3cr3t-value-123")
    assert refused["error"]["code"] == "secret_in_context" and "s3cr3t" not in json.dumps(refused)
    assert w.host.store.list("p") == []
