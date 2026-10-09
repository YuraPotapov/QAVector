"""The step that lets an agent merge branches - locally, and nowhere else.

What is worth defending has little to do with the model. The repository must
have nowhere to push to, checked before and after; the CLI must be given the
git commands it may run and told by name the ones it may not; and every
command the agent ran must come back as a list, so a review can check the
work against the plan rather than against the agent's account of it.

The fake `claude` below really runs the git commands it reports, so the
repository afterwards is the evidence, not the plugin's bookkeeping.
"""

import json
import os
import stat
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cycle import registry                                        # noqa: E402
from cycle.context import RunContext                              # noqa: E402
from cycle.plugins import agent_stream                            # noqa: E402
from cycle.plugins.agent_merge import GIT_ALLOWED, GIT_REFUSED   # noqa: E402
from cycle.workspace import create                                # noqa: E402
from domain.cycle import CycleRun, CycleStep                      # noqa: E402

MERGED = """
import subprocess
for command in ("git status --short", "git merge --no-ff --no-edit feature"):
    say({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": command}}]}})
    subprocess.run(command.split(), cwd=where, check=False,
                   capture_output=True)
say({"type": "result", "subtype": "success", "is_error": False,
     "num_turns": 2, "total_cost_usd": 0.02, "result": "Merged feature."})
"""

ADDS_A_REMOTE = """
import subprocess
subprocess.run(["git", "remote", "add", "sneaky", "/nowhere"], cwd=where)
say({"type": "result", "subtype": "success", "is_error": False,
     "num_turns": 1, "result": "done"})
"""


def _git(where, *args):
    return subprocess.run(["git", "-C", str(where)] + list(args), check=True,
                          capture_output=True, text=True).stdout


def _claude(directory, body, argv_path):
    path = os.path.join(str(directory), "claude")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!%s\n%s\n" % (sys.executable, """
import json, os, sys
if "auth" in sys.argv:
    print(json.dumps({"loggedIn": True, "email": "a@example.invalid",
                      "subscriptionType": "pro"}))
    sys.exit(0)
