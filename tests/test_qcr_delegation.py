"""One agent hands a task to another through the QCR host (ADR-006, Phase 5.1).

The host is the real one, in-process; the agents are stand-in CLIs (no model is
asked anything). What is defended:

* only targets the actor was granted are visible and invocable, never its own
  connection, and only from a conversation that works with other agents;
* Approve waits for the person and runs nothing until then; a denial is the
  task's result; Auto runs at once;
* the child is a record in the delegator's folder, at most in its mode, with
  no QAVector access (depth 1);
* the output is evidence - every criterion unverified until its delegator
  records a verdict with an execution of its own.
"""

import json
import os
import threading
import time

import pytest

from agent_cli_fixture import fake_agent
from codex_cli_fixture import fake_codex
from integrations.agents import sessions, store
from qcr.adapters import delegation
from qcr.host import Host

ACTOR = "agent:claude/s-parent"


class World:
    def __init__(self, tmp_path, others="auto", mode="full", targets=("codex",)):
        self.tmp = tmp_path
        self.agents = str(tmp_path / "agents.json")
        store.upsert({"name": "Claude", "kind": "claude", "binary": fake_agent(tmp_path)}, self.agents)
        codex_home = tmp_path / "codex-bin"
        codex_home.mkdir()
        store.upsert({"name": "Codex", "kind": "codex", "binary": fake_codex(codex_home)}, self.agents)
        self.root = str(tmp_path / "sessions")
        self.folder = tmp_path / "work"
        self.folder.mkdir()
        self.sent = []
        self.host = Host("p", str(tmp_path / "journal.sqlite"), lambda line: self.sent.append(json.loads(line)))
        self.host._on_hello({"protocol": 1, "actors": {}})
        self.host._on_agent_connections({"sessions_root": self.root, "agents_path": self.agents})
        self.grant(others=others, mode=mode, targets=targets)

    def grant(self, actor=ACTOR, others="auto", mode="full", targets=("codex",), connection="claude"):
        grants = [{"permission": "agents.delegate", "resource_id": delegation.target_resource(one)}
                  for one in targets] + [{"permission": "agents.delegate", "resource_id": delegation.TASKS}]
        self.host._on_actor({"actor": actor, "grants": grants, "agents": {
            "session": "s-parent", "connection": connection, "name": "Claude", "folder": str(self.folder),
            "mode": mode, "others": others, "restrictions": {}}})

    def ask(self, kind, actor=ACTOR, **fields):
        return self.host.handle_agent(actor, dict(fields, type=kind))

    def delegate(self, target="codex", actor=ACTOR, **task):
        answer = self.ask("invoke", actor, capability="agents.%s.delegate" % target,
                          input=dict({"goal": "Fix the login test",
                                      "acceptance_criteria": ["login passes", "nothing else changed"],
                                      "summary": "The selector changed."}, **task))
        return answer

    def wait(self, execution_id, actor=ACTOR):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            execution = self.ask("wait", actor, execution_id=execution_id, timeout=1)["execution"]
            if execution["state"] in ("succeeded", "failed", "canceled", "timed_out"):
                return execution
        pytest.fail("the delegation did not end: %s %s" % (execution["state"], [
            event.get("type") for event in self.events(execution_id)]))

    def events(self, execution_id):
        return self.host.store.events(execution_id)

    def close(self):
        self.host.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "codex.jsonl"))
    made = []

    def make(**kwargs):
        one = World(tmp_path, **kwargs)
        made.append(one)
        return one
    yield make
    for one in made:
        one.close()


def _codex_turns(tmp_path):
    log = tmp_path / "codex.jsonl"
    rows = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return rows


def test_only_granted_targets_are_offered_and_its_own_connection_when_granted(world):
    w = world()
    listed = w.ask("capabilities")
    ids = {one["capability_id"] for one in listed["capabilities"]}
    assert "agents.codex.delegate" in ids and "agents.tasks.verify" in ids
    assert "agents.claude.delegate" not in ids
    refused = w.delegate("claude")
    assert not refused["ok"]
    # Granted, its own connection takes a task too - another model of the same
    # program, say (ADR-009); the child cannot hand anything on in turn.
    w.grant(targets=("codex", "claude"))
    own = w.delegate("claude")
    assert own["ok"], own
    done = w.wait(own["execution"]["execution_id"])
    assert done["state"] == "succeeded", done


