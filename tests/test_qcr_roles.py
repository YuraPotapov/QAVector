"""Agent roles for delegation (QCR ADR-010), the core's side.

* Roles are targets on one resource, offered with a grant on it, each with a
  describe; a role whose program is gone is not offered.
* A task for a role runs on the role's program with its model, effort and
  instructions, never in more than the role's mode or the conversation's.
* A role with a verdict: approve lets what is after it start; rework, or no
  verdict at all, skips it and counts as a failure for waking the delegator.
* The conversation is told its roles in its session: once, and again when they
  change - in the prompt, never in what the person is recorded as saying.
"""

import json
import os
import time

import pytest

from agent_cli_fixture import fake_agent
from integrations.agents import roles, runner, sessions, store
from qcr.adapters import delegation
from test_qcr_delegation import ACTOR, World, _codex_turns

ROLES_GRANT = {"permission": "agents.delegate", "resource_id": delegation.ROLES}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "codex.jsonl"))
    made = []

    def make(*made_roles, mode="full"):
        w = World(tmp_path, mode=mode)
        for one in made_roles:
            role, found = roles.upsert(one, w.agents, new=True)
            assert not found, found
        w.host._on_agent_connections({"sessions_root": w.root, "agents_path": w.agents})
        grants = [{"permission": "agents.delegate", "resource_id": delegation.TASKS}, ROLES_GRANT]
        w.host._on_actor({"actor": ACTOR, "grants": grants, "agents": {
            "session": "s-parent", "connection": "claude", "name": "Claude", "folder": str(w.folder),
            "mode": mode, "others": "auto", "restrictions": {}, "wake": "continue"}})
        made.append(w)
        return w
    yield make
    for one in made:
        one.close()


def role(**more):
    return dict({"id": "dev", "name": "Developer", "does": "Changes code.", "connection": "codex", "mode": "edit",
                 "instructions": "Make the smallest change."}, **more)


