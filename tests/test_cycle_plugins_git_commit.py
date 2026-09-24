"""Committing a tree somebody verified, and only that tree.

Every test drives a real git repository in a tmp_path. A fake would prove
nothing here: the whole plugin is a conversation with git about hashes, and a
stub that agreed with it by construction would agree with a broken version too.

Three properties carry the plugin, and each has its own section: it refuses
when the files changed after they were verified, it finds its own earlier
commit instead of making a second, and it fails loudly when a hook rewrites
the tree it just committed.
"""

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cycle import registry                                        # noqa: E402
from cycle.context import CancelToken, RunContext                 # noqa: E402
from cycle.plugins.git_commit import TRAILER, _operation_id       # noqa: E402
from cycle.workspace import create                                # noqa: E402
from domain.cycle import CycleRun, CycleStep                      # noqa: E402


def _git(where, *arguments, **kwargs):
    return subprocess.run(["git"] + list(arguments), cwd=str(where),
                          capture_output=True, text=True, **kwargs)


@pytest.fixture
def repo(tmp_path):
    """A repository with one commit already in it."""
    where = tmp_path / "repo"
    where.mkdir()
    _git(where, "init", "-q", check=True)
    _git(where, "config", "user.email", "t@example.invalid", check=True)
    _git(where, "config", "user.name", "Tester", check=True)
    (where / "kept.txt").write_text("one\n")
    _git(where, "add", "-A", check=True)
    _git(where, "commit", "-qm", "the beginning", check=True)
    return where


@pytest.fixture
def commit(tmp_path, repo):
    """Runs git.commit against that repository."""
    def go(cancel=None, **settings):
        workspace = create("20260919-000000-g", str(tmp_path / "runs"))
        context = RunContext(CycleRun(id="r", cycle_id="c",
                                      workspace=workspace),
                             None, workspace, cancel=cancel or CancelToken())
        step = CycleStep(id="commit", plugin="git.commit", settings=dict({
            "directory": str(repo), "message": "the work"}, **settings))
        return registry.get("git.commit").execute(context, step)
    return go


def _tree_now(repo, tmp_path):
    """The hash the working directory has right now, git's own answer."""
    index = str(tmp_path / ("idx-%d" % len(os.listdir(str(tmp_path)))))
    environment = dict(os.environ, GIT_INDEX_FILE=index)
    subprocess.run(["git", "read-tree", "HEAD"], cwd=str(repo),
                   env=environment, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=str(repo), env=environment,
                   check=True, capture_output=True)
    return subprocess.run(["git", "write-tree"], cwd=str(repo),
                          env=environment, capture_output=True,
                          text=True).stdout.strip()


# ------------------------------------------------------------ the plain case
def test_it_commits_what_is_there(repo, commit):
    (repo / "new.txt").write_text("two\n")
    result = commit()

    assert result.ok, result.message
    assert result.outputs["created"] is True
    assert result.outputs["changed_files"] == ["new.txt"]
    assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == "the work"


def test_what_it_reports_is_read_back_from_git_not_assumed(repo, commit):
    (repo / "new.txt").write_text("two\n")
    result = commit()

    assert result.outputs["commit"] == _git(repo, "rev-parse",
                                            "HEAD").stdout.strip()
    assert result.outputs["tree"] == _git(repo, "rev-parse",
                                          "HEAD^{tree}").stdout.strip()
    assert result.outputs["parent"] == _git(repo, "rev-parse",
                                            "HEAD^").stdout.strip()


def test_only_the_named_paths_are_staged_when_the_step_names_any(repo, commit):
    (repo / "wanted.txt").write_text("in\n")
    (repo / "other.txt").write_text("out\n")
    result = commit(add=["wanted.txt"])

    assert result.outputs["changed_files"] == ["wanted.txt"]
    assert "other.txt" in _git(repo, "status", "--porcelain").stdout


def test_the_author_can_be_said_by_the_step(repo, commit):
    (repo / "new.txt").write_text("two\n")
    commit(author_name="QAVector", author_email="cycle@example.invalid")

    assert _git(repo, "log", "-1",
                "--format=%an <%ae>").stdout.strip() == (
        "QAVector <cycle@example.invalid>")


