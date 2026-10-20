"""Prepare task is a fresh, bounded workflow with durable evidence."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from cycle import bus, checkpoints, cyclefile, executor, memory, model, prepare_claim, registry, sessions
from cycle import run as records
from cycle.context import RunContext
from cycle.plugins.task_brief import TaskBrief
from domain.cycle import CycleRun, CycleStep


class Fake(registry.CyclePlugin):
    def __init__(self, plugin_id, action=None, reusable=True):
        self.metadata = registry.PluginMetadata(id=plugin_id, name=plugin_id, reusable=reusable)
        self.action = action or (lambda c, s: registry.succeeded())
        self.calls = []

    def execute(self, context, step):
        self.calls.append(step.id)
        return self.action(context, step)


class Plugins:
    def __init__(self, **entries):
        self.entries = entries

    def get(self, name):
        return self.entries.get(name) or registry.get(name)


def node(name, plugin="command.shell", **extra):
    return dict(id=name, plugin=plugin, **extra)


def action_cycle(nodes, selected, result="brief", action="prepare_task"):
    return model.parse_cycle({"id": "demo", "actions": {action: {
        "title": "Prepare task", "steps": selected, "result": result}}, "steps": nodes}, "demo")


def test_actions_round_trip_and_do_not_change_step_fingerprints():
    cycle = action_cycle([node("brief", "task.brief")], ["brief"])
    rendered = cyclefile.render(model.to_document(cycle))
    import yaml
    parsed = model.parse_cycle(yaml.safe_load(rendered), "demo")
    assert parsed.actions == cycle.actions == model.to_graph(cycle)["actions"]
    before = checkpoints.signatures(cycle, {}, registry)
    cycle.actions = {}
    assert checkpoints.signatures(cycle, {}, registry) == before


@pytest.mark.parametrize("plugin", ["agent.merge", "agent.review", "agent.implement", "jira.transition", "memory.remember", "scenario.run"])
def test_prepare_cannot_select_paid_work_external_write_or_attempt_counters(plugin):
    cycle = action_cycle([node("bad", plugin), node("brief", "task.brief")], ["bad", "brief"])
    assert any("not a preparation operation" in p for p in model.action_problems(cycle))


def test_actions_reject_missing_input_producers_even_at_conditional_joins():
    cycle = action_cycle([node("other"), node("brief", "task.brief", needs=["other"],
        **{"if": "true", "with": {"issues": "${steps.other.outputs.issues}"}})], ["brief"])
    assert any("reads unselected step other" in p for p in model.action_problems(cycle))


@pytest.mark.parametrize("definition", [{"steps": []}, {"steps": "brief"}, {"steps": ["brief"], "result": []},
    {"steps": ["brief", "brief"], "result": "brief"}, {"steps": ["gone"], "result": "gone"}])
def test_bad_action_definitions_are_problems_rather_than_validator_exceptions(definition):
    cycle = model.parse_cycle({"id": "demo", "actions": {"prepare_task": definition},
                               "steps": [node("brief", "task.brief")]}, "demo")
    assert model.problems(cycle)


def fake_preparation():
    read = Fake("jira.issues", lambda c, s: registry.succeeded(count=1, issues=[{"key": "QA-1"}]))
    brief = Fake("task.brief", lambda c, s: registry.succeeded(brief={"ready": True, "external_effects": []}))
    unselected = Fake("approval.gate", reusable=False)
    budget = Fake("memory.remember", lambda c, s: (memory.bump("task", "attempts", path=c.memory_path), registry.succeeded())[1])
    cycle = action_cycle([node("read", "jira.issues"), node("brief", "task.brief", needs=["read"]),
        node("gate", "approval.gate"), node("budget", "memory.remember", needs=["brief"]),
        node("implement", "agent.implement", needs=["budget"])], ["read", "brief"])
    plugins = Plugins(**{"jira.issues": read, "task.brief": brief, "approval.gate": unselected,
                         "memory.remember": budget, "agent.implement": Fake("agent.implement")})
    return cycle, plugins, read, brief, unselected, budget


def test_prepare_runs_no_future_gate_agent_or_budget_and_publishes_brief(tmp_path):
    cycle, plugins, read, brief, gate, budget = fake_preparation()
    seen = []
    observer = bus.Tee([records.Persister(root=str(tmp_path)), bus.EventObserver(
        sink=lambda kind, **f: seen.append(dict(kind=kind, **f)))])
    run = executor.run_cycle(cycle, str(tmp_path / "run"), run_id="run", registry=plugins,
        action="prepare_task", observer=observer, memory_path=str(tmp_path / "memory.json"))
    assert run.outcome == "task_prepared" and run.scope == "partial"
    assert run.coverage == {"completed": 2, "selected": 2, "total": 5}
    assert gate.calls == budget.calls == plugins.get("agent.implement").calls == []
    assert memory.recall("task", str(tmp_path / "memory.json")) == {}
    assert seen[-1]["outcome"] == "task_prepared"
    assert seen[-1]["task_brief"]["ready"] is True
    session = sessions.page(str(tmp_path), memory_path=str(tmp_path / "memory.json"))["sessions"][0]
    assert session["task_brief"]["ready"] is True and session["action_id"] == "prepare_task"
    again = executor.run_cycle(cycle, str(tmp_path / "again"), registry=plugins, action="prepare_task")
    assert again.outcome == "task_prepared" and read.calls == ["read", "read"]
    assert all(not s.source_run for s in again.steps.values())


def test_resume_of_failed_prepare_stays_inside_its_original_selection(tmp_path):
    cycle, plugins, read, brief, gate, budget = fake_preparation()
    brief.action = lambda c, s: registry.failed("temporary local writer error")
    executor.run_cycle(cycle, str(tmp_path), registry=plugins, action="prepare_task")
    brief.action = lambda c, s: registry.succeeded(brief={"ready": True})
    final = executor.run_cycle(cycle, str(tmp_path), registry=plugins, resume=True)
    assert final.scope == "resume" and final.outcome == "task_prepared"
    assert final.selected_steps == ["read", "brief"]
    assert read.calls == ["read"] and brief.calls == ["brief", "brief"]
    assert gate.calls == budget.calls == plugins.get("agent.implement").calls == []


def test_resume_refuses_changed_action_selection(tmp_path):
    cycle, plugins, read, brief, gate, budget = fake_preparation()
    brief.action = lambda c, s: registry.failed("local failure")
    executor.run_cycle(cycle, str(tmp_path), registry=plugins, action="prepare_task")
    cycle.steps.append(model.parse_cycle({"steps": [node("extra")]}, "extra").steps[0])
    cycle.actions["prepare_task"]["steps"].append("extra")
    with pytest.raises(checkpoints.CheckpointError, match="selection changed"):
        executor.run_cycle(cycle, str(tmp_path), registry=plugins, resume=True)
    assert brief.calls == ["brief"]


def test_no_task_found_does_not_claim_a_prepared_workspace(tmp_path):
    cycle = action_cycle([node("read", "jira.issues"), node("brief", "task.brief", needs=["read"], **{"if": "false"})], ["read", "brief"])
    read = Fake("jira.issues", lambda c, s: registry.succeeded(count=0))
    run = executor.run_cycle(cycle, str(tmp_path), registry=Plugins(**{"jira.issues": read}), action="prepare_task")
    assert run.outcome == "no_work_found" and not run.task_brief


def git(directory, *args):
    return subprocess.run(["git", "-C", str(directory), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    directory = tmp_path / "repo"
    directory.mkdir()
    git(directory, "init", "-q", "-b", "main")
    git(directory, "config", "user.name", "Test")
    git(directory, "config", "user.email", "test@example.invalid")
    (directory / "file").write_text("before")
    git(directory, "add", "file")
    git(directory, "commit", "-qm", "base")
    git(directory, "update-ref", "refs/remotes/origin/production-ecs", "HEAD")
    git(directory, "switch", "-qc", "QA-1")
    return directory


@pytest.mark.parametrize("existed", [False, True])
def test_real_task_brief_records_paths_sha_recreation_and_no_git_write(repo, tmp_path, existed):
    (repo / ".git/qavector-prepare.json").write_text(json.dumps({"existed_before": existed}))
    before = git(repo, "rev-parse", "HEAD")
    run = CycleRun("run", "demo", workspace=str(tmp_path / "run"), action_id="prepare_task")
    context = RunContext(run, None, run.workspace)
    settings = {"issues": [{"key": "QA-1", "summary": "Fix <login>", "status": "To Do"}],
                "directory": str(repo), "source": str(repo), "base": "origin/production-ecs"}
    result = TaskBrief().execute(context, CycleStep("brief", "task.brief", settings=settings))
    brief = result.outputs["brief"]
    assert result.ok and brief["ready"]
    assert brief["git"]["head"] == brief["git"]["base_commit"] == before
    assert brief["workspace"]["operation"] == ("recreated" if existed else "created")
    assert brief["external_effects"] == [] and brief["effects_verification"] == "declared"
    assert git(repo, "rev-parse", "HEAD") == before and git(repo, "status", "--porcelain") == ""
    assert "Fix &lt;login&gt;" in Path(context.workspace, result.outputs["path"]).read_text()


def test_unmerged_base_is_captured_as_a_blocked_brief_without_an_agent(repo, tmp_path):
    git(repo, "switch", "-q", "main")
    (repo / "file").write_text("base moved")
    git(repo, "commit", "-qam", "move base")
    git(repo, "update-ref", "refs/remotes/origin/production-ecs", "HEAD")
    git(repo, "switch", "-q", "QA-1")
    before = git(repo, "rev-parse", "HEAD")
    run = CycleRun("r", "demo", workspace=str(tmp_path / "r"), action_id="prepare_task")
    result = TaskBrief().execute(RunContext(run, None, run.workspace), CycleStep("brief", "task.brief", settings={
        "issues": [{"key": "QA-1"}], "directory": str(repo), "source": str(repo), "base": "origin/production-ecs"}))
    assert not result.ok and not result.outputs["brief"]["git"]["contains_base"]
    assert result.artifacts and git(repo, "rev-parse", "HEAD") == before


def test_missing_source_checkout_cannot_produce_a_prepared_snapshot(repo, tmp_path):
    run = CycleRun("r", "demo", workspace=str(tmp_path / "r"), action_id="prepare_task")
    result = TaskBrief().execute(RunContext(run, None, run.workspace), CycleStep("brief", "task.brief", settings={
        "issues": [{"key": "QA-1"}], "directory": str(repo), "source": str(tmp_path / "missing"), "base": "origin/production-ecs"}))
    assert not result.ok and not result.outputs.get("brief")


def migration_source():
    nodes = [node(name) for name in prepare_claim.PREPARE_STEPS if name != "task_brief"]
    nodes += [node("base_resolve", "agent.merge"), node("budget", "memory.remember", needs=["admit"]),
              node("baseline"), node("plan", "agent.review"), node("preflight", "check.gate"),
              node("implement", "agent.implement")]
    steps = {s["id"]: s for s in nodes}
    steps["workspace"].update(needs=["budget"], **{"with": {"command": 'WD="$ROOT/$KEY"\ncd "$WD"\necho ready'}})
    steps["base_ready"].update(needs=["base_verdict", "base_resolve"], **{"if": "true"})
    return {"id": "claim_development", "steps": nodes}


def test_claim_upgrade_moves_only_implementation_budget_and_stops_before_agents():
    upgraded = prepare_claim.upgrade(migration_source())
    nodes = {s["id"]: s for s in upgraded["steps"]}
    assert nodes["workspace"]["needs"] == ["admit"]
    assert nodes["budget"]["needs"] == ["preflight", "baseline"]
    assert nodes["implement"]["needs"] == ["budget"]
    assert set(upgraded["actions"]["prepare_task"]["steps"]) == set(prepare_claim.PREPARE_STEPS)
    assert model.action_problems(model.parse_cycle(upgraded, "claim_development")) == []
    assert prepare_claim.upgrade(upgraded) == upgraded


@pytest.mark.parametrize("failure,expected", [("plan", 0), ("preflight", 0), ("", 1)])
def test_only_admitted_implementation_spends_an_attempt(tmp_path, failure, expected):
    raw = migration_source()
    by_id = {s["id"]: s for s in raw["steps"]}
    by_id["plan"]["needs"] = ["facts"]
    by_id["preflight"]["needs"] = ["plan"]
    by_id["base_resolve"]["if"] = "false"
    raw["variables"] = {"work_root": str(tmp_path), "claim_dir": str(tmp_path)}
    cycle = model.parse_cycle(prepare_claim.upgrade(raw), "claim_development")
    shell = Fake("command.shell", lambda c, s: registry.succeeded(stdout_tail="ready"))
    agent = Fake("agent.review", lambda c, s: registry.failed("plan failed") if failure == "plan" else registry.succeeded())
    check = Fake("check.gate", lambda c, s: registry.failed("declined") if failure == "preflight" else registry.succeeded())
    counter = Fake("memory.remember", lambda c, s: (memory.bump("task", "attempts", path=c.memory_path), registry.succeeded())[1])
    shell.action = lambda c, s: registry.succeeded(stdout_tail="ready", issues=[{"key": "QA-1"}], key="QA-1", html_path="task.html")
    brief = Fake("task.brief", lambda c, s: registry.succeeded(brief={"ready": True}))
    plugins = Plugins(**{"command.shell": shell, "agent.review": agent, "check.gate": check,
                         "memory.remember": counter, "task.brief": brief,
                         "agent.implement": Fake("agent.implement"), "agent.merge": Fake("agent.merge")})
    store = str(tmp_path / "memory.json")
    executor.run_cycle(cycle, str(tmp_path / "run"), registry=plugins, memory_path=store)
    assert memory.recall("task", store).get("attempts", 0) == expected
    assert len(plugins.get("agent.implement").calls) == expected


def test_prepared_brief_is_not_lost_when_a_conflict_blocks_the_action(tmp_path):
    cycle = action_cycle([node("base", on_failure="continue"),
        node("brief", "task.brief", needs=["base"], **{"if": "true"})], ["base", "brief"])
    plugins = Plugins(**{"command.shell": Fake("command.shell", lambda c, s: registry.failed("conflict")),
        "task.brief": Fake("task.brief", lambda c, s: registry.failed("base not merged", brief={"ready": False, "git": {"contains_base": False}}))})
    run = executor.run_cycle(cycle, str(tmp_path), registry=plugins, action="prepare_task")
    assert run.outcome == "failed" and run.task_brief["ready"] is False
    assert records.load(str(tmp_path)).task_brief == run.task_brief


def test_cli_runs_only_the_named_action_and_does_not_borrow_from_history(tmp_path):
    root = tmp_path / "cycles"
    root.mkdir()
    forbidden = tmp_path / "unexpected"
    document = {"id": "demo", "actions": {"inspect": {"steps": ["read"]}}, "steps": [
        node("read", **{"with": {"command": "printf ready"}}),
        node("publish", **{"with": {"command": "touch '%s'" % forbidden}})]}
    (root / "demo.yaml").write_text(cyclefile.render(document))
    result = subprocess.run([sys.executable, "session_launcher.py", "--cycle-run=demo", "--cycle-action=inspect",
        "--cycles-dir=" + str(root), "--cycle-runs-dir=" + str(tmp_path / "runs")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not forbidden.exists()
    saved = json.loads(next((tmp_path / "runs").glob("*/metadata.json")).read_text())
    assert saved["action_id"] == "inspect" and saved["selected_steps"] == ["read"]
    assert saved["steps"]["publish"]["reason_code"] == "not_selected"


def real_claim_fixture(repo, tmp_path):
    """The actual Claim preparation scripts, with only Jira replaced by a fake."""
    import yaml
    raw = yaml.safe_load((Path(__file__).parent / "fixtures/cycles/claim_prepare.yaml").read_text())
    cycle = model.parse_cycle(raw, "claim_development")
    git(repo, "branch", "production-ecs", "main")
    git(repo, "branch", "development-ecs", "main")
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "--bare", "--quiet", str(repo), str(remote)], check=True)
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "fetch", "--quiet", "origin")
    jira = Fake("jira.issues", lambda c, s: registry.succeeded(count=1, key="QA-2", title="Fix login",
        issues=[{"key": "QA-2", "summary": "Fix login", "status": "To Do", "assignee": "Tester"}],
        report_path="task.json", html_path="task.html"))
    future = Fake("agent.merge")
    plugins = Plugins(**{"jira.issues": jira, "agent.merge": future,
                         "agent.review": future, "agent.implement": future})
    sandbox = tmp_path / "sandbox/claim-dev"
    values = {"claim_dir": str(repo), "work_root": str(sandbox), "task_key": "QA-2"}
    return cycle, plugins, values, future


def execute_real_prepare(cycle, plugins, values, directory):
    directory.mkdir()
    (directory / "task.json").write_text(json.dumps({"issues": [{"key": "QA-2", "summary": "Fix login", "status": "To Do"}]}))
    return executor.run_cycle(cycle, str(directory), registry=plugins, action="prepare_task", variables_=values,
                              secrets={"jira_token": "fake"}, memory_path=str(directory.parent / "memory.json"))


def test_real_claim_prepare_and_repeat_recreate_only_the_sandbox_without_spending(repo, tmp_path):
    cycle, plugins, values, future = real_claim_fixture(repo, tmp_path)
    before = git(repo, "rev-parse", "HEAD")
    store = str(tmp_path / "memory.json")
    memory.bump("claimdev/QA-2", "attempts", path=store)
    first = execute_real_prepare(cycle, plugins, values, tmp_path / "first")
    assert first.outcome == "task_prepared", {k: (s.status, s.message) for k, s in first.steps.items()}
    assert first.task_brief["workspace"]["operation"] == "created"
    workspace = Path(values["work_root"]) / "QA-2"
    (workspace / "discarded-local-file").write_text("only in the disposable sandbox")
    second = execute_real_prepare(cycle, plugins, values, tmp_path / "second")
    assert second.outcome == "task_prepared"
    assert second.task_brief["workspace"]["operation"] == "recreated"
    assert not (workspace / "discarded-local-file").exists()
    assert memory.recall("claimdev/QA-2", store)["attempts"] == 1
    assert not memory.holder("claimdev/QA-2", store)
    assert git(repo, "rev-parse", "HEAD") == before and not git(repo, "status", "--porcelain")
    assert future.calls == []


def test_real_prepare_records_a_base_conflict_and_never_invokes_the_merger(repo, tmp_path):
    cycle, plugins, values, future = real_claim_fixture(repo, tmp_path)
    git(repo, "switch", "-qc", "QA-2-fix-login", "main")
    (repo / "file").write_text("task change")
    git(repo, "commit", "-qam", "task")
    git(repo, "switch", "-q", "production-ecs")
    (repo / "file").write_text("production change")
    git(repo, "commit", "-qam", "production")
    git(repo, "push", "--quiet", "origin", "production-ecs")
    before = git(repo, "rev-parse", "HEAD")
    run = execute_real_prepare(cycle, plugins, values, tmp_path / "conflict")
    assert run.outcome == "failed"
    assert run.task_brief["ready"] is False and run.task_brief["git"]["base_update"] == "conflict"
    assert run.steps["base_resolve"].reason_code == "not_selected"
    assert git(repo, "rev-parse", "HEAD") == before and future.calls == []
    assert memory.recall("claimdev/QA-2", str(tmp_path / "memory.json")).get("attempts", 0) == 0


def test_migration_is_idempotent_preserves_comments_permissions_and_refuses_stale_apply(tmp_path):
    import stat
    source = tmp_path / "claim_development.yaml"
    text = "# Keep this comment\n" + (Path(__file__).parent / "fixtures/cycles/claim_prepare.yaml").read_text()
    source.write_text(text)
    source.chmod(0o600)
    before, after = prepare_claim.draft(str(source))
    assert before == after == text
    with pytest.raises(SystemExit) as exc:
        prepare_claim.main(["--source", str(source), "--apply", "--expected-digest", "stale"])
    assert exc.value.code == 2 and source.read_text() == text
    prepare_claim.main(["--source", str(source), "--apply", "--expected-digest", cyclefile.text_digest(text)])
    assert source.read_text() == text and stat.S_IMODE(source.stat().st_mode) == 0o600
