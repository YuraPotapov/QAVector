"""command.shell: what it runs, what it reports, and how it stops."""

import os
import threading
import time

import pytest

from cycle import registry
from cycle.context import CancelToken, RunContext
from cycle.plugins.command import TAIL_LINES, ShellCommand
from domain.cycle import CycleRun, CycleStep

posix_only = pytest.mark.skipif(os.name == "nt",
                                reason="the shell lines here are posix")


@pytest.fixture
def context(tmp_path):
    run = CycleRun(id="r", cycle_id="demo", workspace=str(tmp_path))
    return RunContext(run, None, str(tmp_path))


def run_command(context, command, step_id="cmd", **settings):
    settings["command"] = command
    step = CycleStep(id=step_id, plugin="command.shell", settings=settings)
    return ShellCommand().execute(context, step)


# ------------------------------------------------------------------- the basics
@posix_only
def test_a_command_that_works_succeeds(context):
    result = run_command(context, "echo hello")
    assert result.ok
    assert result.outputs["exit_code"] == 0


@posix_only
def test_what_it_printed_comes_back_as_the_tail(context):
    assert run_command(context, "echo hello").outputs["stdout_tail"] == "hello"


@posix_only
def test_a_command_that_fails_fails_and_says_what_it_exited_with(context):
    result = run_command(context, "exit 3")
    assert not result.ok
    assert result.outputs["exit_code"] == 3
    assert "exited 3" in result.message


@posix_only
def test_the_message_quotes_the_last_thing_it_said(context):
    """The end of a failed build is usually the whole of the diagnosis."""
    result = run_command(context, "echo 'cannot find module'; exit 1")
    assert "cannot find module" in result.message


@posix_only
def test_another_exit_code_can_be_the_expected_one(context):
    """grep exits 1 for "found nothing", which is often the wanted answer."""
    assert run_command(context, "exit 1", expect_exit=1).ok
    assert not run_command(context, "exit 0", expect_exit=1).ok


@posix_only
def test_any_exit_code_can_be_accepted_and_decided_on_later(context):
    result = run_command(context, "exit 7", expect_exit=-1)
    assert result.ok
    assert result.outputs["exit_code"] == 7


def test_a_command_that_cannot_be_run_at_all_fails_rather_than_raising(context):
    result = run_command(context, ["/no/such/program"], shell=False)
    assert not result.ok
    assert "cannot run it" in result.message


# ------------------------------------------------------------------------ output
@posix_only
def test_both_streams_are_written_to_files(context, tmp_path):
    run_command(context, "echo out; echo err >&2", step_id="both")
    step_dir = tmp_path / "steps" / "both"
    assert (step_dir / "stdout.log").read_text().strip() == "out"
    assert (step_dir / "stderr.log").read_text().strip() == "err"


@posix_only
def test_the_two_log_files_are_recorded_as_artifacts(context):
    result = run_command(context, "echo hello")
    names = {artifact.name: artifact for artifact in result.artifacts}
    assert set(names) == {"stdout", "stderr"}
    assert names["stdout"].producer == "cmd"
    assert names["stdout"].bytes > 0


@posix_only
def test_an_artifact_path_is_relative_to_the_workspace(context):
    """So the run directory still makes sense on another machine."""
    for artifact in run_command(context, "echo hello").artifacts:
        assert not os.path.isabs(artifact.path)
        assert artifact.path.startswith("steps" + os.sep)


@posix_only
def test_the_tail_is_the_end_of_the_output_not_all_of_it(context, tmp_path):
    result = run_command(context, "seq 1 500", step_id="many")
    tail = result.outputs["stdout_tail"].splitlines()
    assert len(tail) == TAIL_LINES
    assert tail[-1] == "500"
    # All of it is still on disk - the tail is a convenience, not the record.
    written = (tmp_path / "steps" / "many" / "stdout.log").read_text().splitlines()
    assert len(written) == 500


@posix_only
def test_output_reaches_the_observer_while_it_is_running(context):
    seen = []

    class Watcher:
        def step_log(self, step_id, stream, lines):
            seen.append((stream, list(lines)))

    context.observer = Watcher()
    run_command(context, "echo one; echo two; echo bad >&2")

    streams = {stream for stream, _lines in seen}
    assert streams == {"out", "err"}
    said = [line for _stream, lines in seen for line in lines]
    assert "one" in said and "two" in said and "bad" in said


@posix_only
def test_output_arrives_in_batches_rather_than_one_event_per_line(context):
    """events.emit holds one lock per line; a chatty step would block the rest."""
    batches = []

    class Watcher:
        def step_log(self, step_id, stream, lines):
            batches.append(len(lines))

    context.observer = Watcher()
    run_command(context, "seq 1 400")
    assert sum(batches) == 400
    assert max(batches) > 1


@posix_only
def test_a_lot_of_output_does_not_deadlock(context):
    """Reading one pipe at a time hangs as soon as the other one fills."""
    result = run_command(context, "seq 1 20000; seq 1 20000 >&2")
    assert result.ok


@posix_only
def test_output_that_is_not_utf_eight_does_not_break_anything(context):
    result = run_command(context, r"printf '\xff\xfe bad bytes\n'")
    assert result.ok


