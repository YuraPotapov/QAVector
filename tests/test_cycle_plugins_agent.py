"""Agent boundary tests need neither a framework nor a model/API connection."""

import json
from pathlib import Path

import pytest

from cycle import registry
from cycle.context import RunContext
from cycle.plugins.agent import AgentReview
from cycle.plugins.agent_worker import repository_tools, validate_review
from domain.cycle import CycleRun, CycleStep

REVIEW = {"summary": "Reviewed the supplied changes", "issues": [],
          "recommendations": ["Check the deployment configuration"], "risk": "low"}


@pytest.fixture
def context(tmp_path):
    return RunContext(CycleRun(id="r", cycle_id="review"), None, str(tmp_path))


def review_step(**changes):
    return CycleStep(id="review", plugin="agent.review", settings=dict({
        "python": "/agent env/bin/python", "model": "provider/model",
        "task": "Review the changes", "framework": "crewai",
    }, **changes))


def test_review_passes_structured_inputs_and_keeps_credentials_out_of_request(context, monkeypatch):
    context.environment["REVIEW_API_KEY"] = "test-secret-value"
    log = Path(context.workspace) / "test.log"
    log.write_text("one failed check\n")
    seen = {}

    def worker(ctx, step, argv, cwd, name, env=None):
        seen.update(json.loads(Path(argv[-2]).read_text()))
        assert "test-secret-value" not in Path(argv[-2]).read_text()
        assert argv[0] == "/agent env/bin/python"
        assert ctx.environment["REVIEW_API_KEY"] == "test-secret-value"
        Path(argv[-1]).write_text(json.dumps(REVIEW))
        return registry.succeeded()

    monkeypatch.setattr("cycle.plugins.agent.run_process", worker)
    result = AgentReview().execute(context, review_step(
        api_key_env="REVIEW_API_KEY", inputs={"exit_code": 1}, files=["test.log"]))
    assert result.ok, result.message
    assert seen["inputs"] == {"exit_code": 1}
    assert seen["files"][0]["content"] == "one failed check\n"
    assert result.outputs["summary"] == REVIEW["summary"]
    assert result.outputs["risk"] == "low"
    assert json.loads((Path(context.workspace) / result.outputs["report_path"]).read_text()) == REVIEW
    assert not list((Path(context.workspace) / "steps" / "review").glob("request-*.json"))


@pytest.mark.parametrize("reply", ["not json", "[]", '{"summary":"missing fields"}'])
def test_zero_exit_with_invalid_review_is_a_failed_step(context, monkeypatch, reply):
    def worker(_ctx, _step, argv, *_args, **_kwargs):
        Path(argv[-1]).write_text(reply)
        return registry.succeeded()
    monkeypatch.setattr("cycle.plugins.agent.run_process", worker)
    result = AgentReview().execute(context, review_step())
    assert not result.ok
    assert "valid review" in result.message


def test_failed_worker_cannot_reuse_an_earlier_review(context, monkeypatch):
    directory = Path(context.step_dir("review"))
    (directory / "review.json").write_text(json.dumps(REVIEW))
    monkeypatch.setattr("cycle.plugins.agent.run_process",
                        lambda *args, **kwargs: registry.failed("provider unavailable"))
    result = AgentReview().execute(context, review_step())
    assert not result.ok
    assert "report_path" not in result.outputs
    assert result.message == "provider unavailable"


def test_missing_named_api_key_is_reported_before_starting_worker(context):
    result = AgentReview().execute(context, review_step(api_key_env="NO_REVIEW_KEY"))
    assert not result.ok
    assert "NO_REVIEW_KEY is not set" in result.message


def test_log_files_outside_workspace_are_refused(context):
    result = AgentReview().execute(context, review_step(files=["../outside.log"]))
    assert not result.ok
    assert "outside the run workspace" in result.message


def test_cancelled_review_does_not_start_worker(context):
    context.cancel.set("stop")
    assert not AgentReview().execute(context, review_step()).ok
    assert not (Path(context.workspace) / "steps").exists()


