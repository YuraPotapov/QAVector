"""Standalone worker semantics on temporary fixtures; native isolation is tested separately."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import pytest

from execution_runtime.workers import GIT_CONTRACT, git_artifact

pytestmark = pytest.mark.skipif(sys.platform != "linux" or shutil.which("git") is None, reason="Linux Git worker artifact")


def git(directory, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run([shutil.which("git"), "-C", str(directory), "-c", "user.name=Runtime Test",
        "-c", "user.email=runtime@example.invalid", "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=True, env=env).stdout.strip()


@pytest.fixture
def checkout_packet(tmp_path):
    base = tmp_path / "project"
    base.mkdir()
    source = base / "source repo"
    source.mkdir()
    git(source, "init")
    git(source, "checkout", "-b", "main")
    (source / "code.txt").write_text("first\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "first")
    revision = git(source, "rev-parse", "HEAD")
    git(source, "checkout", "-b", "feature")
    (source / "code.txt").write_text("feature\n")
    git(source, "commit", "-am", "feature")
    value = dict(contract=GIT_CONTRACT, operation="checkout", nonce=uuid.uuid4().hex, base=str(base),
                 repository=str(source), path="checkout/result", branch="main", commit="", depth=0, git=shutil.which("git"))
    return value, revision


def execute(value, env=None):
    path, _ = git_artifact("linux")
    done = subprocess.run([sys.executable, "-I", "-B", str(path), json.dumps(value)],
        capture_output=True, text=True, timeout=10, env=env)
    return done, json.loads(done.stdout)


@pytest.mark.parametrize("detached", [False, True])
def test_worker_clones_selected_branch_or_commit_without_sharing_or_modifying_source(checkout_packet, detached):
    value, revision = checkout_packet
    if detached:
        value.update(branch="", commit=revision)
    done, result = execute(value)
    assert done.returncode == 0, (result, done.stderr)
    assert result["outputs"] == {"commit": revision, "branch": "HEAD" if detached else "main"}
    target, source = Path(value["base"]) / value["path"], Path(value["repository"])
    assert (target / "code.txt").read_text() == "first\n"
    assert (source / "code.txt").read_text() == "feature\n"
    assert git(source, "branch", "--show-current") == "feature"
    obj = Path(".git/objects") / revision[:2] / revision[2:]
    assert (source / obj).stat().st_ino != (target / obj).stat().st_ino
    assert not list(target.parent.glob(".qavector-checkout-*"))


def test_existing_empty_destination_is_never_overwritten(checkout_packet):
    value, _ = checkout_packet
    target = Path(value["base"]) / value["path"]
    target.mkdir(parents=True)
    before = target.stat().st_ino
    done, result = execute(value)
    assert done.returncode == 1 and result["code"] == "checkout_destination_exists"
    assert target.stat().st_ino == before and not list(target.iterdir())


def test_injected_git_environment_and_global_hooks_do_not_run(checkout_packet, tmp_path):
    value, _ = checkout_packet
    marker = tmp_path / "hook-was-run"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "post-checkout"
    hook.write_text('#!/bin/sh\nprintf wrong > "' + str(marker) + '"\n')
    hook.chmod(0o700)
    global_config = tmp_path / "gitconfig"
    global_config.write_text('[core]\n hooksPath = ' + str(hooks) + '\n')
    env = dict(os.environ, GIT_CONFIG_GLOBAL=str(global_config), GIT_DIR="/nonexistent", GIT_CONFIG_COUNT="1",
               GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0=str(hooks))
    done, result = execute(value, env)
    assert done.returncode == 0, (result, done.stderr)
    assert not marker.exists()


def test_cleanup_is_owned_idempotent_and_preserves_published_checkout(checkout_packet):
    value, _ = checkout_packet
    done, result = execute(value)
    assert done.returncode == 0, (result, done.stderr)
    for _ in range(2):
        cleaned, evidence = execute(dict(value, operation="cleanup"))
        assert cleaned.returncode == 0 and evidence["outputs"] == {}
    assert (Path(value["base"]) / value["path"] / "code.txt").exists()


def test_cleanup_removes_only_matching_abandoned_staging(checkout_packet):
    value, _ = checkout_packet
    target = Path(value["base"]) / value["path"]
    stage = target.parent / (".qavector-checkout-" + value["nonce"])
    stage.mkdir(parents=True)
    owner = dict(contract=GIT_CONTRACT, nonce=value["nonce"], destination=str(target))
    (stage / "owner.json").write_text(json.dumps(owner))
    (stage / "partial").write_text("partial")
    done, result = execute(dict(value, operation="cleanup"))
    assert done.returncode == 0, (result, done.stderr)
    assert not stage.exists()
    stage.mkdir()
    (stage / "owner.json").write_text(json.dumps(dict(owner, nonce="foreign")))
    (stage / "keep").write_text("user data")
    done, result = execute(dict(value, operation="cleanup"))
    assert done.returncode == 1 and (stage / "keep").read_text() == "user data"


def test_symlink_parent_escape_is_refused(checkout_packet, tmp_path):
    value, _ = checkout_packet
    outside = tmp_path / "outside"
    outside.mkdir()
    (Path(value["base"]) / "escape").symlink_to(outside)
    value["path"] = "escape/result"
    done, result = execute(value)
    assert done.returncode == 1 and not list(outside.iterdir())


def test_atomic_publication_preserves_a_destination_created_after_initial_check(tmp_path):
    from execution_runtime.workers.git_checkout_linux import publish, WorkerError
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir(); target.mkdir()
    (source / "keep").write_text("source")
    inode = target.stat().st_ino
    with pytest.raises(WorkerError, match="checkout_destination_exists"):
        publish(source, target)
    assert target.stat().st_ino == inode and not list(target.iterdir())
    assert (source / "keep").read_text() == "source"
