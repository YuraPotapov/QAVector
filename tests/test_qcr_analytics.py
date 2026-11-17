"""Agent usage as QCR reads (ADR-008 U.2): the same sums every view uses, only
for an actor granted ``analytics.read``, and nothing but the project's record."""

import json

from integrations.agents import sessions
from qcr.host import Host


def _turn(cost, model="sonnet"):
    return {"n": 1, "started": 1000.0, "ended": 1005.0, "state": "completed", "model": model,
            "tokens": {"input": 10, "output": 5}, "cost_usd": cost,
            "cost_kind": "estimate" if cost is not None else "unknown"}


def _host(tmp_path, grants):
    root = str(tmp_path / "agent-sessions")
    owner = {"id": "claude", "name": "Claude", "kind": "claude"}
    parent = sessions.create(root, owner, str(tmp_path), "read", source={"label": "Chat", "ref": "jira:t:QA-1"})
    sessions.append_turn(root, parent["id"], _turn(0.5), [{"type": "user", "text": "hi"}])
    child = sessions.create(root, owner, str(tmp_path), "read",
                            source={"label": "Delegated", "parent": parent["id"], "record": True})
    sessions.append_turn(root, child["id"], _turn(None, "gpt-5"), [{"type": "user", "text": "task"}])
    sent = []
    host = Host("p", str(tmp_path / "journal.sqlite"), lambda line: sent.append(json.loads(line)))
    host._on_hello({"protocol": 1, "actors": {}})
    (tmp_path / "agents.json").write_text(json.dumps({"version": 1, "connections": []}))
    host._on_agent_connections({"sessions_root": root, "agents_path": str(tmp_path / "agents.json")})
    host._on_actor({"actor": "agent:claude/s1", "grants": [{"permission": one} for one in grants]})
    return host, parent["id"]


def test_an_agent_granted_usage_reads_the_same_sums_as_every_view(tmp_path):
    host, parent = _host(tmp_path, ["analytics.read"])
    try:
        ask = lambda **message: host.handle_agent("agent:claude/s1", message)
        listed = {one["capability_id"] for one in ask(type="capabilities")["capabilities"]}
        assert {"analytics.usage.summary", "analytics.usage.conversation", "analytics.usage.ref"} <= listed
        summary = ask(type="read", capability="analytics.usage.summary", input={"by": ["model", "kind"]})["result"]
        assert summary["turns"] == 2 and summary["estimate_usd"] == 0.5 and summary["unknown_cost_turns"] == 1
        assert sorted(summary["by"]["kind"]) == ["delegated", "jira"]
        one = ask(type="read", capability="analytics.usage.conversation", input={"session": parent})["result"]
        assert one["own"]["turns"] == 1 and one["inclusive"]["turns"] == 2 and len(one["children"]) == 1
        issue = ask(type="read", capability="analytics.usage.ref", input={"ref": "jira:t:QA-1"})["result"]
        assert issue["inclusive"]["unknown_cost_turns"] == 1
        missing = ask(type="read", capability="analytics.usage.conversation", input={"session": "nope"})
        assert missing["ok"] is False and missing["error"]["code"] == "not_found"
    finally:
        host.close()


def test_without_the_grant_nothing_of_it_is_seen(tmp_path):
    host, _parent = _host(tmp_path, ["services.read"])
    try:
        listed = host.handle_agent("agent:claude/s1", {"type": "capabilities"})["capabilities"]
        assert not [one for one in listed if one["capability_id"].startswith("analytics.")]
        refused = host.handle_agent("agent:claude/s1", {"type": "read", "capability": "analytics.usage.summary",
                                                        "input": {}})
        assert refused["ok"] is False
    finally:
        host.close()