def test_auto_runs_the_child_once_as_a_record_with_evidence(world, tmp_path):
    w = world(others="auto", mode="edit")
    started = w.delegate()
    assert started["ok"], started
    done = w.wait(started["execution"]["execution_id"])
    assert done["state"] == "succeeded", done
    output = done["output"]
    assert output["provider"] == "codex" and output["mode"] == "edit"     # the parent's mode
    assert output["acceptance"] == [{"criterion": "login passes", "status": "unverified"},
                                    {"criterion": "nothing else changed", "status": "unverified"}]
    assert output["verified"] is False and output["answer"].startswith("Answer: ")
    child = sessions.load(w.root, output["session_id"])
    assert child["folder"] == str(w.folder) and child["mode"] == "edit" and child["others"] == "deny"
    assert child["source"]["record"] and child["source"]["ref"] == "qcr-task:" + done["execution_id"]
    turns = [row for row in _codex_turns(tmp_path) if row.get("method") == "turn/start"]
    assert len(turns) == 1
    told = turns[0]["params"]["input"][0]["text"]
    assert "Goal: Fix the login test" in told and "1. login passes" in told and "The selector changed." in told
    # The delegator had no QAVector access of its own, so the child gets none either.
    starts = [row["argv"] for row in _codex_turns(tmp_path) if "argv" in row]
    assert not any("mcp_servers.qavector" in arg for argv in starts for arg in argv)
    kinds = [event.get("type") for event in w.events(done["execution_id"])]
    assert "agent.started" in kinds and "usage.reported" in kinds


def test_a_child_never_gets_more_than_its_parent(world):
    w = world(mode="read")
    started = w.delegate(mode="full")
    failed = w.wait(started["execution"]["execution_id"])
    assert failed["state"] == "failed" and failed["error"]["code"] == "mode_exceeds_parent"


def test_approve_waits_for_the_person_and_runs_nothing_until_then(world, tmp_path):
    w = world(others="approve")
    started = w.delegate()
    execution_id = started["execution"]["execution_id"]
    time.sleep(0.3)
    current = w.ask("get", execution_id=execution_id)["execution"]
    assert current["state"] == "dispatched"
    assert not _codex_turns(tmp_path)
    asked = [one for one in w.sent if one.get("type") == "agent_approval"]
    assert asked and asked[0]["execution_id"] == execution_id and asked[0]["goal"] == "Fix the login test"
    assert "approval.requested" in [event.get("type") for event in w.events(execution_id)]
    assert w.host._on_agent_answer({"execution_id": execution_id, "approve": True, "by": "you"}) == {}
    assert w.wait(execution_id)["state"] == "succeeded"
    answered = [event for event in w.events(execution_id) if event.get("type") == "approval.answered"]
    assert answered[0]["payload"] == {"approved": True, "by": "you"}


def test_a_denial_is_the_result_and_nothing_ran(world, tmp_path):
    w = world(others="approve")
    execution_id = w.delegate()["execution"]["execution_id"]
    w.host._on_agent_answer({"execution_id": execution_id, "approve": False, "by": "you"})
    ended = w.wait(execution_id)
    assert ended["state"] == "failed" and ended["error"] == {"code": "denied", "message": "Denied by you."}
    assert not _codex_turns(tmp_path)
    with pytest.raises(Exception):
        w.host._on_agent_answer({"execution_id": execution_id, "approve": True})


def test_cancel_while_waiting_for_the_person(world, tmp_path):
    w = world(others="approve")
    execution_id = w.delegate()["execution"]["execution_id"]
    canceled = w.ask("cancel", execution_id=execution_id)["execution"]
    assert canceled["state"] == "canceled"
    assert not w.host.delegation._pending and not _codex_turns(tmp_path)


def test_deny_and_a_conversation_without_a_policy_delegate_nothing(world):
    w = world(others="deny")
    started = w.delegate()
    assert w.wait(started["execution"]["execution_id"])["error"]["code"] == "authorization_denied"
    w.host._on_actor({"actor": "agent:x/y", "grants": [
        {"permission": "agents.delegate", "resource_id": delegation.target_resource("codex")}]})
    stranger = w.delegate(actor="agent:x/y")
    assert w.wait(stranger["execution"]["execution_id"], actor="agent:x/y")["error"]["code"] == \
        "authorization_denied"


def test_children_per_conversation_are_limited(world, monkeypatch):
    monkeypatch.setattr(delegation, "MAX_CHILDREN", 1)
    w = world(others="approve", targets=("codex", "claude-2"))
    store.upsert({"name": "Claude 2", "kind": "claude", "binary": fake_agent(w.tmp / "codex-bin")}, w.agents)
    w.host._on_agent_connections({"sessions_root": w.root, "agents_path": w.agents})
    w.grant(others="approve", targets=("codex", "claude-2"))
    first = w.delegate()["execution"]["execution_id"]
    second = w.delegate("claude-2")["execution"]["execution_id"]
    assert w.wait(second)["error"]["code"] == "limit_exceeded"
    w.ask("cancel", execution_id=first)


