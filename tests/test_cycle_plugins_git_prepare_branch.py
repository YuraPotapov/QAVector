"""Task branches against real local remotes, including stale refs and conflicts."""

import subprocess

import pytest

from cycle import registry
from cycle.context import RunContext
from cycle.workspace import create
from domain.cycle import CycleRun, CycleStep


def git(where, *args):
    return subprocess.run(["git", *args], cwd=str(where), check=True,
                          capture_output=True, text=True).stdout.strip()


def commit_file(where, name, content):
    (where / name).write_text(content)
    git(where, "add", "--", name)
    git(where, "commit", "-qm", name)
    return git(where, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path):
    remote, author, repo = (tmp_path / name for name in ("remote.git", "author", "repo"))
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "init", "-b", "mainline", str(author))
    git(author, "config", "user.name", "Test")
    git(author, "config", "user.email", "test@example.invalid")
    commit_file(author, "base.txt", "initial\n")
    git(author, "remote", "add", "origin", str(remote))
    git(author, "push", "origin", "mainline")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/mainline")
    git(tmp_path, "clone", str(remote), str(repo))
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    # The clone starts stale on purpose. Preparation must fetch this commit.
    latest = commit_file(author, "production.txt", "new upstream work\n")
    git(author, "push", "origin", "mainline")

    def prepare(**settings):
        workspace = create("branch-test", str(tmp_path / "runs"))
        context = RunContext(CycleRun(id="r", cycle_id="c", workspace=workspace),
                             None, workspace)
        step = CycleStep(id="prepare", plugin="git.prepare_branch", settings=dict(
            {"directory": str(repo), "branch": "TASK-1",
             "base": "mainline"}, **settings))
        return registry.get(step.plugin).execute(context, step)

    return repo, author, remote, latest, prepare


def test_new_task_branch_uses_fresh_remote_base_not_local_production(world):
    repo, author, remote, latest, prepare = world
    commit_file(repo, "local-only.txt", "must not become the base\n")
    local_base = git(repo, "rev-parse", "HEAD")
    remote_before = git(remote, "show-ref")
    result = prepare()
    assert result.ok, result.message
    assert result.outputs == {"branch": "TASK-1", "base_commit": latest,
                              "commit": latest, "created": True,
                              "offline": False,
                              "base_ref": "refs/remotes/origin/mainline"}
    assert git(repo, "branch", "--show-current") == "TASK-1"
    assert not (repo / "local-only.txt").exists()
    assert (repo / "production.txt").exists()
    assert git(repo, "rev-parse", "mainline") == local_base
    assert git(remote, "show-ref") == remote_before


def test_existing_local_task_branch_keeps_work_and_merges_latest_base(world):
    repo, author, remote, latest, prepare = world
    git(repo, "checkout", "-b", "TASK-1")
    task = commit_file(repo, "task.txt", "local task work\n")
    git(repo, "checkout", "mainline")
    result = prepare()
    assert result.ok, result.message
    assert result.outputs["created"] is False
    assert git(repo, "branch", "--show-current") == "TASK-1"
    git(repo, "merge-base", "--is-ancestor", latest, "HEAD")
    git(repo, "merge-base", "--is-ancestor", task, "HEAD")
    assert (repo / "task.txt").exists() and (repo / "production.txt").exists()
    before = git(repo, "rev-parse", "HEAD")
    assert prepare().ok
    assert git(repo, "rev-parse", "HEAD") == before


def test_existing_remote_task_branch_is_checked_out_and_updated(world):
    repo, author, remote, latest, prepare = world
    git(author, "checkout", "-b", "TASK-1", "HEAD^")
    task = commit_file(author, "remote-task.txt", "existing remote work\n")
    git(author, "push", "origin", "TASK-1")
    result = prepare()
    assert result.ok, result.message
    assert result.outputs["created"] is True
    assert git(repo, "branch", "--show-current") == "TASK-1"
    assert git(repo, "rev-parse", "--abbrev-ref", "@{upstream}") == "origin/TASK-1"
    git(repo, "merge-base", "--is-ancestor", task, "HEAD")
    git(repo, "merge-base", "--is-ancestor", latest, "HEAD")


def test_local_and_remote_task_work_are_both_preserved(world):
    repo, author, remote, latest, prepare = world
    git(author, "checkout", "-b", "TASK-1")
    theirs = commit_file(author, "theirs.txt", "remote task work\n")
    git(author, "push", "origin", "TASK-1")
    git(repo, "checkout", "-b", "TASK-1")
    ours = commit_file(repo, "ours.txt", "local task work\n")
    result = prepare()
    assert result.ok, result.message
    for revision in (theirs, ours, latest):
        git(repo, "merge-base", "--is-ancestor", revision, "HEAD")