json.dump(sys.argv, open(%r, "w"))
where = sys.argv[sys.argv.index("--add-dir") + 1]
def say(one): print(json.dumps(one), flush=True)
%s
""" % (argv_path, body)))
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP)
    return path


@pytest.fixture
def repository(tmp_path):
    """A repository on ``release`` with a ``feature`` branch to merge."""
    where = tmp_path / "repo"
    where.mkdir()
    _git(where, "init", "-q", "-b", "release")
    _git(where, "config", "user.email", "t@example.invalid")
    _git(where, "config", "user.name", "T")
    (where / "a.txt").write_text("one\n")
    _git(where, "add", "a.txt")
    _git(where, "commit", "-q", "-m", "base")
    _git(where, "switch", "-q", "-c", "feature")
    (where / "b.txt").write_text("two\n")
    _git(where, "add", "b.txt")
    _git(where, "commit", "-q", "-m", "feature")
    _git(where, "switch", "-q", "release")
    return where


@pytest.fixture
def merge(tmp_path, repository):
    argv_path = str(tmp_path / "argv.json")

    def run(body=MERGED, **settings):
        binary = _claude(tmp_path, body, argv_path)
        workspace = create("20261008-000000-m", str(tmp_path / "runs"))
        context = RunContext(CycleRun(id="r", cycle_id="c", workspace=workspace),
                             None, workspace)
        step = CycleStep(id="merge", plugin="agent.merge", settings=dict({
            "model": "opus", "task": "Merge feature into release.",
            "directory": str(repository), "claude": binary}, **settings))
        result = registry.get("agent.merge").execute(context, step)
        argv = json.load(open(argv_path)) if os.path.exists(argv_path) else []
        return result, argv

    return run


def test_it_merges_and_reports_every_command_it_ran(merge, repository):
    result, _argv = merge()
    assert result.ok, result.message
    assert result.outputs["commands"] == ["git status --short",
                                          "git merge --no-ff --no-edit feature"]
    assert result.outputs["command_count"] == 2
    assert result.outputs["branch"] == "release"
    assert result.outputs["merging"] == 0
    assert (repository / "b.txt").exists(), "the merge really happened"
    assert "Merge branch 'feature'" in _git(repository, "log", "-1", "--format=%s")


def test_the_cli_is_given_local_git_and_refused_the_rest_by_name(merge):
    _result, argv = merge()
    allowed = argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]
    refused = argv[argv.index("--disallowedTools") + 1:]
    assert "Bash(git merge --no-ff:*)" in allowed
    assert "Bash(git push:*)" not in allowed
    for one in ("Bash(git push:*)", "Bash(git fetch:*)", "Bash(git remote:*)",
                "Bash(git config:*)"):
        assert one in refused
    # Each rule is an argument of its own: joined, the CLI splits it at the space.
    assert "Bash(git merge --no-ff:*)" in argv


def test_a_plain_bash_is_never_among_its_tools():
    assert "Bash" not in GIT_ALLOWED
    assert all(one.startswith("Bash(git ") for one in GIT_ALLOWED + GIT_REFUSED)


def test_a_repository_with_a_remote_is_refused_before_the_agent_starts(
        merge, repository, tmp_path):
    _git(repository, "remote", "add", "origin", str(tmp_path / "elsewhere"))
    result, argv = merge()
    assert not result.ok
    assert "has a remote (origin)" in result.message
    assert argv == [], "the agent was never started"


def test_a_remote_added_during_the_step_fails_it(merge):
    result, _argv = merge(body=ADDS_A_REMOTE)
    assert not result.ok
    assert "gained a remote (sneaky)" in result.message


def test_a_directory_that_is_not_a_repository_is_refused(merge, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    result, _argv = merge(directory=str(plain))
    assert not result.ok and "Not a git repository" in result.message


def test_a_merge_left_unfinished_is_reported(merge, repository):
    body = """
import subprocess
open(os.path.join(where, ".git", "MERGE_HEAD"), "w").write("x\\n")
say({"type": "result", "subtype": "success", "is_error": False,
     "num_turns": 1, "result": "stopped"})
"""
    result, _argv = merge(body=body)
    assert result.outputs["merging"] == 1
    assert "merge left unfinished" in result.message


def test_the_commands_are_read_off_the_stream():
    line = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "git log -1"}},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "x"}}]}})
    assert agent_stream.commands(line) == ["git log -1"]
    assert agent_stream.commands("not json") == []


def test_its_metadata_survives_the_wire():
    json.dumps(registry.get("agent.merge").metadata.to_dict())


TRIED_A_SCRIPT = """
import subprocess
say({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "t1", "name": "Bash",
     "input": {"command": "python3 - <<'EOF'\\nprint(1)\\nEOF"}}]}})
say({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "t2", "name": "Bash",
     "input": {"command": "git merge --no-ff --no-edit feature"}}]}})
subprocess.run(["git", "merge", "--no-ff", "--no-edit", "feature"], cwd=where,
               capture_output=True)
say({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
     "result": "Merged.", "permission_denials": [
         {"tool_name": "Bash", "tool_use_id": "t1",
          "tool_input": {"command": "python3 - <<'EOF'"}}]})
"""


def test_a_refused_attempt_is_not_reported_as_a_command_that_ran(merge):
    """A review read every attempt as something done: an agent stopped from
    running a script, which then resolved the conflict by editing, was
    reported as having rewritten the files with a script."""
    result, _argv = merge(body=TRIED_A_SCRIPT)
    assert result.ok, result.message
    assert result.outputs["commands"] == ["git merge --no-ff --no-edit feature"]
    assert result.outputs["command_count"] == 1
    assert result.outputs["refused"] == ["python3 - <<'EOF'\nprint(1)\nEOF"]
