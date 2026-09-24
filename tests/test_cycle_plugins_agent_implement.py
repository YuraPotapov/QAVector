"""The step that makes a change and proves it.

The property worth defending is the loop itself: that a failed check really
does send the work round again, that what failed reaches the next attempt's
prompt, and that "verified" is never the agent's own word for it.

So the fake `claude` here is not a stub that returns a canned answer - it
writes a real file whose content decides whether the real check command
passes. The first attempt writes something broken, the second writes something
that works, and the test watches the plugin notice the difference. A fake that
could not fail would prove nothing about a loop whose whole job is failing.
"""

import json
import os
import stat
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cycle import bus, registry                                   # noqa: E402
from cycle.context import CancelToken, Cancelled, RunContext      # noqa: E402
from cycle.plugins.agent_implement import BLOCKING, _tree         # noqa: E402
from cycle.workspace import create                                # noqa: E402
from domain.cycle import CycleRun, CycleStep                      # noqa: E402

#: What the fake writes on each attempt: the first breaks the check, the second
#: satisfies it. Attempt numbers come from a counter file it keeps itself.
BODIES = ["raise SystemExit('not done yet')", "print('ok')"]

REVIEW_OK = {"summary": "It does what was asked.", "risk": "low",
             "issues": [], "recommendations": []}
REVIEW_BLOCKS = {"summary": "Not quite.", "risk": "high",
                 "issues": [{"severity": "high", "description": "Misses the edge case",
                             "file": "main.py", "line": 3}],
                 "recommendations": ["Handle the empty case"]}
REVIEW_NITS = {"summary": "Fine.", "risk": "low",
               "issues": [{"severity": "low", "description": "Could be tidier",
                           "file": "main.py", "line": 1}],
               "recommendations": []}


def _claude(directory, bodies=None, reviews=None):
    """A fake CLI that edits on a `-p` run and reviews when asked to review.

    It tells the two apart the way the plugin does: a review run is given the
    reading tools and no `--permission-mode`.
    """
    path = os.path.join(str(directory), "claude")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!%s\n%s" % (sys.executable, '''
import json, os, sys
if "auth" in sys.argv:
    print(json.dumps({"loggedIn": True, "email": "a@example.invalid",
                      "subscriptionType": "pro"}))
    sys.exit(0)

state = %r
bodies = %r
reviews = %r
where = sys.argv[sys.argv.index("--add-dir") + 1]
prompt = sys.argv[sys.argv.index("-p") + 1]
reviewing = "--permission-mode" not in sys.argv
open(state + ".argv", "a").write(json.dumps(
    {"reviewing": reviewing, "argv": sys.argv}) + "\\n")

def say(one): print(json.dumps(one), flush=True)
say({"type": "system", "subtype": "init", "model": "claude-sonnet-5"})

turn = 0
if os.path.exists(state):
    turn = int(open(state).read() or 0)
open(state, "w").write(str(turn + 1))

if reviewing:
    seen = [t for t in open(state + ".seen").read().splitlines()] if os.path.exists(state + ".seen") else []
    index = min(len(seen), len(reviews) - 1)
    open(state + ".seen", "a").write("r\\n")
    say({"type": "result", "subtype": "success", "is_error": False,
         "result": json.dumps(reviews[index]), "total_cost_usd": 0.01})
    sys.exit(0)

open(os.path.join(where, "prompt-%%d.txt" %% turn, ), "w").write(prompt)
body = bodies[min(turn, len(bodies) - 1)]
open(os.path.join(where, "main.py"), "w").write(body + "\\n")
say({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "Write",
     "input": {"file_path": os.path.join(where, "main.py")}}]}})
say({"type": "result", "subtype": "success", "is_error": False,
     "result": "Wrote main.py (pass %%d)." %% turn, "total_cost_usd": 0.02})