def test_deleted_remote_task_ref_is_not_reused(world):
    repo, author, remote, latest, prepare = world
    git(author, "checkout", "-b", "TASK-1")
    commit_file(author, "obsolete.txt", "deleted task branch\n")
    git(author, "push", "origin", "TASK-1")
    git(repo, "fetch", "origin")
    git(author, "push", "origin", "--delete", "TASK-1")
    result = prepare()
    assert result.ok, result.message
    assert git(repo, "rev-parse", "HEAD") == latest
    assert not (repo / "obsolete.txt").exists()


def test_fetch_failure_does_not_use_cached_production(world, tmp_path):
    repo, author, remote, latest, prepare = world
    before = git(repo, "rev-parse", "HEAD")
    git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    result = prepare()
    assert not result.ok
    assert "Cannot fetch" in result.message
    assert git(repo, "rev-parse", "HEAD") == before
    assert git(repo, "branch", "--show-current") == "mainline"
    assert git(repo, "branch", "--list", "TASK-1") == ""


# ------------------------------------------------ when the remote will not answer
# A laptop off the VPN still has a base branch to start from, and losing a whole
# run to a network that dropped for a minute is the worse of the two answers -
# but only where the cycle asked for that, and never silently.
def test_offline_prepares_the_branch_from_what_the_checkout_has(world, tmp_path):
    repo, author, remote, latest, prepare = world
    git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    cached = git(repo, "rev-parse", "refs/remotes/origin/mainline")

    result = prepare(offline=True)

    assert result.ok, result.message
    assert result.outputs["offline"] is True
    assert result.outputs["created"] is True
    assert result.outputs["base_commit"] == cached
    assert git(repo, "branch", "--show-current") == "TASK-1"


def test_offline_says_nothing_was_fetched_and_what_it_used(world, tmp_path):
    """Working from an old base is fine; not knowing that you are is not.

    "Nothing was fetched" rather than "could not be reached": a remote that is
    not configured at all lands here too, and it was never unreachable.
    """
    repo, author, remote, latest, prepare = world
    git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))

    result = prepare(offline=True)

    assert "nothing was fetched from origin" in result.message
    assert "origin/mainline" in result.message
    assert "as old as this checkout" in result.message
    assert result.outputs["base_ref"] == "refs/remotes/origin/mainline"


def test_offline_falls_back_to_the_local_base_when_nothing_was_fetched(world,
                                                                       tmp_path):
    """A checkout that has never fetched this base still has the branch
    somebody has been working from."""
    repo, author, remote, latest, prepare = world
    local_base = git(repo, "rev-parse", "mainline")
    git(repo, "update-ref", "-d", "refs/remotes/origin/mainline")
    git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))

    result = prepare(offline=True)

    assert result.ok, result.message
    assert result.outputs["base_commit"] == local_base
    assert result.outputs["base_ref"] == "refs/heads/mainline"
    assert "mainline" in result.message


def test_offline_with_no_base_at_all_says_so_rather_than_inventing_one(world,
                                                                       tmp_path):
    repo, author, remote, latest, prepare = world
    git(repo, "checkout", "-q", "--detach")
    git(repo, "update-ref", "-d", "refs/remotes/origin/mainline")
    git(repo, "update-ref", "-d", "refs/heads/mainline")
    git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))

    result = prepare(offline=True)

    assert not result.ok
    assert "no base to start from" in result.message


def test_offline_changes_nothing_else_about_what_stops_the_step(world, tmp_path):
    """The point is the remote, not the guards: a dirty tree still stops it."""
    repo, author, remote, latest, prepare = world
    (repo / "base.txt").write_text("edited and not committed\n")
    git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))

    result = prepare(offline=True)

    assert not result.ok
    assert "uncommitted changes" in result.message


def test_a_reachable_remote_is_still_fetched_when_offline_is_allowed(world):
    """Offline is a fallback, not a mode: nothing about an ordinary run moves."""
    repo, author, remote, latest, prepare = world

    result = prepare(offline=True)

    assert result.ok, result.message
    assert result.outputs["offline"] is False
    assert result.outputs["base_commit"] == latest


@pytest.mark.parametrize("value", ["true", "yes", "1", True])
def test_the_cycle_file_may_spell_the_flag_the_way_yaml_does(world, tmp_path,
                                                             value):
    repo, author, remote, latest, prepare = world
    git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))

    assert prepare(offline=value).ok


@pytest.mark.parametrize("value", ["false", "no", "0", False, ""])
def test_anything_that_is_not_yes_keeps_the_remote_compulsory(world, tmp_path,
                                                              value):
    repo, author, remote, latest, prepare = world
    git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))

    result = prepare(offline=value)

    assert not result.ok
    assert "Cannot fetch" in result.message


def test_a_flag_nobody_can_read_an_intention_from_is_refused(world):
    repo, author, remote, latest, prepare = world

    result = prepare(offline="sometimes")

    assert not result.ok
    assert "must be a boolean" in result.message


def test_missing_remote_base_is_an_error_even_if_local_base_exists(world):
    repo, author, remote, latest, prepare = world
    git(remote, "update-ref", "-d", "refs/heads/mainline")
    result = prepare()
    assert not result.ok
    assert "remote base branch origin/mainline is missing" in result.message
    assert git(repo, "branch", "--show-current") == "mainline"


