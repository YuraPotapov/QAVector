"""An agent step that names an agent connection, and every agent call on the record.

Two things are defended. A step that names a connection runs exactly the
program the connection says - its model and effort where the step left them
blank - and is refused, before anything starts, when it cannot. And every call
an agent step really makes, connection or not, lands in the Agents
integration's record of conversations, saying which cycle and step made it; a
call replayed from its receipt was not made, and is not recorded again.

The agent is a script the test writes: no model is asked anything.
"""

import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cycle import bus, cyclefile, model, registry                   # noqa: E402
from cycle import connections as binding                            # noqa: E402
from cycle.context import RunContext                                # noqa: E402
from cycle.executor import run_cycle                                # noqa: E402
from cycle.workspace import create                                  # noqa: E402
from domain.cycle import FAILED, SUCCESS, CycleRun, CycleStep       # noqa: E402
from integrations.agents import sessions, store                     # noqa: E402

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the stand-in CLI is a script")

AGENT = r'''
import json, os, sys
if "auth" in sys.argv:
    print(json.dumps({"loggedIn": True, "email": "a@example.invalid"}))
    sys.exit(0)
with open(os.environ["FAKE_ARGV"], "a") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\n")
def say(one): print(json.dumps(one), flush=True)
target = os.path.join(os.getcwd(), "notes.md")
say({"type": "system", "subtype": "init", "model": "fake-model", "session_id": "p-1"})
say({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "Writing the notes."},
    {"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": target}}]}})
open(target, "w").write("notes")
say({"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}})
say({"type": "result", "is_error": False, "result": "Done.", "session_id": "p-1",
     "num_turns": 2, "total_cost_usd": 0.02})
'''


@pytest.fixture
def world(tmp_path, monkeypatch):
    class World:
        pass

    one = World()
    one.argv_log = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_ARGV", str(one.argv_log))
    one.binary = str(tmp_path / "bin" / "claude-custom")
    os.makedirs(os.path.dirname(one.binary))
    with open(one.binary, "w", encoding="utf-8") as handle:
        handle.write("#!%s\n%s" % (sys.executable, AGENT))
    os.chmod(one.binary, os.stat(one.binary).st_mode | stat.S_IEXEC)
    one.agents = str(tmp_path / "agents.json")
    one.sessions = str(tmp_path / "agent-sessions")
    one.repo = tmp_path / "repo"
    one.repo.mkdir()
    one.connection, problems = store.upsert(
        {"name": "Claude Code", "kind": "claude", "binary": one.binary, "model": "opus",
         "effort": "high"}, one.agents)
    assert problems == []

    def starts():
        if not one.argv_log.exists():
            return []
        return [json.loads(line) for line in one.argv_log.read_text().splitlines()]

    one.starts = starts
    return one


def _flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def _cycle(*steps, variables=None):
    return model.parse_cycle({"id": "c", "name": "Nightly", "variables": dict(variables or {}),
                              "steps": list(steps)}, "c")


def _edit(world, **settings):
    return {"id": "fix", "label": "Fix it", "plugin": "agent.edit",
            "with": dict({"task": "Write the notes.", "directory": str(world.repo)},
                         **settings)}


def _run(world, cycle, tmp_path, sessions_root=True):
    connections, problems = binding.load(
        cycle, agents_path=world.agents,
        agent_sessions=world.sessions if sessions_root else None)
    assert problems == []
    return run_cycle(cycle, str(tmp_path / "run"), connections=connections)


# -- the form -----------------------------------------------------------------
def test_a_step_naming_a_connection_needs_no_program_or_model():
    plugin = registry.get("agent.edit")
    assert plugin.problems({"connection": "claude-code", "task": "t", "directory": "d"}) == []
    assert "Connection" in [one.label for one in plugin.metadata.inputs]


def test_a_claude_step_without_a_model_takes_the_cli_s_own():
    """Blank is the connection's model, and without one the CLI's - as in chat."""
    assert registry.get("agent.implement").problems({"task": "t", "directory": "d"}) == []
    assert registry.get("agent.review").problems({"task": "t"}) == []


@pytest.mark.parametrize("framework", ["crewai", "autogen"])
def test_a_worker_framework_still_needs_its_model(framework):
    found = registry.get("agent.review").problems(
        {"task": "t", "framework": framework, "python": "/usr/bin/python3",
         "model_client": "x.Y"})
    assert "Model is required for %s." % framework in found


