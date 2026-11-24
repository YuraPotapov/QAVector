"""The folder's branch for the chat's summary: which repository, which branch,
whether it has changes - read by git, once, without taking its locks."""

import os
import subprocess

import pytest

from cms_gui import gitstate

pytestmark = pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode,
                                reason="git is not installed")


def _git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    subprocess.run(["git", "-C", str(repo)] + list(args), check=True, capture_output=True, env=env)


def _repo(path, branch="main"):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", branch)
    (path / "a.txt").write_text("a")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-q", "-m", "first")
    return path


def test_a_folder_that_is_a_repository_says_its_branch_and_changes(tmp_path):
    repo = _repo(tmp_path / "shop", "task-1")
    found = gitstate.state(str(repo))
    assert (found["branch"], found["dirty"], found["name"]) == ("task-1", False, "")
    (repo / "a.txt").write_text("changed")
    assert gitstate.state(str(repo))["dirty"] is True
    assert not os.path.exists(repo / ".git" / "index.lock")


def test_of_several_repositories_the_one_that_moved_last_is_told(tmp_path):
    _repo(tmp_path / "project")
    old = _repo(tmp_path / "project" / "odoo", "17.0")
    modules = _repo(tmp_path / "project" / "modules", "release")
    for path in (tmp_path / "project", old):
        for root, _dirs, files in os.walk(path / ".git"):
            for name in files:
                os.utime(os.path.join(root, name), (1, 1))
    (modules / "b.txt").write_text("new")
    found = gitstate.state(str(tmp_path / "project"))
    assert (found["name"], found["branch"], found["dirty"], found["repositories"]) == ("modules", "release", True, 3)


def test_no_repository_or_no_folder_says_nothing(tmp_path):
    assert gitstate.state(str(tmp_path)) is None
    assert gitstate.state(str(tmp_path / "gone")) is None
    assert gitstate.state("") is None


def test_status_lines_name_their_branch():
    assert gitstate.branch_of("## main...origin/main [ahead 1]") == "main"
    assert gitstate.branch_of("## No commits yet on fresh") == "fresh"
    assert gitstate.branch_of("## HEAD (no branch)") == ""
    assert gitstate.branch_of("## feature/x") == "feature/x"