@pytest.mark.parametrize("staged", [False, True])
def test_dirty_work_is_left_untouched(world, staged):
    repo, author, remote, latest, prepare = world
    (repo / "base.txt").write_text("unfinished work\n")
    if staged:
        git(repo, "add", "base.txt")
    before = git(repo, "status", "--porcelain")
    result = prepare()
    assert not result.ok
    assert "uncommitted changes" in result.message
    assert git(repo, "status", "--porcelain") == before
    assert (repo / "base.txt").read_text() == "unfinished work\n"
    assert git(repo, "branch", "--show-current") == "mainline"


def test_merge_conflict_fails_on_task_branch_and_preserves_conflict(world):
    repo, author, remote, latest, prepare = world
    git(repo, "checkout", "-b", "TASK-1")
    task = commit_file(repo, "base.txt", "task's version\n")
    commit_file(author, "base.txt", "production's version\n")
    git(author, "push", "origin", "mainline")
    result = prepare()
    assert not result.ok
    assert "Cannot merge into task branch TASK-1" in result.message
    assert git(repo, "branch", "--show-current") == "TASK-1"
    assert git(repo, "rev-parse", "HEAD") == task
    assert "base.txt" in git(repo, "diff", "--name-only", "--diff-filter=U")


@pytest.mark.parametrize("branch", ["", "HEAD", "--detach", "@{-1}", "bad name", "mainline"])
def test_invalid_or_base_task_branch_is_refused(world, branch):
    repo, author, remote, latest, prepare = world
    before = git(repo, "rev-parse", "HEAD")
    assert not prepare(branch=branch).ok
    assert git(repo, "rev-parse", "HEAD") == before


def test_alternate_configured_remote_is_supported(world):
    repo, author, remote, latest, prepare = world
    git(repo, "remote", "rename", "origin", "upstream")
    result = prepare(remote="upstream")
    assert result.ok, result.message
    assert result.outputs["base_commit"] == latest


# ------------------------------------------------------- a checkout with no remote
# The step refused a repository whose remote had been removed, before it
# reached any of the offline handling above. There is nothing unreachable about
# a checkout that was never given a remote and nothing misconfigured either, so
# the only thing that makes a directory unworkable here is not being a checkout.
def test_a_checkout_with_no_remote_at_all_still_prepares_the_branch(world):
    repo, _author, _remote, _latest, prepare = world
    git(repo, "remote", "remove", "origin")
    local_base = git(repo, "rev-parse", "refs/heads/mainline")

    result = prepare()                       # no offline setting: none is needed

    assert result.ok, result.message
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "TASK-1"
    assert result.outputs["offline"] is True
    assert result.outputs["base_commit"] == local_base
    assert result.outputs["base_ref"] == "refs/heads/mainline"


def test_it_says_nothing_was_fetched_rather_than_that_nobody_answered(world):
    repo, _author, _remote, _latest, prepare = world
    git(repo, "remote", "remove", "origin")
    result = prepare()

    assert "nothing was fetched from origin" in result.message
    assert "as old as this checkout" in result.message


def test_a_remote_that_is_named_and_missing_is_still_a_mistake_worth_saying(world):
    """A typo should not quietly build on a stale base, so this one follows
    `offline` like a failed fetch rather than carrying on by itself."""
    repo, _author, _remote, _latest, prepare = world
    result = prepare(remote="orgin")

    assert not result.ok
    assert "orgin" in result.message
    # And it says what this checkout actually has, which is the whole of what
    # somebody needs to fix it.
    assert "origin" in result.message


def test_a_named_missing_remote_carries_on_when_offline_is_allowed(world):
    repo, _author, _remote, _latest, prepare = world
    result = prepare(remote="orgin", offline=True)

    assert result.ok, result.message
    assert result.outputs["offline"] is True
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "TASK-1"


def test_a_directory_that_is_not_a_checkout_is_still_refused(world, tmp_path):
    """The one thing that makes a directory unworkable here."""
    _repo, _author, _remote, _latest, prepare = world
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    result = prepare(directory=str(plain), offline=True)

    assert not result.ok
    assert "not a git working checkout" in result.message.lower()


def test_with_no_remote_a_dirty_tree_still_stops_it(world):
    """Everything else about the step is unchanged."""
    repo, _author, _remote, _latest, prepare = world
    git(repo, "remote", "remove", "origin")
    (repo / "scratch.txt").write_text("uncommitted\n")
    result = prepare()

    assert not result.ok
    assert "uncommitted changes" in result.message


def test_with_no_remote_and_no_base_branch_it_says_so(world):
    repo, _author, _remote, _latest, prepare = world
    git(repo, "remote", "remove", "origin")
    result = prepare(base="nonexistent")

    assert not result.ok
    assert "no base to start from" in result.message