@pytest.mark.parametrize("plugin, settings, said", [
    ("agent.edit", {"claude": "/opt/claude"}, "Give Connection or claude, not both"),
    ("agent.review", {"framework": "crewai", "python": "/usr/bin/python3", "model": "m"},
     "Give Connection or python, not both"),
    ("agent.review", {"framework": "autogen", "model": "m", "model_client": "x.Y"},
     "framework autogen is not one"),
    ("agent.implement", {"connection": "${steps.pick.outputs.name}"},
     "connection may read only ${vars...}"),
])
def test_a_connection_and_a_program_together_are_refused(plugin, settings, said):
    base = {"connection": "claude-code", "task": "t", "directory": "d"}
    found = registry.get(plugin).problems(dict(base, **settings))
    assert any(said in one for one in found), found


def test_a_field_left_empty_beside_a_connection_says_nothing():
    found = registry.get("agent.review").problems(
        {"connection": "claude-code", "task": "t", "claude": "", "framework": "claude_cli"})
    assert found == []


def test_the_cycle_validator_accepts_an_agent_step_with_a_connection(world):
    cycle = _cycle(_edit(world, connection="claude-code"))
    assert model.problems(cycle, registry=registry) == []


# -- reading the connections ----------------------------------------------------
def test_agent_and_project_connections_are_named_apart():
    cycle = _cycle({"id": "q", "plugin": "jira.issues",
                    "with": {"connection": "product-team", "user": "currentUser()"}},
                   {"id": "fix", "plugin": "agent.edit",
                    "with": {"connection": "${vars.agent}", "task": "t", "directory": "d"}},
                   variables={"agent": "claude-code"})
    assert binding.named_by(cycle) == (["product-team"], [])
    assert binding.named_by(cycle, agents=True) == (["claude-code"], [])


def test_a_run_reads_the_agent_connection_it_names_and_where_calls_go(world):
    cycle = _cycle(_edit(world, connection="claude-code"))
    connections, problems = binding.load(cycle, agents_path=world.agents,
                                         agent_sessions=world.sessions)
    assert problems == []
    assert connections["agents/claude-code"]["binary"] == world.binary
    assert connections[binding.AGENTS] == {"sessions": world.sessions}


@pytest.mark.parametrize("entry, said", [
    (None, "No agent connection 'claude-code'. Add it in Integrations -> Agents"),
])
def test_a_run_naming_an_agent_connection_that_is_not_there_is_refused(world, tmp_path,
                                                                       entry, said):
    cycle = _cycle(_edit(world, connection="claude-code"))
    _connections, problems = binding.load(cycle, agents_path=str(tmp_path / "none.json"))
    assert any(said in one for one in problems), problems


def test_a_connection_to_a_program_cycles_cannot_run_yet_is_refused(world):
    raw = json.load(open(world.agents))
    raw["connections"][0]["kind"] = "codex"
    json.dump(raw, open(world.agents, "w"))
    cycle = _cycle(_edit(world, connection="claude-code"))
    _connections, problems = binding.load(cycle, agents_path=world.agents)
    assert problems == ["Claude Code runs codex; a cycle step can run claude only so far."]


def test_a_listener_reads_no_agent_connection(world, monkeypatch):
    monkeypatch.setattr(store, "load", lambda *_a: pytest.fail("a listener runs no agent"))
    cycle = _cycle(_edit(world, connection="claude-code"))
    assert binding.load(cycle, agents_path=world.agents, include_agents=False) == ({}, [])


def test_the_cycle_list_still_names_project_connections_alone(world):
    cycle = _cycle(_edit(world, connection="claude-code"))
    assert binding.named_by(cycle, env={})[0] == []


# -- expanding ----------------------------------------------------------------------
def test_a_connection_stands_for_the_program_and_fills_what_the_step_left_blank(world):
    connections, _ = binding.load(_cycle(_edit(world, connection="claude-code")),
                                  agents_path=world.agents)
    expanded = binding.expand(registry.get("agent.review"),
                              {"connection": "claude-code", "task": "t", "model": "",
                               "effort": "low"}, connections)
    assert expanded == {"task": "t", "claude": world.binary, "framework": "claude_cli",
                        "model": "opus", "effort": "low"}
    expanded = binding.expand(registry.get("agent.edit"),
                              {"connection": "claude-code", "task": "t", "directory": "d"},
                              connections)
    assert (expanded["model"], expanded["effort"], "framework" in expanded) == (
        "opus", "high", False)


def test_a_connection_this_run_did_not_read_is_said_as_the_step_s_failure():
    with pytest.raises(binding.BindingError) as caught:
        binding.expand(registry.get("agent.edit"), {"connection": "ghost", "task": "t"}, {})
    assert "No agent connection 'ghost'" in str(caught.value)


