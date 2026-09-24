"""Local Git repositories exercise checkout without network access."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from cycle.context import RunContext
from cycle.plugins.git import GitCheckout
from domain.cycle import CycleRun, CycleStep

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")


def git(path, *args):
    environment = dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        environment.pop(name, None)
    return subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=Cycle Test",
         "-c", "user.email=cycle@example.invalid", "-c", "commit.gpgsign=false"] + list(args),
        check=True, capture_output=True, text=True, env=environment).stdout.strip()


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source repo"
    path.mkdir()
    git(path, "init")
    git(path, "checkout", "-b", "main")
    (path / "code.txt").write_text("first\n", encoding="utf-8")
    git(path, "add", "code.txt")
    git(path, "commit", "-m", "first")
    first = git(path, "rev-parse", "HEAD")
    git(path, "checkout", "-b", "feature")
    (path / "code.txt").write_text("feature\n", encoding="utf-8")
    git(path, "commit", "-am", "feature")
    return path, first


@pytest.fixture
def context(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    return RunContext(CycleRun(id="r", cycle_id="git"), None, str(root))


def checkout(context, source, **settings):
    return GitCheckout().execute(context, CycleStep(
        id="checkout", plugin="git.checkout", settings=dict(repository=str(source), **settings)))


def test_checkout_returns_selected_branch_and_isolated_workspace(context, source):
    path, first = source
    result = checkout(context, path, branch="main")
    assert result.ok, result.message
    target = Path(context.workspace) / result.outputs["workspace"]
    assert result.outputs["commit"] == first
    assert result.outputs["branch"] == "main"
    assert (target / "code.txt").read_text() == "first\n"
    assert not os.path.isabs(result.outputs["workspace"])
    assert git(path, "branch", "--show-current") == "feature"
    assert (path / "code.txt").read_text() == "feature\n"
    assert all((Path(context.workspace) / one.path).is_file() for one in result.artifacts)


def test_commit_checkout_is_detached_and_uses_the_requested_revision(context, source):
    path, first = source
    result = checkout(context, path, commit=first)
    assert result.ok, result.message
    assert result.outputs["commit"] == first
    assert result.outputs["branch"] == "HEAD"


def test_existing_destination_is_preserved(context, source):
    target = Path(context.workspace) / "existing"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    result = checkout(context, source[0], path="existing")
    assert not result.ok
    assert (target / "keep.txt").read_text() == "keep"
    assert list(target.iterdir()) == [target / "keep.txt"]


@pytest.mark.parametrize("destination", ["..", ".", "../elsewhere"])
def test_destination_must_be_below_the_workspace(context, source, destination):
    result = checkout(context, source[0], path=destination)
    assert not result.ok
    assert "inside the run workspace" in result.message


def test_failed_clone_can_be_retried_and_keeps_its_logs(context, source):
    failed = checkout(context, source[0], branch="no-such-branch")
    assert not failed.ok
    assert failed.artifacts
    assert not (Path(context.workspace) / "source" / "checkout").exists()
    assert not list((Path(context.workspace) / "source").glob(".checkout-*"))
    retried = checkout(context, source[0], branch="main")
    assert retried.ok, retried.message


def test_cancelled_checkout_does_not_create_a_repository(context, source):
    context.cancel.set("stop")
    result = checkout(context, source[0])
    assert not result.ok
    assert not (Path(context.workspace) / "source").exists()


@pytest.mark.parametrize("depth", [-1, 1.5, True, "bad"])
def test_invalid_clone_depth_is_reported(depth):
    assert GitCheckout().problems({"repository": "repo", "depth": depth})