def test_verification_needs_the_delegators_own_evidence(world):
    w = world()
    task = w.wait(w.delegate()["execution"]["execution_id"])
    proof = w.wait(w.delegate(goal="Run the login scenario")["execution"]["execution_id"])

    def verify(actor=ACTOR, **fields):
        answer = w.ask("invoke", actor, capability="agents.tasks.verify",
                       input=dict({"task": task["execution_id"]}, **fields))
        return w.wait(answer["execution"]["execution_id"], actor) if answer["ok"] else answer
    one = verify(criterion=0, verdict="passed", evidence=proof["execution_id"])
    assert one["state"] == "succeeded", one
    assert one["output"]["evidence"]["state"] == "succeeded" and one["output"]["verified"] is False
    both = verify(criterion=1, verdict="passed")
    assert both["output"]["verified"] is True
    assert [row["status"] for row in both["output"]["acceptance"]] == ["passed", "passed"]
    again = verify(criterion=1, verdict="failed")
    assert again["output"]["verified"] is False
    assert verify(criterion=5, verdict="passed")["error"]["code"] == "invalid_input"
    # Another agent can neither see the task nor verify it.
    w.grant(actor="agent:codex/other", connection="codex", targets=("claude",))
    other = verify(actor="agent:codex/other", criterion=0, verdict="passed")
    assert other["error"]["code"] == "not_found"


def test_closing_fails_what_waits_and_stops_what_runs(world, tmp_path):
    w = world(others="approve")
    waiting = w.delegate()["execution"]["execution_id"]
    w.host.delegation.close()
    assert w.host.store.get(waiting)["error"]["code"] == "source_unavailable"


def test_the_policy_comes_only_from_the_owner(world):
    w = world()
    for kind in ("actor", "agent_connections", "agent_answer"):
        assert not w.ask(kind)["ok"]
    with pytest.raises(Exception):
        w.host._on_actor({"actor": "agent:z/z", "grants": [], "agents": {"session": "s", "connection": "c",
                          "folder": "relative", "mode": "full", "others": "auto"}})


def test_codex_delegates_to_claude_through_the_real_mcp_server_and_socket(tmp_path, monkeypatch):
    """Codex -> Claude end to end: the chat turn's MCP server, the agent socket,
    the host, the delegation, the child Claude turn - and the answer back."""
    from qcr.agent_socket import digest
    from integrations.agents import runner
    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "codex.jsonl"))
    w = World(tmp_path, others="auto", mode="read", targets=())
    try:
        socket_path = w.host.open_agent_socket(str(tmp_path / "sock"))
        codex = store.get("codex", w.agents)
        parent = sessions.create(w.root, codex, str(w.folder), "read", others="auto")
        actor = "agent:codex/%s" % parent["id"]
        w.host._on_actor({"actor": actor, "grants": [
            {"permission": "agents.delegate", "resource_id": delegation.target_resource("claude")},
            {"permission": "agents.delegate", "resource_id": delegation.TASKS}],
            "agents": {"session": parent["id"], "connection": "codex", "name": "Codex",
                       "folder": str(w.folder), "mode": "read", "others": "auto", "restrictions": {}}})
        token = "b" * 64
        w.host._on_agent_token({"actor": actor, "token_sha256": digest(token), "expires_at": time.time() + 60})
        token_file = tmp_path / "token"
        token_file.write_text(token)
        os.chmod(token_file, 0o600)
        result = runner.send(w.root, parent["id"], "MCP_DELEGATE", agents_path=w.agents,
                             qavector={"socket": socket_path, "token_file": str(token_file)})
        assert result["turn"]["state"] == "completed", result
        said = sessions.events(w.root, parent["id"])[-1]["text"]
        assert said.startswith("Answer: delegated succeeded: ") and "Say hello" in said
        children = [one for one in sessions.listing(w.root)["sessions"]
                    if (one.get("source") or {}).get("label") == "Delegated"]
        assert len(children) == 1 and children[0]["kind"] == "claude" and children[0]["mode"] == "read"
    finally:
        w.close()