# -- a run ------------------------------------------------------------------------
def test_a_step_runs_the_connection_s_program_and_its_call_is_on_the_record(world, tmp_path):
    run = _run(world, _cycle(_edit(world, connection="claude-code")), tmp_path)
    assert run.steps["fix"].status == SUCCESS, run.steps["fix"].message
    (argv,) = world.starts()
    assert (_flag(argv, "--model"), _flag(argv, "--effort")) == ("opus", "high")
    (session,) = sessions.listing(world.sessions)["sessions"]
    assert session["source"] == {"label": "Cycle Nightly · step Fix it",
                                 "detail": "Run %s" % run.id}
    assert (session["connection"], session["connection_name"]) == ("claude-code", "Claude Code")
    assert session["title"] == "Nightly · Fix it" and session["mode"] == "edit"
    (turn,) = session["turns"]
    assert (turn["state"], turn["model"], turn["effort"], turn["cost_usd"], turn["steps"]) == (
        "completed", "opus", "high", 0.02, 2)
    assert turn["provider_session"] == "p-1"
    # Linked, not copied: the run keeps its own files.
    assert os.path.isfile(turn["files"]["stdout"]) and os.path.isfile(turn["files"]["prompt"])
    assert turn["files"]["stdout"].startswith(str(tmp_path / "run"))
    events = sessions.events(world.sessions, session["id"])
    assert [one["type"] for one in events] == [
        "user", "started", "text", "tool.started", "tool.completed", "file.written",
        "completed"]
    assert "Write the notes." in events[0]["text"]
    assert events[-1]["at"] >= events[0]["at"]
    # The integration's own log has it too.
    (logged,) = sessions.activity(world.sessions)
    assert logged["source"]["label"] == "Cycle Nightly · step Fix it"


def test_the_step_s_own_model_wins_over_the_connection_s(world, tmp_path):
    run = _run(world, _cycle(_edit(world, connection="claude-code", model="sonnet")),
               tmp_path)
    assert run.steps["fix"].status == SUCCESS, run.steps["fix"].message
    assert _flag(world.starts()[0], "--model") == "sonnet"


def test_a_step_naming_its_program_itself_is_on_the_record_too(world, tmp_path):
    run = _run(world, _cycle(_edit(world, claude=world.binary, model="sonnet")), tmp_path)
    assert run.steps["fix"].status == SUCCESS, run.steps["fix"].message
    (session,) = sessions.listing(world.sessions)["sessions"]
    assert (session["connection"], session["connection_name"]) == ("", "Claude Code")
    assert session["turns"][0]["model"] == "sonnet"


def test_a_run_not_told_where_the_record_is_records_nothing_and_still_runs(world, tmp_path):
    run = _run(world, _cycle(_edit(world, connection="claude-code")), tmp_path,
               sessions_root=False)
    assert run.steps["fix"].status == SUCCESS
    assert not os.path.exists(world.sessions)


def test_a_call_that_could_not_be_recorded_does_not_fail_its_step(world, tmp_path):
    os.makedirs(os.path.dirname(world.sessions), exist_ok=True)
    with open(world.sessions, "w") as handle:
        handle.write("a file where the record's folder should be")
    run = _run(world, _cycle(_edit(world, connection="claude-code")), tmp_path)
    assert run.steps["fix"].status == SUCCESS, run.steps["fix"].message


def test_a_run_whose_connection_is_gone_fails_the_step_saying_where_to_fix_it(world,
                                                                             tmp_path):
    run = run_cycle(_cycle(_edit(world, connection="claude-code")), str(tmp_path / "run"),
                    connections={})
    assert run.steps["fix"].status == FAILED
    assert "Integrations -> Agents -> Connections" in run.steps["fix"].message
    assert world.starts() == []


def test_a_call_replayed_from_its_receipt_is_not_recorded_again(world, tmp_path):
    """It was not made: the answer was paid for, and recorded, the first time."""
    workspace = create("20261112-000000-a", str(tmp_path / "runs"))
    context = RunContext(CycleRun(id="r", cycle_id="c", workspace=workspace),
                         _cycle(_edit(world)), workspace, observer=bus.Recorder())
    context.connections = {binding.AGENTS: {"sessions": world.sessions}}
    step = CycleStep(id="fix", plugin="agent.edit", settings={
        "model": "sonnet", "task": "Write the notes.", "directory": str(world.repo),
        "claude": world.binary})
    for _time in range(2):
        result = registry.get("agent.edit").execute(context, step)
        assert result.status == SUCCESS, result.message
    assert len(world.starts()) == 1
    (session,) = sessions.listing(world.sessions)["sessions"]
    assert len(session["turns"]) == 1


