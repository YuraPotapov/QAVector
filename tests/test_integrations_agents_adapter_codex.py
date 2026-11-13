import json
import subprocess
import threading

import pytest

from codex_cli_fixture import fake_codex
from integrations.agents import adapters, runner, sessions, store
from integrations.agents.adapters import codex
from integrations.agents.contracts import Unsupported


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


def test_modes_resume_and_mcp_are_explicit():
    argv = codex.turn_argv("codex", "full", "chosen-model", "high", session="thread", resume=True,
                          qavector={"socket": "/tmp/socket", "token_file": "/tmp/token"})
    assert argv[-3:] == ["resume", "thread", "-"]
    assert "--last" not in argv and "--ignore-user-config" in argv
    assert 'sandbox_mode="workspace-write"' in argv
    assert 'approval_policy="never"' in argv
    assert any("required=true" in arg and "/tmp/token" in arg for arg in argv)
    assert "danger" not in " ".join(argv)
    assert 'sandbox_mode="read-only"' in codex.turn_argv("codex")
    assert "mcp_servers={}" in codex.turn_argv("codex")


@pytest.mark.parametrize("kwargs", [{"mode": "edit"}, {"plan": True}, {"skills_dir": "/skills"},
                                    {"resume": True}, {"effort": "made-up"}])
def test_unsupported_features_do_not_fallback(kwargs):
    with pytest.raises(Unsupported):
        codex.turn_argv("codex", **kwargs)


def test_event_reader_uses_reported_outcome_and_does_not_double_count_cache():
    read = runner.Reader(codex)
    read.events(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}))
    end = read.events(json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 7}}))[0]
    assert end["text"] == "done" and end["cost_usd"] is None
    assert end["tokens"] == {"input": 20, "cache_read": 80, "output": 7}
    assert "context" not in end
    assert codex.events('{"type":"turn.failed","error":{"message":"no"}}')[0]["type"] == "failed"
    assert codex.events("garbage") == codex.events("[]") == codex.events('{"type":"future"}') == []


def test_only_successful_file_changes_are_recorded():
    item = {"type": "file_change", "id": "patch", "changes": [{"path": "a.py"}, {"path": "b.py"}]}
    assert len(codex.events(json.dumps({"type": "item.completed", "item": dict(item, status="completed")}))) == 3
    assert len(codex.events(json.dumps({"type": "item.completed", "item": dict(item, status="failed")}))) == 1


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = str(tmp_path / "agents.json")
    connection, errors = store.upsert({"name": "Codex", "kind": "codex", "binary": fake_codex(tmp_path)}, path)
    assert not errors
    root = str(tmp_path / "sessions")
    work = tmp_path / "work"
    work.mkdir()
    session = sessions.create(root, connection, str(work), "read")
    log = tmp_path / "starts.jsonl"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    return root, path, session, log


def test_new_and_resumed_turns_keep_provider_identity_and_raw_events(world):
    root, path, session, log = world
    first = runner.send(root, session["id"], "hello", agents_path=path)
    assert first["turn"]["state"] == "completed"
    assert first["session"]["cost_usd"] is None
    second = runner.send(root, session["id"], "continue", agents_path=path)
    runs = [json.loads(line) for line in log.read_text().splitlines()]
    assert "resume" not in runs[0]["argv"]
    assert runs[1]["argv"][-3:] == ["resume", first["session"]["provider_session"], "-"]
    assert first["session"]["provider_session"] == second["session"]["provider_session"]
    assert second["session"]["tokens"]["input"] == 40
    assert sessions.events(root, session["id"])[-1]["text"] == "Answer: continue"
    from pathlib import Path
    assert '"thread.started"' in Path(sessions.turn_directory(root, session["id"], 1), "stdout.log").read_text()


@pytest.mark.parametrize("message,state", [("CRASH", "failed"), ("REFUSE", "failed"), ("HANG", "stopped")])
def test_failure_and_stop_are_not_success(world, message, state):
    root, path, session, log = world
    stop = threading.Event()
    def emit(event):
        if message == "HANG" and event["type"] == "started":
            stop.set()
    result = runner.send(root, session["id"], message, agents_path=path, emit=emit, stop=stop)
    assert result["turn"]["state"] == state


def test_unsupported_requests_and_provider_change_leave_no_turn(world):
    root, path, session, log = world
    for kwargs in ({"mode": "edit"}, {"plan": True}, {"skills": ["example"]}):
        with pytest.raises(runner.Refused) as caught:
            runner.send(root, session["id"], "no", agents_path=path, **kwargs)
        assert caught.value.kind == "unsupported"
    assert not log.exists() and not sessions.load(root, session["id"])["turns"]
    connection = store.load(path)[0]
    connection["kind"] = "claude"
    store.save([connection], path)
    with pytest.raises(runner.Refused, match="provider"):
        runner.send(root, session["id"], "no", agents_path=path)