''' % (os.path.join(str(directory), "turns"),
       list(bodies if bodies is not None else BODIES),
       list(reviews if reviews is not None else [REVIEW_OK]))))
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP)
    return path


@pytest.fixture
def work(tmp_path):
    """A directory to work in, a fake CLI, and a way to run the step."""
    project = tmp_path / "project"
    project.mkdir()

    def run(bodies=None, reviews=None, cancel=None, git=False, earlier=None,
            later=None, **settings):
        if git:
            subprocess.run(["git", "init", "-q"], cwd=str(project), check=True,
                           capture_output=True)
        binary = _claude(tmp_path, bodies, reviews)
        recorder = bus.Recorder()
        workspace = create("20260919-000000-i", str(tmp_path / "runs"))
        context = RunContext(CycleRun(id="r", cycle_id="c",
                                      workspace=workspace),
                             None, workspace, observer=recorder,
                             cancel=cancel or CancelToken())
        if earlier is not None:
            from domain.cycle import StepRun
            context.earlier = {"impl": ("20260919-000000-old", StepRun(
                "impl", plugin="agent.implement", status="failed", outputs=earlier))}
            if later is not None:
                # A step after this one, in the same earlier run, and what it
                # said about the work.
                from cycle.model import parse_cycle
                context.cycle = parse_cycle({"id": "c", "steps": [
                    {"id": "impl", "plugin": "agent.implement"},
                    {"id": "accept", "plugin": "agent.review", "needs": ["impl"]}]}, "c")
                context.earlier["accept"] = ("20260919-000000-old", StepRun(
                    "accept", plugin="agent.review", status="success", outputs=later))
        step = CycleStep(id="impl", plugin="agent.implement", settings=dict({
            "model": "sonnet", "task": "Make it print ok",
            "directory": str(project), "claude": binary,
            "checks": "%s main.py" % sys.executable,
            "review": False}, **settings))
        result = registry.get("agent.implement").execute(context, step)
        return result, project, recorder

    return run


def _stages(recorder):
    return [call[1]["stage"] for call in recorder.calls
            if call[0] == "step_stage"]


def _titles(recorder):
    return [one["title"] for one in _stages(recorder)]


# ------------------------------------------------------------------- the loop
def test_a_failed_check_sends_the_work_round_again(work):
    """The whole point of the step. The first pass writes something that does
    not run; the second writes something that does."""
    result, project, _rec = work()

    assert result.ok, result.message
    assert result.outputs["verified"] is True
    assert result.outputs["attempts"] == 2
    assert (project / "main.py").read_text().strip() == "print('ok')"


def test_what_failed_reaches_the_next_attempt(work):
    """A retry that is not told what went wrong is just another guess."""
    _result, project, _rec = work()

    second = (project / "prompt-1.txt").read_text()
    assert "did not hold" in second
    assert "not done yet" in second, "the check's own output has to be in it"


def test_the_first_attempt_is_not_told_about_a_failure_that_has_not_happened(work):
    _result, project, _rec = work()
    assert "did not hold" not in (project / "prompt-0.txt").read_text()


def test_the_checks_are_named_in_the_prompt_so_the_agent_writes_for_them(work):
    _result, project, _rec = work()
    assert "main.py" in (project / "prompt-0.txt").read_text()


def _argvs(project):
    path = project.parent / "turns.argv"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_no_turn_limit_is_passed_unless_the_step_sets_one(work):
    """A turn count cut a six-file change off halfway; the timeout bounds it."""
    _result, project, _rec = work(bodies=["print('ok')"], review=True)
    runs = _argvs(project)
    assert {one["reviewing"] for one in runs} == {False, True}
    assert all("--max-turns" not in one["argv"] for one in runs)


def test_a_turn_limit_the_step_sets_reaches_the_cli(work):
    _result, project, _rec = work(bodies=["print('ok')"], max_iterations=80)
    argv = _argvs(project)[0]["argv"]
    assert argv[argv.index("--max-turns") + 1] == "80"


def test_work_that_holds_the_first_time_does_not_go_round_twice(work):
    result, _project, _rec = work(bodies=["print('ok')"])
    assert result.outputs["attempts"] == 1
    assert result.outputs["verified"] is True


def test_a_budget_that_runs_out_reports_not_verified_rather_than_passing(work):
    result, _project, _rec = work(bodies=["raise SystemExit('never works')"],
                                  max_attempts=2)

    assert not result.ok
    assert result.outputs["verified"] is False
    assert result.outputs["attempts"] == 2
    assert "not verified" in result.message


def test_a_step_that_gave_up_still_reports_what_it_changed(work):
    """It edited whatever it edited. Leaving that out would be the worst
    possible answer."""
    result, _project, _rec = work(bodies=["raise SystemExit('never')"],
                                  max_attempts=2)
    assert result.outputs["files_changed"] == ["main.py"]
    assert result.outputs["changed"] == 1


def test_what_was_still_failing_is_an_output(work):
    result, _project, _rec = work(bodies=["raise SystemExit('never works')"],
                                  max_attempts=1)
    assert "never works" in result.outputs["failed_check"]


def test_every_check_has_to_pass_not_just_the_first(work):
    result, _project, _rec = work(
        bodies=["print('ok')"], max_attempts=1,
        checks="%s main.py\n%s -c 'raise SystemExit(1)'" % (sys.executable,
                                                            sys.executable))
    assert result.outputs["verified"] is False


def test_the_cost_of_every_attempt_is_added_up(work):
    result, _project, _rec = work()
    assert result.outputs["cost_usd"] == pytest.approx(0.04)   # two passes


# ---------------------------------------------------------------- the review
def test_a_blocking_review_sends_the_work_back_even_though_the_checks_passed(work):
    """Tests passing and a reviewer objecting is exactly the case the gate is
    for - and the case the spec calls out."""
    result, _project, recorder = work(
        bodies=["print('ok')"], review=True, max_attempts=2,
        reviews=[REVIEW_BLOCKS, REVIEW_OK])

    assert result.outputs["verified"] is True
    assert result.outputs["attempts"] == 2, "the review sent it round again"


def test_a_review_that_keeps_objecting_ends_unverified(work):
    result, _project, _rec = work(bodies=["print('ok')"], review=True,
                                  max_attempts=2, reviews=[REVIEW_BLOCKS])
    assert result.outputs["verified"] is False
    assert "Misses the edge case" in result.outputs["failed_check"]
    assert "main.py:3" in result.outputs["failed_check"]


def test_a_low_severity_note_is_recorded_and_not_looped_on(work):
    """Spending the budget polishing something that already works is the
    opposite of what the budget is for."""
    result, _project, _rec = work(bodies=["print('ok')"], review=True,
                                  reviews=[REVIEW_NITS])

    assert result.outputs["verified"] is True
    assert result.outputs["attempts"] == 1
    assert result.outputs["findings"][0]["severity"] == "low"


def test_only_high_and_medium_block():
    assert set(BLOCKING) == {"high", "medium"}


def test_the_review_is_skipped_when_the_step_says_so(work):
    result, _project, recorder = work(bodies=["print('ok')"], review=False)
    assert result.outputs["verified"] is True
    assert not [one for one in _stages(recorder) if one["kind"] == "review"]


def _runs(tmp_path):
    """Every time the fake CLI was started, and with what."""
    path = tmp_path / "turns.argv"
    return [json.loads(line) for line in
            path.read_text().splitlines() if line.strip()]


def test_the_review_thinks_as_hard_as_the_work_it_is_judging(tmp_path, work):
    """A reviewer asked to think less than the writer did is not a check on it."""
    work(bodies=["print('ok')"], review=True, reviews=[REVIEW_OK],
         effort="high")

    runs = _runs(tmp_path)
    assert [one["reviewing"] for one in runs] == [False, True]
    for one in runs:
        assert one["argv"][one["argv"].index("--effort") + 1] == "high"


def test_no_effort_level_leaves_the_cli_to_its_own_configuration(tmp_path, work):
    work(bodies=["print('ok')"], review=True, reviews=[REVIEW_OK])
    assert all("--effort" not in one["argv"] for one in _runs(tmp_path))


def test_an_effort_level_the_cli_would_ignore_is_refused(work):
    """It warns and runs at its default, so a typo has to stop the step."""
    result, _project, _rec = work(effort="highest")
    assert not result.ok
    assert "highest" in result.message


def test_a_review_that_will_not_come_back_in_shape_is_not_an_approval(work):
    result, _project, _rec = work(bodies=["print('ok')"], review=True,
                                  max_attempts=1,
                                  reviews=[{"nonsense": True}])
    assert result.outputs["verified"] is False
    assert "required shape" in result.outputs["failed_check"]


# -------------------------------------------------------------- what it shows
def test_each_attempt_opens_a_row_somebody_can_read(work):
    _result, _project, recorder = work()
    assert "Attempt 1 of 3" in _titles(recorder)
    assert "Attempt 2 of 3" in _titles(recorder)


def test_each_check_is_a_row_with_its_verdict(work):
    _result, _project, recorder = work()
    checks = [one for one in _stages(recorder)
              if one["title"].startswith("Check: ")]
    assert [one["status"] for one in checks] == ["failed", "done"]


def test_the_review_is_a_row_carrying_the_parsed_verdict(work):
    _result, _project, recorder = work(bodies=["print('ok')"], review=True,
                                       reviews=[REVIEW_OK])
    rows = [one for one in _stages(recorder) if one["kind"] == "review"]
    assert rows and rows[0]["body"]["risk"] == "low"


# ------------------------------------------------------------------- the tree
def test_the_tree_is_the_state_that_passed(tmp_path, work):
    """What git.commit checks against. Without it "verified" and "committed"
    are two descriptions that are only usually the same thing."""
    result, project, _rec = work(git=True)

    assert len(result.outputs["tree"]) >= 40
    # The same hash git itself gives for what is in the directory now.
    subprocess.run(["git", "add", "-A"], cwd=str(project), check=True,
                   capture_output=True,
                   env=dict(os.environ, GIT_INDEX_FILE=str(tmp_path / "idx")))
    mine = subprocess.run(["git", "write-tree"], cwd=str(project),
                          capture_output=True, text=True,
                          env=dict(os.environ, GIT_INDEX_FILE=str(tmp_path / "idx")))
    assert result.outputs["tree"] == mine.stdout.strip()


def _committed(project, **files):
    """A repository whose working tree is exactly its one commit."""
    for name, text in files.items():
        (project / name).write_text(text)
    for command in (["git", "init", "-q"], ["git", "add", "-A"],
                    ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                     "commit", "-q", "-m", "start"]):
        subprocess.run(command, cwd=str(project), check=True, capture_output=True)


def test_checks_that_fail_on_the_untouched_checkout_stop_before_any_attempt(tmp_path, work):
    """pytest pointed at modules it cannot import failed four paid attempts."""
    _committed(tmp_path / "project", **{"main.py": "raise SystemExit('broken already')\n"})
    result, _project, recorder = work(bodies=["print('ok')"])

    assert not result.ok
    assert result.outputs["attempts"] == 0
    assert "fail before any change" in result.message
    assert "broken already" in result.message
    assert not (tmp_path / "turns").exists(), "the CLI must not have been run"
    assert "Check before the change: %s main.py" % sys.executable in _titles(recorder)


def test_a_failure_unchanged_by_an_attempt_stops_the_loop(tmp_path, work):
    """The checkout had edits, so the first failure might have been the work's;
    failing identically afterwards says the checks cannot see it."""
    project = tmp_path / "project"
    _committed(project, **{"main.py": "print('ok')\n",
                           "check.py": "raise SystemExit('cannot import odoo')\n"})
    (project / "main.py").write_text("print('an earlier attempt')\n")
    result, _project, _rec = work(bodies=["print('ok')"], max_attempts=4,
                                  checks="%s check.py" % sys.executable)

    assert not result.ok
    assert result.outputs["attempts"] == 1
    assert "failed the same way before the change and after attempt 1" in result.message


def test_checks_that_pass_before_the_change_are_no_reason_to_stop(tmp_path, work):
    _committed(tmp_path / "project", **{"main.py": "print('ok')\n"})
    result, _project, _rec = work(bodies=["print('ok')"])
    assert result.ok, result.message
    assert result.outputs["attempts"] == 1


def _left_behind(project, text):
    """A committed checkout with an earlier run's edit on top; its tree hash."""
    _committed(project, **{"main.py": "raise SystemExit('not started')\n"})
    (project / "main.py").write_text(text)
    from cycle.plugins.agent_implement import _tree
    return _tree(str(project))


def test_earlier_work_that_now_passes_is_verified_without_another_attempt(tmp_path, work):
    """An attempt that did its work and failed on a check since fixed."""
    tree = _left_behind(tmp_path / "project", "print('ok')\n")
    result, _project, recorder = work(earlier={"tree": tree, "summary": "did it",
                                               "files_changed": ["main.py"]})

    assert result.ok, result.message
    assert result.outputs["verified"] is True
    assert result.outputs["attempts"] == 0
    assert result.outputs["files_changed"] == ["main.py"]
    assert "20260919-000000-old" in result.message
    assert not (tmp_path / "turns").exists(), "nothing was paid for again"
    assert "Continuing the work of 20260919-000000-old" in _titles(recorder)


def test_earlier_work_that_still_fails_is_carried_on_not_started_over(tmp_path, work):
    tree = _left_behind(tmp_path / "project", "raise SystemExit('half done')\n")
    result, project, _rec = work(bodies=["print('ok')"],
                                 earlier={"tree": tree, "files_changed": ["main.py"]})

    assert result.ok, result.message
    assert result.outputs["attempts"] == 1
    first = (project / "prompt-0.txt").read_text()
    assert "did not hold" in first and "half done" in first


def test_work_a_later_step_returned_goes_back_for_rework(tmp_path, work):
    """Checks passing does not overturn an acceptance review that said no."""
    tree = _left_behind(tmp_path / "project", "print('ok')\n")
    result, project, recorder = work(
        bodies=["print('ok')"], earlier={"tree": tree, "files_changed": ["main.py"]},
        later={"issues": [
            {"severity": "medium", "description": "No uk_UA translation for the header",
             "file": "main.py", "line": 1},
            {"severity": "low", "description": "Could be tidier"}]})

    assert result.ok, result.message
    assert result.outputs["attempts"] == 1, "the agent was sent back to it"
    first = (project / "prompt-0.txt").read_text()
    assert "returned the work" in first
    assert "[accept] No uk_UA translation for the header" in first
    assert "Could be tidier" not in first, "a low note does not send work back"
    assert "Returned for rework: 1 finding(s)" in _titles(recorder)


def test_only_low_notes_from_a_later_step_do_not_send_work_back(tmp_path, work):
    tree = _left_behind(tmp_path / "project", "print('ok')\n")
    result, _project, _rec = work(earlier={"tree": tree},
                                  later={"issues": [{"severity": "low",
                                                     "description": "nit"}]})
    assert result.outputs["attempts"] == 0
    assert not (tmp_path / "turns").exists()


def test_an_unfinished_merge_is_named_in_the_prompt(tmp_path, work):
    """Base-branch work merged under the task's edits: the agent is told where
    the markers are and that upstream is what it builds on."""
    project = tmp_path / "project"
    _committed(project, **{"main.py": "print('start')\n", "seq.xml": "one\n"})
    git = lambda *a: subprocess.run(["git", "-c", "user.name=t",
                                     "-c", "user.email=t@example.invalid"] + list(a),
                                    cwd=str(project), capture_output=True)
    (project / "seq.xml").write_text("task edit\n")
    git("stash")
    (project / "seq.xml").write_text("upstream edit\n")
    git("commit", "-qam", "upstream")
    git("stash", "pop")
    assert "<<<<<<<" in (project / "seq.xml").read_text()

    _result, project, _rec = work(bodies=["print('ok')"])
    first = (project / "prompt-0.txt").read_text()
    assert "# An unfinished merge" in first
    assert "    seq.xml" in first
    assert "Updated upstream" in first


def test_a_checkout_with_edits_that_fail_starts_from_that_failure(tmp_path, work):
    project = tmp_path / "project"
    _committed(project, **{"main.py": "print('ok')\n"})
    (project / "main.py").write_text("raise SystemExit('merged and broken')\n")
    _result, project, _rec = work(bodies=["print('ok')"])
    first = (project / "prompt-0.txt").read_text()
    assert "did not hold" in first and "merged and broken" in first


def test_work_from_a_run_further_back_is_found_by_its_tree(tmp_path, work):
    """The run just before stopped at a gate; the one before it did the work."""
    tree = _left_behind(tmp_path / "project", "print('ok')\n")
    older = tmp_path / "runs" / "20260918-000000-i"
    older.mkdir(parents=True)
    (older / "metadata.json").write_text(json.dumps({
        "id": "20260918-000000-i", "cycle_id": "c",
        "steps": {"impl": {"outputs": {"tree": tree, "files_changed": ["main.py"]}}}}))
    result, _project, _rec = work(earlier={})

    assert result.outputs["attempts"] == 0
    assert "20260918-000000-i" in result.message
    assert not (tmp_path / "turns").exists()


def test_a_checkout_changed_since_the_earlier_run_is_not_continued(tmp_path, work):
    tree = _left_behind(tmp_path / "project", "print('ok')\n")
    (tmp_path / "project" / "main.py").write_text("print('edited by hand')\n")
    result, _project, recorder = work(bodies=["print('ok')"], earlier={"tree": tree})

    assert result.outputs["attempts"] == 1
    assert not any(t.startswith("Continuing") for t in _titles(recorder))


def test_a_directory_that_is_not_a_repository_simply_has_no_tree(work):
    result, _project, _rec = work(bodies=["print('ok')"])
    # The fixture's project has no .git, so this is the ordinary case.
    assert result.outputs["tree"] == ""


def test_the_hash_is_taken_without_touching_anybody_s_staging(tmp_path):
    """Somebody with a half-staged change must not find it staged for them
    because a step wanted a hash."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (["init", "-q"], ["config", "user.email", "a@example.invalid"],
                 ["config", "user.name", "T"]):
        subprocess.run(["git"] + argv, cwd=str(repo), check=True,
                       capture_output=True)
    (repo / "kept.txt").write_text("one\n")
    (repo / "staged.txt").write_text("two\n")
    subprocess.run(["git", "add", "staged.txt"], cwd=str(repo), check=True,
                   capture_output=True)
    before = subprocess.run(["git", "diff", "--name-only", "--cached"],
                            cwd=str(repo), capture_output=True, text=True).stdout

    assert _tree(str(repo)), "it should still produce a hash"

    after = subprocess.run(["git", "diff", "--name-only", "--cached"],
                           cwd=str(repo), capture_output=True, text=True).stdout
    assert after == before == "staged.txt\n"


# ------------------------------------------------------------------ refusals
def test_no_checks_is_allowed_by_the_settings():
    plugin = registry.get("agent.implement")
    problems = plugin.problems({"model": "s", "task": "t", "directory": "d"})
    assert not any("Checks" in one for one in problems)


def test_with_no_checks_the_review_is_the_verdict_and_it_says_so(work):
    result, _project, _recorder = work(checks="", review=True, reviews=[REVIEW_OK])
    assert result.ok, result.message
    assert result.outputs["verified"] is True
    assert result.outputs["checked"] is False
    assert "by the review only; no checks were run" in result.message


def test_with_no_checks_a_blocking_review_still_stops_it(work):
    result, _project, _recorder = work(checks="", review=True, max_attempts=1,
                                       reviews=[REVIEW_BLOCKS])
    assert not result.ok and result.outputs["verified"] is False


def test_with_no_checks_and_no_review_nothing_could_verify_it(work):
    result, _project, _recorder = work(checks="", review=False)
    assert not result.ok
    assert "needs checks or the review" in result.message


def test_a_step_with_checks_says_they_ran(work):
    result, _project, _recorder = work()
    assert result.outputs["checked"] is True


def test_a_directory_that_is_not_there_is_refused_before_the_cli_starts(work):
    result, _project, recorder = work(directory="/no/such/place")
    assert not result.ok
    assert "does not exist" in result.message
    assert _stages(recorder) == []


def test_an_impossible_budget_is_refused():
    plugin = registry.get("agent.implement")
    base = {"model": "s", "task": "t", "directory": "d", "checks": "true"}
    for count in (0, -1, 50, 1.5, True):
        assert plugin.problems(dict(base, max_attempts=count))


def test_a_reference_is_left_for_the_run_to_resolve():
    plugin = registry.get("agent.implement")
    assert plugin.problems({"model": "s", "task": "t", "directory": "d",
                            "checks": "${vars.checks}",
                            "max_attempts": "${vars.n}"}) == []


def test_a_stop_between_attempts_is_obeyed(work):
    token = CancelToken()
    token.set("stopped")
    with pytest.raises(Cancelled):
        work(cancel=token)


def test_it_offers_the_same_sign_in_as_the_other_agent_steps():
    keys = [one.key for one in registry.get("agent.implement").metadata.actions]
    assert keys == [one.key for one in registry.get("agent.review").metadata.actions]


def test_its_metadata_survives_the_wire():
    json.dumps(registry.get("agent.implement").metadata.to_dict())