def test_every_call_of_one_step_is_one_conversation(world, tmp_path):
    workspace = create("20261112-000000-b", str(tmp_path / "runs"))
    context = RunContext(CycleRun(id="r", cycle_id="c", workspace=workspace),
                         _cycle(_edit(world)), workspace, observer=bus.Recorder())
    context.connections = {binding.AGENTS: {"sessions": world.sessions}}
    for task in ("First.", "Second."):
        step = CycleStep(id="fix", plugin="agent.edit", settings={
            "model": "sonnet", "task": task, "directory": str(world.repo),
            "claude": world.binary})
        assert registry.get("agent.edit").execute(context, step).status == SUCCESS
    (session,) = sessions.listing(world.sessions)["sessions"]
    assert [turn["n"] for turn in session["turns"]] == [1, 2]
    assert os.path.isfile(os.path.join(workspace, "steps", "fix", binding.AGENT_SESSION_FILE))


# -- through the launcher -------------------------------------------------------------
def _launcher(monkeypatch, capsys, *args):
    import runtime_paths
    import session_launcher
    monkeypatch.setattr("sys.argv", ["qavector"] + list(args))
    monkeypatch.setattr(runtime_paths, "_cycle_runs_override",
                        runtime_paths._cycle_runs_override)
    with pytest.raises(SystemExit) as caught:
        session_launcher.main()
    return caught.value.code, capsys.readouterr()


def _cycle_file(tmp_path, world, connection="claude-code"):
    cycles = tmp_path / "cycles"
    cycles.mkdir(exist_ok=True)
    (cycles / "c.yaml").write_text(
        "id: c\nname: Nightly\nsteps:\n  - id: fix\n    plugin: agent.edit\n    with:\n"
        "      connection: %s\n      task: Write the notes.\n      directory: %s\n"
        % (connection, world.repo), encoding="utf-8")
    return cycles


def test_a_real_run_reads_the_connection_and_records_its_call(world, tmp_path, monkeypatch,
                                                              capsys):
    monkeypatch.setenv("CMS_HOME", str(tmp_path))
    code, out = _launcher(monkeypatch, capsys, "--cycle-run=c",
                          "--cycles-dir=%s" % _cycle_file(tmp_path, world),
                          "--cycle-runs-dir=%s" % (tmp_path / "runs"),
                          "--agents-file=%s" % world.agents,
                          "--agent-sessions-dir=%s" % world.sessions)
    assert code == 0, out.err[-2000:]
    (session,) = sessions.listing(world.sessions)["sessions"]
    assert session["source"]["label"] == "Cycle Nightly · step fix"


def test_a_real_run_naming_a_missing_agent_connection_is_refused_before_it_starts(
        world, tmp_path, monkeypatch, capsys, caplog):
    monkeypatch.setenv("CMS_HOME", str(tmp_path))
    code, _out = _launcher(monkeypatch, capsys, "--cycle-run=c",
                           "--cycles-dir=%s" % _cycle_file(tmp_path, world, "ghost"),
                           "--cycle-runs-dir=%s" % (tmp_path / "runs"),
                           "--agents-file=%s" % world.agents)
    assert code == 2
    assert "No agent connection 'ghost'" in caplog.text
    assert world.starts() == []


def test_is_it_signed_in_is_asked_of_the_connection_s_program(world, tmp_path, monkeypatch,
                                                              capsys):
    settings = tmp_path / "step.json"
    settings.write_text(json.dumps({"connection": "claude-code", "task": "t"}))
    code, out = _launcher(monkeypatch, capsys, "--cycle-plugin-action=agent.review:sign_in_status",
                          "--from=%s" % settings, "--agents-file=%s" % world.agents)
    answer = json.loads(out.out)
    assert code == 0 and answer["ok"] is True, answer
    assert answer["summary"] == "Signed in as a@example.invalid"
    settings.write_text(json.dumps({"connection": "ghost", "task": "t"}))
    code, out = _launcher(monkeypatch, capsys, "--cycle-plugin-action=agent.review:sign_in_status",
                          "--from=%s" % settings, "--agents-file=%s" % world.agents)
    answer = json.loads(out.out)
    assert answer["ok"] is False and "No agent connection 'ghost'" in answer["summary"]


def test_the_cycle_list_names_no_agent_connection_as_a_project_one(world, tmp_path,
                                                                    monkeypatch, capsys):
    code, out = _launcher(monkeypatch, capsys, "--cycle-list",
                          "--cycles-dir=%s" % _cycle_file(tmp_path, world))
    assert code == 0
    assert [row["connections"] for row in json.loads(out.out)["cycles"]] == [[]]