# ---------------------------------------------------------------- where it runs
@posix_only
def test_a_command_runs_in_the_workspace_by_default(context, tmp_path):
    assert run_command(context, "pwd").outputs["stdout_tail"] == str(
        tmp_path.resolve())


@posix_only
def test_a_relative_directory_is_read_against_the_workspace(context, tmp_path):
    result = run_command(context, "pwd", dir="build")
    assert result.outputs["stdout_tail"] == str((tmp_path / "build").resolve())
    assert (tmp_path / "build").is_dir()


@posix_only
def test_an_absolute_directory_is_used_as_it_is(context, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert run_command(context, "pwd", dir=str(elsewhere)).outputs[
        "stdout_tail"] == str(elsewhere.resolve())


@posix_only
def test_extra_environment_variables_reach_the_command(context):
    result = run_command(context, "echo $CYCLE_MARKER", env={"CYCLE_MARKER": "set"})
    assert result.outputs["stdout_tail"] == "set"


@posix_only
def test_the_command_inherits_the_environment_it_was_given(context):
    context.environment["FROM_THE_RUN"] = "yes"
    assert run_command(context, "echo $FROM_THE_RUN").outputs[
        "stdout_tail"] == "yes"


# -------------------------------------------------------------- how it is written
@posix_only
def test_a_string_goes_through_a_shell_so_pipes_work(context):
    assert run_command(context, "echo one two | wc -w").outputs[
        "stdout_tail"].strip() == "2"


def test_a_list_is_run_directly_whatever_the_shell_setting_says(context):
    """Somebody who wrote a list did so to avoid the shell's quoting."""
    result = run_command(context, ["python3", "-c", "print('from a list')"],
                         shell=True)
    assert result.ok
    assert result.outputs["stdout_tail"] == "from a list"


@posix_only
def test_the_shell_can_be_turned_off_for_a_string(context):
    result = run_command(context, "echo no shell here", shell=False)
    assert result.ok
    assert result.outputs["stdout_tail"] == "no shell here"


# -------------------------------------------------------------------- stopping
@posix_only
def test_a_cancelled_command_stops_promptly_and_says_what_it_knows(context):
    """It reports what happened to the command, not why the run ended.

    The executor prefixes the cancel token's reason when it records the step, so
    a plugin repeating it here would print it twice - "interrupted: interrupted".
    """
    token = CancelToken()
    context.cancel = token
    threading.Timer(0.3, lambda: token.set("the run was stopped")).start()

    started = time.monotonic()
    result = run_command(context, "sleep 30")
    took = time.monotonic() - started

    assert not result.ok
    assert took < 5, "a cancelled command has to stop, not be waited out"
    assert result.message == "the command was signalled and did not finish"
    assert "the run was stopped" not in result.message


@posix_only
def test_a_command_that_ignores_being_asked_nicely_is_killed(context):
    """Cooperative stopping has a floor: it does not mean "cannot be stopped"."""
    token = CancelToken()
    context.cancel = token
    threading.Timer(0.3, token.set).start()

    started = time.monotonic()
    result = run_command(context, 'trap "" TERM; sleep 60')
    took = time.monotonic() - started

    assert not result.ok
    assert took < 20, "the grace period should have expired and killed it"


@posix_only
def test_what_it_printed_before_it_was_stopped_is_still_kept(context, tmp_path):
    token = CancelToken()
    context.cancel = token
    threading.Timer(0.4, token.set).start()

    run_command(context, "echo before; sleep 30", step_id="stopped")
    assert "before" in (tmp_path / "steps" / "stopped" / "stdout.log").read_text()


@posix_only
def test_stopping_reaches_what_the_command_itself_started(context):
    """A make that spawned a compiler, a script that spawned a server."""
    marker = os.path.join(context.workspace, "child-still-running")
    token = CancelToken()
    context.cancel = token
    threading.Timer(0.4, token.set).start()

    run_command(context,
                "(sleep 20; touch %s) & wait" % marker, step_id="group")
    time.sleep(0.5)
    assert not os.path.exists(marker)


# ------------------------------------------------------------------- complaints
def test_a_step_with_no_command_is_complained_about():
    assert ShellCommand().problems({}) == ["Command is required."]


def test_an_empty_list_of_arguments_is_complained_about():
    assert ShellCommand().problems({"command": []})


def test_an_unbalanced_quote_is_complained_about_when_there_is_no_shell():
    """With a shell it is the shell's business; without one it cannot be split."""
    found = ShellCommand().problems({"command": 'echo "unclosed', "shell": False})
    assert found


def test_a_well_formed_step_has_nothing_to_complain_about():
    assert ShellCommand().problems({"command": "echo hi"}) == []
    assert ShellCommand().problems(
        {"command": ["echo", "hi"], "dir": "build", "env": {"A": "1"},
         "shell": False, "expect_exit": 0}) == []


def test_a_misspelt_setting_is_complained_about():
    found = ShellCommand().problems({"command": "x", "comand": "y"})
    assert found and "'comand'" in found[0]


def test_the_plugin_is_in_the_table_and_describes_itself():
    plugin = registry.get("command.shell")
    assert plugin is not None
    assert plugin.metadata.category == registry.ACTION
    assert "process.spawn" in plugin.metadata.permissions