def test_framework_configuration_errors_are_reported():
    plugin = AgentReview()
    assert plugin.problems(review_step(framework="autogen").settings)
    assert plugin.problems(review_step(framework="unknown").settings)
    assert plugin.problems(review_step(model_options={"api_key": "inline"}).settings)
    assert not plugin.problems(review_step(framework="autogen", model_client="provider.Client").settings)


def test_repository_tools_read_numbered_lines_and_refuse_traversal(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "code.py").write_text("first\nsecond\nthird\n")
    (tmp_path / "outside.txt").write_text("outside")
    list_files, read_file = repository_tools(str(root))
    assert "code.py" in list_files()
    assert read_file("code.py", 2, 1) == "2: second"
    assert "outside the selected repository" in read_file("../outside.txt")


def test_repository_tools_refuse_symlinks_outside_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    try:
        (root / "link").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")
    _list_files, read_file = repository_tools(str(root))
    assert "outside the selected repository" in read_file("link")


def test_review_validation_rejects_invalid_issue_locations():
    with pytest.raises(ValueError, match="line"):
        validate_review(dict(REVIEW, issues=[{"description": "Bad input", "severity": "high", "line": -1}]))


# ------------------------------------------------------- the claude_cli backend
# The backend that needs no API key. It runs the Claude Code CLI, which signs in
# through a browser and keeps its credentials to itself - so what is worth
# testing is the boundary: what argv gets built, what is refused before anything
# starts, and what is made of the reply.

import json as _json
import os as _os
import stat as _stat
import sys as _sys

import pytest as _pytest

from cycle.plugins.agent import CLI_TOOLS, _claude_cli_prompt
from cycle.plugins.agent_worker import extract_json as _extract_json

_posix_only = _pytest.mark.skipif(_os.name == "nt",
                                  reason="the fake CLI here is a script")