def test_nothing_to_commit_says_so_rather_than_making_an_empty_commit(repo,
                                                                     commit):
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    result = commit()

    assert result.ok
    assert result.outputs["created"] is False
    assert "nothing to commit" in result.message
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before


def test_an_empty_commit_is_made_when_the_step_asks_for_one(repo, commit):
    result = commit(allow_empty=True)
    assert result.outputs["created"] is True


def test_it_commits_only_on_the_required_task_branch(repo, commit):
    _git(repo, "checkout", "-b", "TASK-1", check=True)
    (repo / "new.txt").write_text("task work\n")
    result = commit(expect_branch="TASK-1")
    assert result.ok, result.message
    assert result.outputs["branch"] == "TASK-1"


@pytest.mark.parametrize("branch", ["mainline", "TASK-2", "feature/TASK-1", None])
def test_wrong_or_detached_branch_refuses_before_staging(repo, commit, branch):
    if branch:
        _git(repo, "checkout", "-b", branch, check=True)
    else:
        _git(repo, "checkout", "--detach", check=True)
    (repo / "new.txt").write_text("task work\n")
    before = _git(repo, "rev-parse", "HEAD").stdout
    result = commit(expect_branch="TASK-1")
    assert not result.ok
    assert "Branch mismatch" in result.message
    assert "TASK-1" in result.message
    assert (branch or "detached HEAD") in result.message
    assert _git(repo, "rev-parse", "HEAD").stdout == before
    assert _git(repo, "diff", "--cached", "--name-only").stdout == ""


def test_empty_required_branch_does_not_disable_the_guard(commit):
    result = commit(expect_branch="")
    assert not result.ok
    assert "Required branch must not be empty" in result.message


def test_branch_is_checked_again_after_hashing_the_work(repo, commit, monkeypatch):
    from cycle.plugins import git_commit

    _git(repo, "checkout", "-b", "TASK-1", check=True)
    (repo / "new.txt").write_text("task work\n")
    original = git_commit._would_be

    def switched(where, paths):
        tree = original(where, paths)
        _git(repo, "checkout", "-b", "wrong-branch", check=True)
        return tree

    monkeypatch.setattr(git_commit, "_would_be", switched)
    result = commit(expect_branch="TASK-1")
    assert not result.ok
    assert "Branch mismatch" in result.message
    assert _git(repo, "diff", "--cached", "--name-only").stdout == ""


# --------------------------------------------------- the tree that was verified
def test_it_refuses_when_the_files_changed_after_they_were_verified(repo,
                                                                   commit,
                                                                   tmp_path):
    """The reason the plugin exists. Whatever is here now is a thing nobody
    checked."""
    (repo / "new.txt").write_text("verified\n")
    verified = _tree_now(repo, tmp_path)
    (repo / "new.txt").write_text("something else entirely\n")

    before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    result = commit(expect_tree=verified)

    assert not result.ok
    assert "changed after they were verified" in result.message
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before, \
        "nothing may be committed when the check fails"
    # And nothing staged either: a refusal must leave the repository exactly as
    # it found it, or somebody is handed a staged index and has to work out
    # what did it.
    assert _git(repo, "diff", "--name-only", "--cached").stdout == ""


def test_it_commits_when_the_tree_is_the_one_that_was_verified(repo, commit,
                                                              tmp_path):
    (repo / "new.txt").write_text("verified\n")
    verified = _tree_now(repo, tmp_path)

    result = commit(expect_tree=verified)

    assert result.ok, result.message
    assert result.outputs["tree"] == verified


def test_a_tree_that_is_not_an_object_id_is_refused_before_anything_runs():
    plugin = registry.get("git.commit")
    assert plugin.problems({"directory": "d", "message": "m",
                            "expect_tree": "the one from before"})


def test_a_reference_is_left_for_the_run_to_resolve():
    plugin = registry.get("git.commit")
    assert plugin.problems({"directory": "d", "message": "m",
                            "expect_tree": "${steps.impl.outputs.tree}"}) == []


