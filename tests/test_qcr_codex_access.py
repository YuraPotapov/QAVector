"""The Codex adapter's actual MCP argv reaches the real server and QCR socket."""

from codex_cli_fixture import fake_codex
from integrations.agents import runner, sessions, store
from test_qcr_agent_access import World


def test_codex_turn_discovers_only_granted_capabilities_then_loses_access(tmp_path):
    world = World(tmp_path)
    try:
        world.grant(actor="agent:codex/s1", grants=[{"permission": "services.read"}])
        agents = str(tmp_path / "agents.json")
        connection, errors = store.upsert({"name": "Codex", "kind": "codex", "binary": fake_codex(tmp_path)}, agents)
        assert not errors
        root = str(tmp_path / "sessions")
        session = sessions.create(root, connection, str(tmp_path), "read")
        access = {"socket": world.socket, "token_file": str(world.token_file)}
        result = runner.send(root, session["id"], "MCP_LIST", agents_path=agents, qavector=access)
        assert result["turn"]["state"] == "completed", result
        answer = sessions.events(root, session["id"])[-1]["text"]
        assert ".status" in answer and ".start\"" not in answer
        world.feed({"type": "revoke", "id": 10, "actor": "agent:codex/s1"})
        result = runner.send(root, session["id"], "MCP_LIST", agents_path=agents, qavector=access)
        assert result["turn"]["state"] == "failed"
    finally:
        world.host.close()