def ended(w, execution_id, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        execution = w.host.store.get(execution_id)
        if execution["state"] in ("succeeded", "failed", "canceled", "timed_out"):
            return execution
        time.sleep(0.05)
    pytest.fail("did not end")


def hand(w, role_id, **task):
    answer = w.ask("invoke", capability="agents.roles.%s.delegate" % role_id,
                   input=dict({"goal": "Do the thing"}, **task))
    assert answer["ok"], answer
    return answer["execution"]["execution_id"]


def test_roles_are_offered_on_one_resource_with_their_describe(world):
    w = world(role())
    # A role whose program was removed afterwards: kept, but offered to nobody.
    roles._save(roles.load(w.agents) + [roles.normalize(role(id="ghost", name="Ghost", connection="gone"))],
                w.agents)
    w.host._on_agent_connections({"sessions_root": w.root, "agents_path": w.agents})
    ids = {one["capability_id"] for one in w.ask("capabilities")["capabilities"]}
    assert {"agents.roles.dev.delegate", "agents.roles.dev.describe"} <= ids
    assert not any("ghost" in one for one in ids), "a role whose program is gone is not offered"
    described = w.ask("read", capability="agents.roles.dev.describe", input={})["result"]
    assert described["role"]["instructions"] == "Make the smallest change." and described["provider"] == "Codex"


def test_a_role_runs_on_its_program_with_its_defaults_and_instructions(world, tmp_path):
    w = world(role(model="fixture-model", effort="high"))
    done = ended(w, hand(w, "dev"))
    assert done["state"] == "succeeded", done
    assert done["output"]["role"] == "dev" and done["output"]["mode"] == "edit"
    [turn] = [row for row in _codex_turns(tmp_path) if row.get("method") == "turn/start"]
    assert turn["params"].get("model") == "fixture-model" and turn["params"].get("effort") == "high"
    told = turn["params"]["input"][0]["text"]
    assert told.startswith("Your role: Developer - Changes code.\nMake the smallest change.")
    child = sessions.load(w.root, done["output"]["session_id"])
    assert child["source"]["role"] == "dev"


def test_a_role_never_works_in_more_than_its_mode_or_the_conversation_s(world):
    w = world(role(mode="read"), role(id="fixer", name="Fixer", mode="full"), mode="edit")
    refused = ended(w, hand(w, "dev", mode="edit"))
    assert refused["error"]["code"] == "mode_exceeds_role"
    capped = ended(w, hand(w, "fixer"))
    assert capped["output"]["mode"] == "edit"


VERDICT = role(id="reviewer", name="Reviewer", connection="claude", mode="read", verdict=True,
               does="Reviews the change.")


def test_approve_lets_what_is_after_it_start(world):
    w = world(VERDICT, role(id="committer", name="Committer"))
    review = hand(w, "reviewer", name="review", summary='{"verdict": "approve", "issues": []}')
    commit = hand(w, "committer", name="commit", after=["review"])
    assert ended(w, review)["output"]["verdict"] == "approve"
    assert ended(w, commit)["state"] == "succeeded"


@pytest.mark.parametrize("said, verdict", [('{"verdict": "rework", "issues": ["Tests are missing"]}', "rework"),
                                           ("Looks fine to me.", "no_verdict")])
def test_rework_or_no_verdict_skips_what_is_after_it_and_wakes_with_the_issues(world, said, verdict):
    w = world(VERDICT, role(id="committer", name="Committer"))
    review = hand(w, "reviewer", name="review", summary=said)
    commit = hand(w, "committer", name="commit", after=["review"])
    done = ended(w, review)
    assert done["state"] == "succeeded" and done["output"]["verdict"] == verdict
    assert ended(w, commit)["error"]["code"] == "skipped"
    w.host._on_revoke({"actor": ACTOR})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not [one for one in w.sent if one.get("type") == "round_finished"]:
        time.sleep(0.05)
    [told] = [one for one in w.sent if one.get("type") == "round_finished"]
    item = next(one for one in told["items"] if one["name"] == "review")
    assert told["failed"] is True and item["state"] == "rework" and item["role"] == "Reviewer"
    if verdict == "rework":
        assert item["issues"] == ["Tests are missing"]


def test_the_session_is_told_its_roles_once_and_again_when_they_change(tmp_path, monkeypatch):
    log = tmp_path / "starts.jsonl"
    monkeypatch.setenv("FAKE_AGENT_LOG", str(log))
    agents = str(tmp_path / "agents.json")
    store.upsert({"name": "Claude", "kind": "claude", "binary": fake_agent(tmp_path)}, agents)
    roles.upsert(role(connection="claude"), agents, new=True)
    root = str(tmp_path / "sessions")
    folder = tmp_path / "work"
    folder.mkdir()
    made = sessions.create(root, store.get("claude", agents), str(folder), "read", others="auto")
    access = {"socket": str(tmp_path / "qv.sock"), "token_file": str(tmp_path / "token")}

    def turn(message):
        runner.send(root, made["id"], message, agents_path=agents, qavector=access)
        return [json.loads(line) for line in log.read_text().splitlines()][-1]

    first = turn("Fix the login")
    said = json.dumps(first)
    assert "who you may hand work to" in said and "dev (Developer)" in said
    assert "who you may hand work to" not in json.dumps(turn("And the logout"))
    roles.upsert(role(id="tester", name="Tester", connection="claude", mode="read"), agents, new=True)
    assert "tester (Tester)" in json.dumps(turn("Now test it"))
    events = sessions.events(root, made["id"])
    users = [one["text"] for one in events if one.get("type") == "user"]
    assert users == ["Fix the login", "And the logout", "Now test it"]


def test_the_cli_lists_saves_and_removes_roles_and_offers_the_usual_ones(tmp_path, capsys):
    from integrations import cli
    agents = str(tmp_path / "agents.json")
    store.upsert({"name": "Codex", "kind": "codex"}, agents)
    store.upsert({"name": "Claude", "kind": "claude"}, agents)

    def ask(*words, document=None):
        argv = ["agents", *words, "--agents-file=" + agents]
        if document is not None:
            source = tmp_path / "doc.json"
            source.write_text(json.dumps(document))
            argv.append("--from=%s" % source)
        code = cli.main(argv)
        answer = json.loads(capsys.readouterr().out)
        assert (code == 0) == answer["ok"], answer
        return answer
    listed = ask("roles")
    assert listed["roles"] == []
    assert [(one["id"], one["connection"], one["mode"], one["verdict"]) for one in listed["templates"]] == [
        ("dev", "codex", "edit", False), ("tester", "claude", "read", False), ("reviewer", "claude", "read", True)]
    for one in listed["templates"]:
        assert ask("role-save", document={"role": one, "new": True})["ok"]
    assert ask("role-save", document={"role": listed["templates"][0], "new": True})["kind"] == "invalid"
    assert ask("role-save", document={"role": role(id="Bad id")})["kind"] == "invalid"
    assert [one["id"] for one in ask("roles")["roles"]] == ["dev", "tester", "reviewer"]
    assert os.stat(roles.path_for(agents)).st_mode & 0o077 == 0
    assert ask("role-delete", "tester")["id"] == "tester"
    assert ask("role-delete", "tester")["kind"] == "not_found"