def _fake_claude(directory, body, name="claude"):
    path = _os.path.join(str(directory), name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!%s\n%s\n" % (_sys.executable, body))
    _os.chmod(path, _os.stat(path).st_mode | _stat.S_IEXEC | _stat.S_IXGRP)
    return path


def _replying(directory, review, cost=0.05):
    """A CLI that answers `auth status` signed in and `-p` with a review."""
    return _fake_claude(directory, """
import json, sys
if "auth" in sys.argv:
    print(json.dumps({"loggedIn": True, "email": "a@example.invalid",
                      "subscriptionType": "pro"}))
    sys.exit(0)
sys.argv_seen = sys.argv
with open(%r, "w") as handle:
    json.dump(sys.argv, handle)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": json.dumps(%r), "total_cost_usd": %r}))
""" % (_os.path.join(str(directory), "argv.json"), review, cost))


_REVIEW = {"summary": "Two problems in the login path.", "risk": "high",
           "issues": [{"severity": "high", "description": "MD5 without a salt",
                       "file": "login.py", "line": 7}],
           "recommendations": ["Use a slow KDF"]}


def _cli_step(**settings):
    from domain.cycle import CycleStep

    base = {"framework": "claude_cli", "model": "sonnet", "task": "Review it"}
    base.update(settings)
    return CycleStep(id="review", plugin="agent.review", settings=base)


def test_cli_report_write_retry_uses_saved_response_without_another_call(context, tmp_path, monkeypatch):
    import builtins
    from cycle.plugins import agent_run

    binary = _replying(tmp_path, _REVIEW)
    step = _cli_step(claude=binary)
    calls = []
    process = agent_run.run_process

    def count(*args, **kwargs):
        calls.append(1)
        return process(*args, **kwargs)

    monkeypatch.setattr(agent_run, "run_process", count)
    real_open = builtins.open

    def fail_report(path, mode="r", *args, **kwargs):
        if str(path).endswith("/steps/review/review.json") and mode == "w":
            raise OSError("report directory unavailable")
        return real_open(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", fail_report)
        with pytest.raises(OSError, match="report directory"):
            AgentReview().execute(context, step)
    context.run.resume_count += 1
    restored = AgentReview().execute(context, step)
    assert restored.ok, restored.message
    assert calls == [1]
    assert restored.outputs["summary"] == _REVIEW["summary"]
    assert restored.outputs["cost_usd"] == 0.05
    assert json.loads(Path(context.workspace, restored.outputs["report_path"]).read_text()) == _REVIEW


# ------------------------------------------------------------------ validation
def test_the_cli_backend_needs_no_interpreter_and_no_key():
    """Which is the whole reason it exists."""
    from cycle import registry

    plugin = registry.get("agent.review")
    assert plugin.problems({"framework": "claude_cli", "model": "sonnet",
                            "task": "Review it"}) == []


def test_the_cli_backend_refuses_settings_that_belong_to_the_others():
    """Quietly ignoring them would leave somebody believing a key is in use."""
    from cycle import registry

    plugin = registry.get("agent.review")
    for key in ("python", "api_key_env", "model_client"):
        found = plugin.problems({"framework": "claude_cli", "model": "sonnet",
                                 "task": "Review it", key: "something"})
        assert any(key in one for one in found), key


def test_an_effort_level_the_cli_would_ignore_is_refused():
    """The CLI warns and carries on at its default, which is the problem.

    A typo left to the binary would run at a level nobody chose, pass, and
    leave the report claiming an effort the step never used.
    """
    from cycle import registry

    plugin = registry.get("agent.review")
    found = plugin.problems({"framework": "claude_cli", "model": "sonnet",
                             "task": "Review it", "effort": "ultra"})
    assert any("Effort" in one and "ultra" in one for one in found), found


def test_effort_is_refused_for_a_backend_that_has_no_such_thing():
    from cycle import registry

    plugin = registry.get("agent.review")
    found = plugin.problems({"framework": "crewai", "model": "x", "task": "y",
                             "python": "/agent/bin/python", "effort": "high"})
    assert any("effort is only used by claude_cli" in one for one in found), found


def test_a_cycle_written_before_effort_existed_still_validates():
    from cycle import registry

    plugin = registry.get("agent.review")
    assert plugin.problems({"framework": "claude_cli", "model": "sonnet",
                            "task": "Review it", "effort": ""}) == []


def test_the_worker_backends_still_need_an_interpreter():
    from cycle import registry

    plugin = registry.get("agent.review")
    found = plugin.problems({"framework": "crewai", "model": "x", "task": "y"})
    assert any("Agent Python" in one for one in found)


def test_the_cli_backend_is_the_default():
    """The one that works without anybody setting anything up first."""
    from cycle import registry

    spec = [one for one in registry.get("agent.review").metadata.inputs
            if one.key == "framework"][0]
    assert spec.default == "claude_cli"
    assert spec.options[0] == "claude_cli"


# ---------------------------------------------------------------- the prompt
def test_the_prompt_carries_the_task_and_says_what_shape_to_answer_in():
    prompt = _claude_cli_prompt("Find the security holes", {}, [], "")
    assert "Find the security holes" in prompt
    assert '"risk"' in prompt and '"issues"' in prompt and '"severity"' in prompt


def test_the_prompt_names_the_checkout_when_there_is_one():
    prompt = _claude_cli_prompt("t", {}, [], "/runs/demo/src")
    assert "/runs/demo/src" in prompt


def test_earlier_results_reach_the_prompt():
    prompt = _claude_cli_prompt("t", {"tests": "3 failed"}, [], "")
    assert "3 failed" in prompt


def test_a_file_reaches_the_prompt_and_says_when_it_was_cut():
    prompt = _claude_cli_prompt("t", {}, [{"path": "a.py", "content": "x = 1",
                                           "truncated": True}], "")
    assert "a.py" in prompt and "x = 1" in prompt and "truncated" in prompt


# ------------------------------------------------------- reading the answer
def test_a_bare_object_is_read():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_a_fenced_object_is_read():
    """Asking for bare JSON mostly works; a fence is the ordinary failure, and
    refusing a whole review over one would make the backend feel broken."""
    assert _extract_json('Here:\n```json\n{"a": 1}\n```\n') == {"a": 1}


def test_a_brace_inside_a_string_does_not_end_the_object():
    assert _extract_json('{"s": "a } brace"}') == {"s": "a } brace"}


def test_a_reply_with_no_object_is_refused():
    with _pytest.raises(ValueError):
        _extract_json("I could not review this.")


def test_an_unclosed_object_is_refused():
    with _pytest.raises(ValueError):
        _extract_json('{"a": 1')


# --------------------------------------------------------------- running it
@_posix_only
def test_a_review_comes_back_as_outputs_and_a_report(tmp_path, monkeypatch):
    from cycle import registry
    from cycle.context import RunContext
    from cycle.workspace import create
    from domain.cycle import CycleRun

    path = create("20260918-000000-demo", str(tmp_path))
    context = RunContext(CycleRun(id="r", cycle_id="demo", workspace=path),
                         None, path)
    binary = _replying(tmp_path, _REVIEW)

    result = registry.get("agent.review").execute(
        context, _cli_step(claude=binary))

    assert result.ok, result.message
    assert result.outputs["risk"] == "high"
    assert len(result.outputs["issues"]) == 1
    assert result.outputs["cost_usd"] == 0.05
    assert result.outputs["report_path"].endswith("review.json")
    # The runner's own logs are kept too; the review is added to them.
    assert "review" in [one.name for one in result.artifacts]
    written = _json.load(open(_os.path.join(path, result.outputs["report_path"])))
    assert written["summary"] == _REVIEW["summary"]


@_posix_only
def test_the_agent_is_only_allowed_to_read(tmp_path):
    """A step asked to review something must not be able to change it."""
    from cycle import registry
    from cycle.context import RunContext
    from cycle.workspace import create
    from domain.cycle import CycleRun

    path = create("20260918-000000-demo", str(tmp_path))
    context = RunContext(CycleRun(id="r", cycle_id="demo", workspace=path),
                         None, path)
    registry.get("agent.review").execute(
        context, _cli_step(claude=_replying(tmp_path, _REVIEW)))

    argv = _json.load(open(_os.path.join(str(tmp_path), "argv.json")))
    assert "--allowedTools" in argv
    allowed = argv[argv.index("--allowedTools") + 1].split()
    assert set(allowed) == set(CLI_TOOLS)
    assert "Write" not in allowed and "Bash" not in allowed


@_posix_only
def test_what_the_step_asked_for_reaches_the_command_line(tmp_path):
    from cycle import registry
    from cycle.context import RunContext
    from cycle.workspace import create
    from domain.cycle import CycleRun

    path = create("20260918-000000-demo", str(tmp_path))
    context = RunContext(CycleRun(id="r", cycle_id="demo", workspace=path),
                         None, path)
    registry.get("agent.review").execute(
        context, _cli_step(claude=_replying(tmp_path, _REVIEW),
                           model="opus", max_iterations=5))

    argv = _json.load(open(_os.path.join(str(tmp_path), "argv.json")))
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--max-turns") + 1] == "5"
    # stream-json, so the step can show its turns as they happen rather than
    # printing one wall of JSON when it is already over.
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv


@_posix_only
def test_the_effort_level_reaches_the_command_line(tmp_path):
    from cycle import registry
    from cycle.context import RunContext
    from cycle.workspace import create
    from domain.cycle import CycleRun

    path = create("20260918-000000-demo", str(tmp_path))
    context = RunContext(CycleRun(id="r", cycle_id="demo", workspace=path),
                         None, path)
    registry.get("agent.review").execute(
        context, _cli_step(claude=_replying(tmp_path, _REVIEW), effort="high"))

    argv = _json.load(open(_os.path.join(str(tmp_path), "argv.json")))
    assert argv[argv.index("--effort") + 1] == "high"


@_posix_only
def test_no_effort_level_leaves_the_cli_to_its_own_configuration(tmp_path):
    """Rather than this step picking one on everybody's behalf."""
    from cycle import registry
    from cycle.context import RunContext
    from cycle.workspace import create
    from domain.cycle import CycleRun

    path = create("20260918-000000-demo", str(tmp_path))
    context = RunContext(CycleRun(id="r", cycle_id="demo", workspace=path),
                         None, path)
    registry.get("agent.review").execute(
        context, _cli_step(claude=_replying(tmp_path, _REVIEW)))

    argv = _json.load(open(_os.path.join(str(tmp_path), "argv.json")))
    assert "--effort" not in argv


@_posix_only
def test_a_cli_that_is_not_signed_in_stops_before_anything_runs(tmp_path):
    """And says the one command that fixes it."""
    from cycle import registry
    from cycle.context import RunContext
    from cycle.workspace import create
    from domain.cycle import CycleRun

    path = create("20260918-000000-demo", str(tmp_path))
    context = RunContext(CycleRun(id="r", cycle_id="demo", workspace=path),
                         None, path)
    binary = _fake_claude(tmp_path, "import json\n"
                                    "print(json.dumps({'loggedIn': False}))")

    result = registry.get("agent.review").execute(context, _cli_step(claude=binary))
    assert not result.ok
    assert "auth login" in result.message


@_posix_only
def test_a_reply_that_is_not_a_review_fails_the_step_rather_than_the_run(tmp_path):
    from cycle import registry
    from cycle.context import RunContext
    from cycle.workspace import create
    from domain.cycle import CycleRun

    path = create("20260918-000000-demo", str(tmp_path))
    context = RunContext(CycleRun(id="r", cycle_id="demo", workspace=path),
                         None, path)
    result = registry.get("agent.review").execute(
        context, _cli_step(claude=_replying(tmp_path, {"summary": "no risk key"})))

    assert not result.ok
    assert "valid review" in result.message


# ------------------------------------------------------------- setting it up
def test_the_plugin_declares_how_it_is_set_up():
    """So the Inspector offers it without knowing what any of it means, and a
    plugin added later gets the same for free."""
    from cycle import registry

    actions = registry.get("agent.review").metadata.actions
    assert [one.key for one in actions] == ["sign_in_status", "sign_in"]
    assert [one.kind for one in actions] == ["status", "command"]


@_posix_only
def test_asking_whether_it_is_signed_in(tmp_path):
    from cycle import registry

    plugin = registry.get("agent.review")
    answer = plugin.run_action("sign_in_status", {
        "framework": "claude_cli", "claude": _replying(tmp_path, _REVIEW)})

    assert answer["ok"]
    assert "Signed in" in answer["summary"]


@_posix_only
def test_signing_in_hands_back_the_command_to_run(tmp_path):
    from cycle import registry

    answer = registry.get("agent.review").run_action("sign_in", {
        "framework": "claude_cli", "claude": _replying(tmp_path, _REVIEW)})

    assert answer["ok"]
    assert answer["argv"][1:3] == ["auth", "login"]


def test_the_other_backends_say_why_signing_in_would_not_help():
    """They read a key from the environment - which is what claude_cli avoids."""
    from cycle import registry

    plugin = registry.get("agent.review")
    answer = plugin.run_action("sign_in", {"framework": "crewai"})

    assert not answer["ok"] and answer["argv"] == []
    assert "claude_cli" in answer["detail"]


def test_an_action_nobody_declared_is_answered_not_raised():
    from cycle import registry

    answer = registry.get("agent.review").run_action("nonsense", {})
    assert not answer["ok"] and "Unknown action" in answer["summary"]


# ------------------------------------------------------ counting the findings
def test_a_review_says_how_many_it_found():
    """`${...}` walks mappings and cannot measure a list, so "did the review
    find anything" is unaskable from a cycle file without these."""
    from cycle.plugins.agent import counts

    review = {"issues": [{"severity": "high", "description": "a"},
                         {"severity": "low", "description": "b"},
                         {"severity": "medium", "description": "c"}]}
    assert counts(review) == {"issue_count": 3, "blocking_count": 2}


def test_a_clean_review_counts_to_zero_rather_than_going_quiet():
    from cycle.plugins.agent import counts

    assert counts({"issues": []}) == {"issue_count": 0, "blocking_count": 0}
    assert counts({}) == {"issue_count": 0, "blocking_count": 0}


def test_blocking_means_the_same_two_severities_it_means_elsewhere():
    """A commit refused by one step and allowed by another, over the same
    finding, would be the worst kind of disagreement."""
    from cycle.plugins.agent import BLOCKING
    from cycle.plugins.agent_implement import BLOCKING as IMPLEMENT_BLOCKING

    assert set(BLOCKING) == set(IMPLEMENT_BLOCKING)


def test_the_counts_are_declared_outputs():
    from cycle import registry

    names = [one.key for one in registry.get("agent.review").metadata.outputs]
    assert "issue_count" in names and "blocking_count" in names


# ------------------------------------------------- judging how hard a task is
def test_the_complexity_scale_is_the_effort_scale():
    """A review's judgement is handed straight to ``effort``, so a level one
    side knows and the other does not would fail the step it was meant for."""
    from cycle.plugins.agent_run import EFFORT_LEVELS
    from cycle.plugins.agent_worker import COMPLEXITIES

    assert COMPLEXITIES == EFFORT_LEVELS


def test_a_review_asked_to_judge_complexity_must_answer_it():
    with pytest.raises(ValueError, match="complexity"):
        validate_review(dict(REVIEW), complexity=True)
    with pytest.raises(ValueError, match="complexity"):
        validate_review(dict(REVIEW, complexity="huge"), complexity=True)
    assert validate_review(dict(REVIEW, complexity="xhigh"),
                           complexity=True)["complexity"] == "xhigh"


def test_a_complexity_nobody_asked_for_is_dropped():
    assert "complexity" not in validate_review(dict(REVIEW, complexity="low"))


def test_the_prompt_asks_for_complexity_only_when_the_step_does():
    plain = _claude_cli_prompt("Review it", {}, [], "")
    judged = _claude_cli_prompt("Review it", {}, [], "", complexity=True)
    assert '"complexity"' not in plain
    assert '"complexity": "low" | "medium" | "high" | "xhigh" | "max"' in judged
    assert "- xhigh:" in judged


def test_an_assessment_nobody_offers_is_refused():
    from cycle import registry

    found = registry.get("agent.review").problems(
        {"framework": "claude_cli", "model": "sonnet", "task": "Review it",
         "assess": "difficulty"})
    assert any("difficulty" in one for one in found), found


@_posix_only
def test_a_judged_complexity_comes_back_as_an_output(tmp_path):
    from cycle import registry
    from cycle.context import RunContext
    from cycle.workspace import create
    from domain.cycle import CycleRun

    path = create("20260924-000000-demo", str(tmp_path))
    context = RunContext(CycleRun(id="r", cycle_id="demo", workspace=path),
                         None, path)
    binary = _replying(tmp_path, dict(_REVIEW, complexity="high"))

    result = registry.get("agent.review").execute(
        context, _cli_step(claude=binary, assess="complexity"))

    assert result.ok, result.message
    assert result.outputs["complexity"] == "high"
    assert "high complexity" in result.message
    argv = _json.load(open(_os.path.join(str(tmp_path), "argv.json")))
    assert any("- max:" in one for one in argv), "the scale reached the prompt"


@_posix_only
def test_a_review_that_forgets_to_judge_fails_its_step(tmp_path):
    from cycle import registry
    from cycle.context import RunContext
    from cycle.workspace import create
    from domain.cycle import CycleRun

    path = create("20260924-000000-demo", str(tmp_path))
    context = RunContext(CycleRun(id="r", cycle_id="demo", workspace=path),
                         None, path)
    result = registry.get("agent.review").execute(
        context, _cli_step(claude=_replying(tmp_path, _REVIEW), assess="complexity"))

    assert not result.ok
    assert "complexity" in result.message


def test_the_worker_is_asked_to_judge_and_its_answer_published(context, monkeypatch):
    seen = {}

    def worker(_ctx, _step, argv, *_args, **_kwargs):
        seen.update(json.loads(Path(argv[-2]).read_text()))
        Path(argv[-1]).write_text(json.dumps(dict(REVIEW, complexity="medium")))
        return registry.succeeded()

    monkeypatch.setattr("cycle.plugins.agent.run_process", worker)
    result = AgentReview().execute(context, review_step(assess="complexity"))

    assert result.ok, result.message
    assert seen["assess"] == "complexity"
    assert result.outputs["complexity"] == "medium"


def test_the_worker_prompt_carries_the_scale_only_when_asked():
    from cycle.plugins.agent_worker import prompt_for

    base = {"task": "Review it", "inputs": {}, "files": []}
    assert "complexity" not in prompt_for(base)
    assert "- max:" in prompt_for(dict(base, assess="complexity"))


@pytest.mark.parametrize("plugin", ["agent.review", "agent.implement"])
def test_a_later_step_may_take_its_effort_from_the_judgement(plugin):
    """Checked when the step starts, against the value the reference became."""
    from cycle import registry

    settings = {"framework": "claude_cli", "model": "sonnet", "task": "Do it",
                "directory": "/tmp", "checks": "true",
                "effort": "${steps.reconcile.outputs.complexity}"}
    found = registry.get(plugin).problems(settings)
    assert not any("ffort" in one for one in found), found
    found = registry.get(plugin).problems(dict(settings, effort="huge"))
    assert any("ffort" in one for one in found), found


@_posix_only
def test_a_review_says_the_effort_it_ran_at(tmp_path):
    from cycle import registry
    from cycle.context import RunContext
    from cycle.workspace import create
    from domain.cycle import CycleRun

    path = create("20260924-000000-demo", str(tmp_path))
    context = RunContext(CycleRun(id="r", cycle_id="demo", workspace=path),
                         None, path)
    result = registry.get("agent.review").execute(
        context, _cli_step(claude=_replying(tmp_path, _REVIEW), effort="xhigh"))

    assert result.ok, result.message
    assert result.outputs["effort"] == "xhigh"
    assert result.message.endswith(" - at xhigh effort")


def test_the_effort_goes_on_the_first_line_of_a_longer_message():
    from cycle.plugins.agent_run import with_effort

    assert with_effort("failed\n- one\n- two", "") == \
        "failed - at the CLI's default effort\n- one\n- two"


# --------------------------------------------- naming what the work leaves out
def test_a_review_asked_for_scope_must_name_what_it_leaves_out():
    with pytest.raises(ValueError, match="out_of_scope"):
        validate_review(dict(REVIEW), scope=True)
    with pytest.raises(ValueError, match="out_of_scope"):
        validate_review(dict(REVIEW, out_of_scope=["fine", ""]), scope=True)
    left = ["Non-text codes raise AttributeError today."]
    assert validate_review(dict(REVIEW, out_of_scope=left), scope=True)[
        "out_of_scope"] == left
    assert validate_review(dict(REVIEW, out_of_scope=[]), scope=True)[
        "out_of_scope"] == [], "an empty list is an answer: nothing left out"


def test_an_out_of_scope_list_nobody_asked_for_is_dropped():
    assert "out_of_scope" not in validate_review(dict(REVIEW, out_of_scope=["x"]))


def test_the_prompt_asks_for_scope_only_when_the_step_does():
    assert '"out_of_scope"' not in _claude_cli_prompt("Plan it", {}, [], "")
    asked = _claude_cli_prompt("Plan it", {}, [], "", scope=True)
    assert '"out_of_scope": [' in asked and "decides whether the line is" in asked


@_posix_only
def test_what_a_plan_leaves_out_comes_back_counted(tmp_path):
    from cycle import registry
    from cycle.context import RunContext
    from cycle.workspace import create
    from domain.cycle import CycleRun

    path = create("20260924-000000-demo", str(tmp_path))
    context = RunContext(CycleRun(id="r", cycle_id="demo", workspace=path),
                         None, path)
    left = ["Whitespace around a code is not stripped.",
            "A code cannot be removed once applied."]
    result = registry.get("agent.review").execute(
        context, _cli_step(claude=_replying(tmp_path, dict(_REVIEW, out_of_scope=left)),
                           scope=True))

    assert result.ok, result.message
    assert result.outputs["out_of_scope"] == left
    assert result.outputs["out_of_scope_count"] == 2
    assert "2 left out of scope" in result.message


def test_a_review_without_scope_counts_nothing_left_out(context, monkeypatch):
    def worker(_ctx, _step, argv, *_args, **_kwargs):
        Path(argv[-1]).write_text(json.dumps(REVIEW))
        return registry.succeeded()

    monkeypatch.setattr("cycle.plugins.agent.run_process", worker)
    result = AgentReview().execute(context, review_step())
    assert result.outputs["out_of_scope_count"] == 0
    assert "out_of_scope" not in result.outputs


def test_the_worker_carries_scope_to_its_prompt():
    from cycle.plugins.agent_worker import prompt_for

    base = {"task": "Plan it", "inputs": {}, "files": []}
    assert "out_of_scope" not in prompt_for(base)
    assert "out_of_scope" in prompt_for(dict(base, scope=True))