def test_the_child_gets_the_delegators_access_its_own_connection_allows_never_agents(tmp_path, monkeypatch):
    """ADR-006 D2 as changed on 2026-11-14: a delegated turn may use QAVector -
    what the delegator was given and the target's switches allow too, never
    agents.* (depth 1) - for that one turn."""
    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "codex.jsonl"))
    w = World(tmp_path, others="auto", mode="read")
    try:
        w.host.open_agent_socket(str(tmp_path / "sock"))
        codex = store.get("codex", w.agents)
        store.upsert(dict(codex, qavector_access={"services": True}), w.agents)
        grants = [{"permission": "agents.delegate", "resource_id": delegation.target_resource("codex")},
                  {"permission": "agents.message", "resource_id": delegation.message_resource("codex")},
                  {"permission": "services.read"}, {"permission": "services.start"},
                  {"permission": "scenarios.run"}]
        w.host._on_actor({"actor": ACTOR, "grants": grants, "agents": {
            "session": "s-parent", "connection": "claude", "name": "Claude", "folder": str(w.folder),
            "mode": "read", "others": "auto", "restrictions": {}}})
        issued = []
        real = w.host._issue
        monkeypatch.setattr(w.host, "_issue", lambda actor, given: (issued.append((actor, given)), real(actor, given)))
        done = w.wait(w.delegate(goal="MCP_LIST")["execution"]["execution_id"])
        assert done["state"] == "succeeded", done
        child_actor, given = issued[-1]
        assert child_actor.startswith("agent:codex/")
        assert sorted(one["permission"] for one in given) == ["services.read", "services.start"]
        assert done["output"]["answer"].startswith("Answer: ")       # the child's MCP call went through
        starts = [row["argv"] for row in _codex_turns(tmp_path) if "argv" in row]
        assert any(arg.startswith("mcp_servers.qavector=") for argv in starts for arg in argv)
        # The turn over, its access is gone.
        assert child_actor not in w.host._callers
        assert not os.listdir(tmp_path / "sock" / "tokens") if (tmp_path / "sock" / "tokens").exists() else True
        kept = delegation.packages.load(done["output"]["context_package"], w.host.context_dir)
        assert kept["constraints"]["qavector_access"] == ["services.read", "services.start"]
    finally:
        w.close()


def test_the_child_conversation_names_its_parent(world):
    w = world()
    done = w.wait(w.delegate()["execution"]["execution_id"])
    child = sessions.load(w.root, done["output"]["session_id"])
    assert child["source"]["parent"] == "s-parent" and child["source"]["by"] == "Claude"


# -- the delegator chooses the child's model and effort --------------------------------
def test_describe_reads_the_target_s_models_and_role_from_its_program(world):
    w = world()
    store.upsert(dict(store.get("codex", w.agents), role="implementation and tests"), w.agents)
    answer = w.ask("read", capability="agents.codex.describe", input={})
    assert answer["ok"], answer
    found = answer["result"]
    assert found["role"] == "implementation and tests" and found["provider"] == "Codex"
    assert found["models_known"] is True and found["models"][0]["value"] == "fixture-model"
    assert found["models"][0]["efforts"] == ["high"] and "minimal" in found["efforts"]


def test_the_chosen_model_and_effort_run_the_child(world, tmp_path):
    w = world()
    w.ask("read", capability="agents.codex.describe", input={})
    started = w.delegate(model="fixture-model", effort="high")
    assert started["ok"], started
    done = w.wait(started["execution"]["execution_id"])
    assert done["state"] == "succeeded", done
    [turn] = [row for row in _codex_turns(tmp_path) if row.get("method") == "turn/start"]
    assert turn["params"].get("model") == "fixture-model" and turn["params"].get("effort") == "high"


def test_a_model_or_effort_the_target_does_not_offer_is_refused_before_anything_is_written(world):
    w = world()
    w.ask("read", capability="agents.codex.describe", input={})
    for task, said in (({"model": "no-such-model"}, "fixture-model"), ({"effort": "max"}, "minimal"),
                       ({"model": "fixture-model", "effort": "low"}, "high")):
        refused = w.delegate(**task)
        assert not refused["ok"] and refused["error"]["code"] == "invalid_input", refused
        assert said in refused["error"]["message"]
    assert w.host.store.list(project_id="p") == []


def test_a_model_chosen_before_the_list_was_read_is_checked_before_the_child_starts(world, tmp_path):
    w = world()
    started = w.delegate(model="no-such-model")
    assert started["ok"], "nothing known yet: checked when it runs"
    done = w.wait(started["execution"]["execution_id"])
    assert done["error"]["code"] == "invalid_input" and "fixture-model" in done["error"]["message"]
    assert not [row for row in _codex_turns(tmp_path) if row.get("method") == "turn/start"]
