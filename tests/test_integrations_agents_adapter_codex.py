import json
import subprocess
import threading
from pathlib import Path

import pytest

from codex_cli_fixture import fake_codex
from integrations.agents import adapters, codex_turn, runner, sessions, store
from integrations.agents.adapters import codex
from integrations.agents.contracts import Unsupported


def _spec(argv):
    assert argv[argv.index("--codex-turn") - 1].endswith("session_launcher.py")
    return json.loads(next(arg for arg in argv if arg.startswith("--spec="))[len("--spec="):])


def test_discovery_and_status_do_not_run_a_model(tmp_path):
    binary = fake_codex(tmp_path)
    assert adapters.get("codex") is codex
    assert codex.status(binary)["logged_in"]
    models = codex.models(binary)
    assert models["ok"] and models["models"][0]["value"] == "fixture-model"
    assert models["models"][0]["efforts"] == ["high"]


def test_login_output_never_discloses_a_key(tmp_path):
    result = codex.status(fake_codex(tmp_path), run=lambda *a, **k:
        subprocess.CompletedProcess(a, 0, b"", b"Logged in using an API key: sk-secret"))
    assert result["logged_in"] and result["method"] == "api_key"
    assert "sk-secret" not in json.dumps(result)


def test_rate_limits_read_all_buckets_without_starting_a_thread(tmp_path, monkeypatch):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    assert codex.rate_limits(fake_codex(tmp_path)) == {"ok": True, "windows": [
        {"label": "5-hour limit", "used_percent": 34, "resets_at": 1795200000,
         "duration_minutes": 300, "account_wide": True},
        {"label": "Weekly limit", "used_percent": 48, "resets_at": None,
         "duration_minutes": 10080, "account_wide": True},
        {"label": "5-hour limit · Example", "used_percent": 34, "resets_at": 1795200000,
         "duration_minutes": 300, "account_wide": False},
    ]}
    requests = [json.loads(line).get("method") for line in log.read_text().splitlines()]
    assert requests == [None, "initialize", "initialized", "account/rateLimits/read"]


def test_capabilities_advertise_planning_skills_and_edit():
    features = codex.capabilities()
    assert features.planning and features.skills and codex.PLANS
    assert features.modes == ("read", "edit", "full")
    assert not features.interactive_approvals


def test_turn_argv_is_the_bridge_with_choices_but_no_secrets():
    argv = codex.turn_argv("/bin/codex", "full", "chosen-model", "high", session="thread", resume=True,
                           skills_dir="/s", qavector={"socket": "/tmp/socket", "token_file": "/tmp/token"})
    spec = _spec(argv)
    assert spec["binary"] == "/bin/codex" and spec["thread"] == "thread" and spec["mode"] == "full"
    assert spec["model"] == "chosen-model" and spec["effort"] == "high" and spec["skills_root"] == "/s"
    assert spec["qavector"][-2:] == ["--socket=/tmp/socket", "--token-file=/tmp/token"]
    # A planning turn only reads: no QAVector server, whatever was granted.
    planning = _spec(codex.turn_argv("/bin/codex", "full", plan=True,
                                     qavector={"socket": "/tmp/socket", "token_file": "/tmp/token"}))
    assert planning["plan"] is True and planning["qavector"] is None
    assert _spec(codex.turn_argv("/bin/codex", session="new"))["thread"] == ""


@pytest.mark.parametrize("kwargs", [{"mode": "made-up"}, {"resume": True}, {"effort": "made-up"}])
def test_unsupported_choices_are_refused(kwargs):
    with pytest.raises(Unsupported):
        codex.turn_argv("codex", **kwargs)


def test_server_argv_switches_off_user_extensions_and_requires_qavector():
    argv = codex_turn.server_argv("codex", ["/core", "--qcr-mcp"], ["node_repl"])
    assert argv[:2] == ["codex", "app-server"]
    joined = " ".join(argv)
    for feature in ("plugins", "apps", "computer_use", "browser_use", "hooks",
                    "multi_agent", "multi_agent_v2", "multi_agent_v2_dynamic_tools"):
        assert "features.%s=false" % feature in joined
    assert "mcp_servers.node_repl.enabled=false" in argv
    assert ('mcp_servers.qavector={command="/core",args=["--qcr-mcp"],required=true,'
            'default_tools_approval_mode="approve"}') in argv
    assert "danger" not in joined


