"""The step that lets an agent change files.

Two properties are worth defending here and neither is about the model. The
first is the tool list: this step may write, and it may not run commands, and
both halves of that have to stay true or the cycle file stops describing what
the step can do. The second is the record: what it changed is read off the
CLI's own stream, and a step that failed halfway has still edited whatever it
edited, so the record has to survive the failure.

Every test runs a fake `claude` that replays a stream and really does write the
files it says it wrote - so "did anything change" is answered by looking at the
directory rather than by trusting the plugin's own bookkeeping.
"""

import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cycle import bus, registry                                   # noqa: E402
from cycle.context import RunContext                              # noqa: E402
from cycle.plugins.agent_edit import EDIT_TOOLS                   # noqa: E402
from cycle.workspace import create                                # noqa: E402
from domain.cycle import CycleRun, CycleStep                      # noqa: E402

WROTE = """
say({"type": "system", "subtype": "init", "model": "claude-sonnet-5"})
say({"type": "assistant", "message": {"content": [
    {"type": "thinking", "thinking": "I will rewrite it."}]}})
for name in ("main.py", "README.md", "main.py"):
    open(os.path.join(where, name), "w").write("new " + name)
    say({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Write",
         "input": {"file_path": os.path.join(where, name)}}]}})
say({"type": "result", "subtype": "success", "is_error": False,
     "num_turns": 3, "total_cost_usd": 0.031, "result": "Rewrote main.py."})
"""


def _claude(directory, body, argv_path):
    """A fake CLI: signed in, and whatever ``body`` says on a -p run."""
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
def edit(tmp_path):
    """Runs agent.edit against a fake CLI; hands back everything it produced."""
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "main.py").write_text("print('old')\n", encoding="utf-8")
    argv_path = str(tmp_path / "argv.json")

    def run(body=WROTE, **settings):
        binary = _claude(tmp_path, body, argv_path)
        recorder = bus.Recorder()
        workspace = create("20260919-000000-e", str(tmp_path / "runs"))
        context = RunContext(CycleRun(id="r", cycle_id="c",
                                      workspace=workspace),
                             None, workspace, observer=recorder)
        step = CycleStep(id="fix", plugin="agent.edit", settings=dict({
            "model": "sonnet", "task": "Rewrite it",
            "directory": str(repository), "claude": binary}, **settings))
        result = registry.get("agent.edit").execute(context, step)
        argv = json.load(open(argv_path)) if os.path.exists(argv_path) else []
        return result, argv, repository, recorder

    return run


# -------------------------------------------------------------- what it may do
def test_the_agent_may_write_and_the_files_really_change(edit):
    result, _argv, repository, _recorder = edit()
    assert result.ok, result.message
    assert (repository / "main.py").read_text() == "new main.py"
    assert (repository / "README.md").exists()


def test_it_is_given_the_tools_to_edit(edit):
    _result, argv, _repository, _recorder = edit()
    given = argv[argv.index("--allowedTools") + 1].split()
    assert "Edit" in given and "Write" in given
    assert given == list(EDIT_TOOLS)


def test_it_is_never_given_a_shell(edit):
    """A step that needs to run something has command.shell, which says so in
    the cycle file. An editing agent with a shell makes "what did this step do"
    unanswerable from the file."""
    _result, argv, _repository, _recorder = edit()
    assert "Bash" not in argv[argv.index("--allowedTools") + 1].split()


def test_edits_are_approved_as_they_are_made_because_nobody_is_watching(edit):
    _result, argv, _repository, _recorder = edit()
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    # Not the mode that hands over everything else as well.
    assert "bypassPermissions" not in argv
    assert "--dangerously-skip-permissions" not in argv


def test_the_directory_is_the_one_the_step_named(edit):
    _result, argv, repository, _recorder = edit()
    assert argv[argv.index("--add-dir") + 1] == str(repository)


def test_the_effort_level_is_asked_for_only_when_the_step_names_one(edit):
    _result, argv, _repository, _recorder = edit(effort="max")
    assert argv[argv.index("--effort") + 1] == "max"

    _result, argv, _repository, _recorder = edit()
    assert "--effort" not in argv


def test_an_effort_level_the_cli_would_ignore_is_refused_before_anything_starts(edit):
    """It warns and runs at its default, so a typo has to stop here instead."""
    result, argv, _repository, _recorder = edit(effort="maximum")
    assert not result.ok
    assert "maximum" in result.message
    assert argv == []                       # the CLI was never started


# ------------------------------------------------------------- what it reports
def test_what_it_changed_is_named_once_each_and_relative_to_the_directory(edit):
    """main.py is written twice in the stream and is one changed file."""
    result, _argv, _repository, _recorder = edit()
    assert result.outputs["files_changed"] == ["main.py", "README.md"]
    assert result.outputs["changed"] == 2