# ------------------------------------------------------------- doing it twice
def test_a_rerun_finds_its_own_commit_instead_of_making_a_second(repo, commit):
    """A run killed between the commit and the record leaves a commit nobody
    knows about. The next attempt must not double it."""
    (repo / "new.txt").write_text("two\n")
    first = commit()
    assert first.outputs["created"] is True

    again = commit()

    assert again.ok
    assert again.outputs["created"] is False
    assert again.outputs["commit"] == first.outputs["commit"]
    assert "already made" in again.message
    assert len(_git(repo, "log", "--format=%H").stdout.split()) == 2


def test_the_intent_is_written_into_the_commit_so_it_can_be_found(repo, commit):
    (repo / "new.txt").write_text("two\n")
    result = commit()

    body = _git(repo, "log", "-1", "--format=%B").stdout
    assert "%s: %s" % (TRAILER, result.outputs["operation_id"]) in body


def test_the_same_intent_names_itself_the_same_way_every_time():
    """Which is the whole mechanism: a fresh id each run would find nothing."""
    assert _operation_id("t", "m") == _operation_id("t", "m")
    assert _operation_id("t", "m") != _operation_id("t", "other")
    assert _operation_id("t", "m") != _operation_id("other", "m")


def test_the_intent_does_not_name_the_parent(repo, commit):
    """The parent is the one thing committing changes. An id that included it
    would be different on the run that needed it to match, and the re-run
    would commit a second time."""
    (repo / "one.txt").write_text("1\n")
    first = commit(message="the work")

    # Somebody else commits on top, so HEAD's tree is no longer ours.
    (repo / "unrelated.txt").write_text("theirs\n")
    _git(repo, "add", "-A", check=True)
    _git(repo, "commit", "-qm", "somebody else", check=True)

    # Put our own tree back and ask again: it still recognises its own work.
    (repo / "unrelated.txt").unlink()
    again = commit(message="the work")

    assert again.outputs["created"] is False
    assert again.outputs["commit"] == first.outputs["commit"]
    assert "already made" in again.message


def test_different_work_on_the_same_parent_is_a_different_commit(repo, commit):
    (repo / "one.txt").write_text("1\n")
    first = commit(message="the first thing")
    (repo / "two.txt").write_text("2\n")
    second = commit(message="the second thing")

    assert second.outputs["created"] is True
    assert second.outputs["commit"] != first.outputs["commit"]


# ----------------------------------------------------------------- a hook
def test_a_hook_that_rewrites_the_tree_fails_the_step_and_does_not_retry(repo,
                                                                        commit):
    """The commit exists and says something nobody checked. Naming it and
    stopping is the only honest answer."""
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'tidied' > tidied.txt\ngit add tidied.txt\n")
    os.chmod(str(hook), 0o755)

    (repo / "new.txt").write_text("two\n")
    result = commit()

    assert not result.ok
    assert "not the one that was verified" in result.message
    assert result.outputs["commit"], "the commit it made has to be named"
    assert len(_git(repo, "log", "--format=%H").stdout.split()) == 2, \
        "it must not commit again on top"


# ------------------------------------------------------------------ refusals
def test_a_directory_that_is_not_a_repository_is_refused(tmp_path, commit):
    plain = tmp_path / "plain"
    plain.mkdir()
    result = commit(directory=str(plain))

    assert not result.ok
    assert "Not a git repository" in result.message


def test_a_directory_that_is_not_there_is_refused(commit):
    result = commit(directory="/no/such/place")
    assert not result.ok
    assert "does not exist" in result.message


def test_it_writes_and_it_says_so():
    """A step that commits into somebody's repository should declare it."""
    plugin = registry.get("git.commit")
    assert "filesystem.write" in plugin.metadata.permissions


def test_it_is_its_own_plugin_rather_than_a_mode_of_checkout():
    assert registry.get("git.commit") is not registry.get("git.checkout")


def test_its_metadata_survives_the_wire():
    json.dumps(registry.get("git.commit").metadata.to_dict())