@pytest.mark.parametrize("mode,plan,expected", [
    ("read", False, ("read-only", "readOnly", "never")),
    ("full", True, ("read-only", "readOnly", "never")),
    ("edit", False, ("workspace-write", "workspaceWrite", "untrusted")),
    ("full", False, ("workspace-write", "workspaceWrite", "never"))])
def test_sandbox_per_mode(mode, plan, expected):
    thread, policy, approval = codex_turn.sandbox(mode, plan)
    assert (thread, policy["type"], approval) == expected and policy["networkAccess"] is False


def test_exec_fallback_keeps_the_old_command_and_its_limits(monkeypatch):
    monkeypatch.setenv(codex.TRANSPORT_ENV, "exec")
    argv = codex.turn_argv("codex", "full", "chosen-model", "high", session="thread", resume=True,
                           qavector={"socket": "/tmp/socket", "token_file": "/tmp/token"})
    assert all("features.%s=false" % name in argv for name in codex_turn.NATIVE_AGENTS)
    assert argv[-3:] == ["resume", "thread", "-"] and "--ignore-user-config" in argv
    assert 'sandbox_mode="workspace-write"' in argv and 'approval_policy="never"' in argv
    assert any("required=true" in arg and "/tmp/token" in arg for arg in argv)
    assert "mcp_servers={}" in codex.turn_argv("codex")
    assert not codex.capabilities().planning and codex.capabilities().modes == ("read", "full")
    for kwargs in ({"mode": "edit"}, {"plan": True}, {"skills_dir": "/skills"}):
        with pytest.raises(Unsupported):
            codex.turn_argv("codex", **kwargs)