def test_what_it_said_it_did_is_an_output_a_later_step_can_read(edit):
    result, _argv, _repository, _recorder = edit()
    assert result.outputs["summary"] == "Rewrote main.py."
    assert result.outputs["cost_usd"] == 0.031


def test_the_message_says_how_much_changed(edit):
    result, _argv, _repository, _recorder = edit()
    assert result.message == "agent.edit: 2 files changed - at the CLI's default effort"


def test_one_file_is_not_called_files(edit):
    body = """
say({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "Edit",
     "input": {"file_path": os.path.join(where, "main.py")}}]}})
say({"type": "result", "subtype": "success", "is_error": False, "result": "ok"})
"""
    result, _argv, _repository, _recorder = edit(body)
    assert result.message == "agent.edit: 1 file changed - at the CLI's default effort"


def test_its_turns_are_shown_as_stages_like_a_review_s_are(edit):
    _result, _argv, _repository, recorder = edit()
    titles = [call[1]["stage"]["title"] for call in recorder.calls
              if call[0] == "step_stage"]
    assert "Thinking" in titles
    assert "Write main.py" in titles


def test_a_step_that_failed_halfway_still_reports_what_it_changed(edit):
    """It has edited whatever it edited. Leaving that out of the record would
    be the worst possible answer."""
    body = """
open(os.path.join(where, "main.py"), "w").write("half done")
say({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "Write",
     "input": {"file_path": os.path.join(where, "main.py")}}]}})
sys.exit(3)
"""
    result, _argv, repository, _recorder = edit(body)
    assert not result.ok
    assert result.outputs["files_changed"] == ["main.py"]
    assert (repository / "main.py").read_text() == "half done"


def test_a_run_that_never_reached_a_result_fails_rather_than_claiming_success(edit):
    body = 'say({"type": "assistant", "message": {"content": []}})'
    result, _argv, _repository, _recorder = edit(body)
    assert not result.ok
    assert "without a result" in result.message


def test_a_cli_that_refused_says_so(edit):
    body = ('say({"type": "result", "subtype": "error_max_turns", '
            '"is_error": True, "result": "ran out of turns"})')
    result, _argv, _repository, _recorder = edit(body)
    assert not result.ok
    assert "ran out of turns" in result.message


# ------------------------------------------------------------------- refusals
def test_a_directory_that_is_not_there_is_refused_rather_than_created(edit):
    """An agent let loose on a directory that was meant to be a checkout and is
    in fact empty would work very hard on nothing."""
    result, argv, _repository, _recorder = edit(directory="/no/such/place")
    assert not result.ok
    assert "does not exist" in result.message
    assert argv == []                       # the CLI was never started


def test_the_task_and_the_directory_are_both_required():
    plugin = registry.get("agent.edit")
    problems = plugin.problems({"model": "sonnet"})
    assert any("task" in one.lower() for one in problems)
    assert any("directory" in one.lower() for one in problems)


def test_an_impossible_turn_limit_is_refused_before_anything_starts():
    plugin = registry.get("agent.edit")
    for count in (0, -1, 500, 1.5, True):
        assert plugin.problems({"model": "s", "task": "t", "directory": "d",
                                "max_iterations": count})


def test_a_reference_is_left_for_the_run_to_resolve():
    """${steps.x.outputs.y} is not a number yet and must not be judged as one."""
    plugin = registry.get("agent.edit")
    assert plugin.problems({"model": "s", "task": "t", "directory": "d",
                            "max_iterations": "${vars.turns}"}) == []


# ------------------------------------------------------------------ the shape
def test_it_is_its_own_plugin_rather_than_a_setting_on_the_review():
    """So a cycle file says which it is, in the one place somebody looks."""
    review = registry.get("agent.review")
    assert registry.get("agent.edit") is not review
    assert "edit" not in [one.key for one in review.metadata.inputs]


def test_it_offers_the_same_sign_in_as_the_review_does():
    keys = [one.key for one in registry.get("agent.edit").metadata.actions]
    assert keys == [one.key for one in registry.get("agent.review").metadata.actions]


def test_its_metadata_survives_the_wire():
    json.dumps(registry.get("agent.edit").metadata.to_dict())


def test_the_effort_it_ran_at_is_said_and_published(edit):
    """The CLI never reports the level back, and one taken from a review's
    judgement is otherwise visible nowhere in the run."""
    result, argv, _repository, _recorder = edit(effort="low")
    assert argv[argv.index("--effort") + 1] == "low"
    assert result.message.endswith(" - at low effort")
    assert result.outputs["effort"] == "low"