def test_server_and_bridge_lines_become_conversation_events():
    def one(value):
        return codex.events(json.dumps(value))
    assert one({"qavector": "thread", "id": "t1", "model": "m"}) == [
        {"type": "started", "provider_session": "t1", "model": "m"}]
    assert one({"qavector": "decision", "decision": "declined", "item": "c1", "reason": "no"}) == [
        {"type": "tool.refused", "id": "c1", "reason": "no"}]
    assert one({"qavector": "decision", "decision": "accepted", "item": "p1"}) == []
    questions = one({"id": "q", "method": "item/tool/requestUserInput", "params": {"questions": [
        {"id": "a", "header": "H", "question": "Which?", "options": [{"label": "X", "description": ""}]}]}})
    assert questions == [{"type": "questions", "items": [{"question": "Which?", "options": ["X"], "multiple": False}]}]
    assert one({"method": "item/completed", "params": {"item": {"type": "plan", "id": "p", "text": " Do it "}}}) == [
        {"type": "plan", "text": "Do it"}]
    assert one({"method": "item/completed", "params": {"item": {"type": "reasoning", "summary": ["a", "b"]}}}) == [
        {"type": "thinking", "text": "a\n\nb"}]
    command = {"type": "commandExecution", "id": "c", "command": "ls", "status": "completed", "exitCode": 1}
    assert one({"method": "item/started", "params": {"item": command}})[0]["title"] == "ls"
    assert one({"method": "item/completed", "params": {"item": command}})[0]["ok"] is False
    mcp = {"type": "mcpToolCall", "id": "m", "server": "qavector", "tool": "list", "status": "completed",
           "arguments": {"a": 1}, "result": {"content": []}}
    assert one({"method": "item/completed", "params": {"item": mcp}})[0]["ok"] is True
    assert one({"method": "item/started", "params": {"item": mcp}})[0]["name"] == "qavector.list"
    assert one({"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {"last": {"inputTokens": 900}}}}) == [
        {"type": "context", "used": 900}]
    assert one({"id": 3, "result": {}}) == one({"method": "turn/started", "params": {}}) == []
    assert codex.events("garbage") == codex.events("[]") == codex.events('{"type":"future"}') == []


def test_only_successful_file_changes_are_recorded():
    item = {"type": "fileChange", "id": "patch", "changes": [
        {"path": "a.py", "kind": {"type": "update", "movePath": "moved.py"}},
        {"path": "b.py", "kind": {"type": "delete"}}]}
    started = codex.events(json.dumps({"method": "item/started", "params": {"item": item}}))
    assert started[0]["file_changes"] == [{"path": "a.py", "kind": "update", "destination": "moved.py"},
                                          {"path": "b.py", "kind": "delete"}]
    done = codex.events(json.dumps({"method": "item/completed", "params": {"item": dict(item, status="completed")}}))
    assert [one["type"] for one in done] == ["tool.completed", "file.written", "file.written"]
    assert done[0]["file_changes"] == started[0]["file_changes"]
    declined = codex.events(json.dumps({"method": "item/completed", "params": {"item": dict(item, status="declined")}}))
    assert len(declined) == 1 and declined[0]["ok"] is False
    assert declined[0]["declined"]
    exec_item = {"type": "file_change", "id": "patch", "changes": [{"path": "a.py", "kind": "add"}]}
    done = codex.events(json.dumps({"type": "item.completed", "item": dict(exec_item, status="completed")}))
    assert [one["type"] for one in done] == ["tool.completed", "file.written"]
    assert done[0]["file_changes"] == [{"path": "a.py", "kind": "add"}]


def test_reader_takes_the_bridge_end_and_its_own_counts():
    read = runner.Reader(codex)
    read.events(json.dumps({"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "done"}}}))
    read.events(json.dumps({"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {"last": {"inputTokens": 900}}}}))
    end = read.events(json.dumps({"qavector": "end", "state": "completed", "context_window": 272000,
                                  "tokens": {"input": 20, "cache_read": 80, "output": 7, "bogus": 1}}))[0]
    assert end["text"] == "done" and end["cost_usd"] is None and end.get("cost_total_usd") is None
    assert end["tokens"] == {"input": 20, "cache_read": 80, "output": 7}
    assert end["context"] == {"used": 900, "window": 272000}
    failed = codex.events(json.dumps({"qavector": "end", "state": "failed", "error": "no"}))[0]
    assert failed["type"] == "failed" and failed["error"] == "no"


@pytest.fixture(autouse=True)
def _track_changes(monkeypatch):
    """These tests cover what a turn changed, off by default in the app."""
    monkeypatch.setenv("QAVECTOR_TRACK_CHANGES", "1")


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = str(tmp_path / "agents.json")
    connection, errors = store.upsert({"name": "Codex", "kind": "codex", "binary": fake_codex(tmp_path)}, path)
    assert not errors
    root = str(tmp_path / "sessions")
    work = tmp_path / "work"
    work.mkdir()
    session = sessions.create(root, connection, str(work), "read")
    log = tmp_path / "codex.jsonl"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))

    def logged():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return root, path, session, logged, work


def _requests(logged, method):
    return [one["params"] for one in logged() if one.get("method") == method]


def test_new_and_resumed_turns_keep_provider_identity_and_raw_events(world):
    root, path, session, logged, _work = world
    first = runner.send(root, session["id"], "hello", agents_path=path)
    assert first["turn"]["state"] == "completed"
    assert first["session"]["cost_usd"] is None
    assert first["turn"]["tokens"] == {"input": 20, "cache_read": 80, "output": 7, "thinking": 5}
    assert first["turn"]["context"] == {"used": 100, "window": 272000}
    second = runner.send(root, session["id"], "continue", agents_path=path)
    thread = first["session"]["provider_session"]
    assert thread and thread == second["session"]["provider_session"]
    assert len(_requests(logged, "thread/start")) == 1
    assert [one["threadId"] for one in _requests(logged, "thread/resume")] == [thread]
    turns = _requests(logged, "turn/start")
    assert [one["input"][0]["text"] for one in turns] == ["hello", "continue"]
    assert all(one["collaborationMode"]["mode"] == "default" and one["approvalPolicy"] == "never"
               and one["sandboxPolicy"]["type"] == "readOnly" for one in turns)
    assert second["session"]["tokens"]["input"] == 40
    assert sessions.events(root, session["id"])[-1]["text"] == "Answer: continue"
    raw = Path(sessions.turn_directory(root, session["id"], 1), "stdout.log").read_text()
    assert '"thread/started"' in raw and '"qavector": "end"' in raw
    starts = [one["argv"] for one in logged() if "argv" in one]
    assert len(starts) == 2 and all("features.plugins=false" in argv for argv in starts)


@pytest.mark.parametrize("message,state", [("CRASH", "failed"), ("REFUSE", "failed"), ("HANG", "stopped")])
def test_failure_and_stop_are_not_success(world, message, state):
    root, path, session, logged, _work = world
    stop = threading.Event()

    def emit(event):
        if message == "HANG" and event["type"] == "started":
            stop.set()
    result = runner.send(root, session["id"], message, agents_path=path, emit=emit, stop=stop)
    assert result["turn"]["state"] == state
    if message == "HANG":
        # Stopped by interrupting the turn, not by killing Codex mid-write.
        assert _requests(logged, "turn/interrupt")


def test_questions_end_a_planning_turn_and_wait_for_answers(world):
    root, path, session, logged, _work = world
    result = runner.send(root, session["id"], "ASK", agents_path=path, plan=True)
    assert result["turn"]["state"] == "completed"
    assert result["session"]["phase"] == "questions"
    asked = [one for one in sessions.events(root, session["id"]) if one["type"] == "questions"]
    assert asked[0]["items"][0] == {"question": "Which database?", "options": ["Postgres", "SQLite"],
                                    "multiple": False}
    turn = _requests(logged, "turn/start")[0]
    assert turn["collaborationMode"]["mode"] == "plan" and turn["sandboxPolicy"]["type"] == "readOnly"
    assert _requests(logged, "turn/interrupt")


def test_plan_then_approved_work_switches_the_mode_back(world):
    root, path, session, logged, _work = world
    planned = runner.send(root, session["id"], "PLAN", agents_path=path, plan=True)
    assert planned["session"]["phase"] == "plan"
    plan = [one for one in sessions.events(root, session["id"]) if one["type"] == "plan"]
    assert plan[0]["text"].startswith("1. Change a.py")
    runner.send(root, session["id"], "Go ahead", agents_path=path, plan=False, mode="full")
    modes = [one["collaborationMode"]["mode"] for one in _requests(logged, "turn/start")]
    assert modes == ["plan", "default"]
    assert _requests(logged, "turn/start")[1]["sandboxPolicy"]["type"] == "workspaceWrite"


def test_edit_declines_commands_and_accepts_file_changes(world):
    root, path, session, logged, work = world
    result = runner.send(root, session["id"], "CMD PATCH", agents_path=path, mode="edit")
    assert result["turn"]["state"] == "completed"
    assert _requests(logged, "turn/start")[0]["approvalPolicy"] == "untrusted"
    rows = {one.get("id"): one for one in sessions.events(root, session["id"]) if one["type"] == "tool.completed"}
    assert rows["cmd-1"]["ok"] is False and rows["cmd-1"]["refused"] is True
    assert rows["patch-1"]["ok"] is True
    assert (work / "a.py").exists()
    assert result["turn"]["changes"]["files"] == 1


def test_edit_runs_commands_codex_reads_as_reading_and_declines_the_rest(world):
    root, path, session, logged, _work = world
    runner.send(root, session["id"], "READCMD CMD", agents_path=path, mode="edit")
    rows = {one.get("id"): one for one in sessions.events(root, session["id"]) if one["type"] == "tool.completed"}
    assert rows["cmd-2"]["ok"] is True
    assert rows["cmd-1"]["ok"] is False and rows["cmd-1"]["refused"] is True
    assert "only commands that read" in rows["cmd-1"]["reason"]


def test_full_never_asks_and_read_declines_even_reading_commands(world):
    root, path, session, logged, _work = world
    runner.send(root, session["id"], "READCMD", agents_path=path, mode="read")
    rows = {one.get("id"): one for one in sessions.events(root, session["id"]) if one["type"] == "tool.completed"}
    assert rows["cmd-2"]["ok"] is False          # Read: nothing runs that Codex asks about


def test_qavector_tools_are_approved_and_other_servers_are_not(world):
    root, path, session, logged, _work = world
    result = runner.send(root, session["id"], "ELICIT", agents_path=path, mode="edit")
    assert result["turn"]["state"] == "completed"
    assert sessions.events(root, session["id"])[-1]["text"] == "Answer: qavector=accept elsewhere=decline"


def test_reads_only_needs_codex_to_name_every_part():
    assert codex_turn.reads_only([{"type": "read"}, {"type": "search"}, {"type": "listFiles"}])
    assert not codex_turn.reads_only([{"type": "read"}, {"type": "unknown"}])
    assert not codex_turn.reads_only([]) and not codex_turn.reads_only(None)


def test_read_declines_file_changes(world):
    root, path, session, logged, work = world
    runner.send(root, session["id"], "PATCH", agents_path=path)
    assert not (work / "a.py").exists()


def test_selected_skills_are_an_extra_root_for_this_turn_only(world, tmp_path):
    root, path, session, logged, _work = world
    library = tmp_path / "library"
    (library / "probe").mkdir(parents=True)
    (library / "probe" / "SKILL.md").write_text("---\nname: probe\ndescription: For a test.\n---\n")
    runner.send(root, session["id"], "hello", agents_path=path, skills=["probe"], skills_root=str(library))
    roots = _requests(logged, "skills/extraRoots/set")[0]["extraRoots"]
    assert Path(roots[0], "probe", "SKILL.md").is_file()
    assert not Path(roots[0]).is_relative_to(library)


def test_user_mcp_servers_are_switched_off(world, monkeypatch):
    root, path, session, logged, _work = world
    monkeypatch.setenv("FAKE_CODEX_MCP", json.dumps({"node_repl": {"command": "x"}, "off": {"enabled": False}}))
    assert runner.send(root, session["id"], "hello", agents_path=path)["turn"]["state"] == "completed"
    starts = [one["argv"] for one in logged() if "argv" in one]
    assert len(starts) == 2
    assert "mcp_servers.node_repl.enabled=false" in starts[1]
    assert not any("mcp_servers.off" in arg for arg in starts[1])


def test_an_mcp_server_that_cannot_be_switched_off_fails_the_turn(world, monkeypatch):
    root, path, session, logged, _work = world
    monkeypatch.setenv("FAKE_CODEX_MCP", json.dumps({"odd.name": {"command": "x"}}))
    result = runner.send(root, session["id"], "hello", agents_path=path)
    assert result["turn"]["state"] == "failed" and "odd.name" in result["turn"]["error"]
    assert not _requests(logged, "turn/start")


def test_exec_fallback_refuses_what_it_cannot_do_and_leaves_no_turn(world, monkeypatch):
    root, path, session, logged, _work = world
    monkeypatch.setenv(codex.TRANSPORT_ENV, "exec")
    for kwargs in ({"mode": "edit"}, {"plan": True}, {"skills": ["example"]}):
        with pytest.raises(runner.Refused) as caught:
            runner.send(root, session["id"], "no", agents_path=path, **kwargs)
        assert caught.value.kind == "unsupported"
    assert not logged() and not sessions.load(root, session["id"])["turns"]
    result = runner.send(root, session["id"], "hello", agents_path=path)
    assert result["turn"]["state"] == "completed"
    assert logged()[0]["argv"][0] == "exec"


def test_provider_change_leaves_no_turn(world):
    root, path, session, logged, _work = world
    connection = store.load(path)[0]
    connection["kind"] = "claude"
    store.save([connection], path)
    with pytest.raises(runner.Refused, match="provider"):
        runner.send(root, session["id"], "no", agents_path=path)
    assert not logged()


def test_a_turn_keeps_the_model_and_effort_codex_ran_with(world):
    root, path, session, logged, _work = world
    turn = runner.send(root, session["id"], "hello", agents_path=path)["turn"]
    assert turn["model"] == "" and turn["model_used"] == "fixture-default"
    assert sessions.load(root, session["id"])["turns"][0]["model_used"] == "fixture-default"
    assert codex.events(json.dumps({"qavector": "thread", "id": "t", "model": "m", "effort": "high"}))[0]["effort"] == "high"
