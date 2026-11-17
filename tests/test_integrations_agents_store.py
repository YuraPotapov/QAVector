"""The agent connections: one file for the whole of QAVector, and no secret in it.

Two properties are worth more than the rest. An id never changes, because
whatever uses a connection names it by its id. And what a check found is kept
only for as long as it is still about the same program.
"""

import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import runtime_paths                                               # noqa: E402
from integrations.agents import store                              # noqa: E402


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "agents.json")


def _add(path, name="Claude", **fields):
    saved, problems = store.upsert(dict({"name": name, "kind": "claude"}, **fields), path)
    assert problems == []
    return saved


def test_a_connection_gets_its_id_from_its_first_name(path):
    assert _add(path, "Claude for reviews")["id"] == "claude-for-reviews"
    assert [one["id"] for one in store.load(path)] == ["claude-for-reviews"]


def test_renaming_keeps_the_id_everything_else_calls_it_by(path):
    saved = _add(path)
    changed, problems = store.upsert(dict(saved, name="Claude Code"), path)
    assert problems == []
    assert changed["id"] == saved["id"] == "claude"
    assert store.get("claude", path)["name"] == "Claude Code"


def test_the_file_is_readable_by_its_owner_only_and_holds_no_secret(path):
    _add(path, model="sonnet", effort="high")
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    entry = json.load(open(path, encoding="utf-8"))["connections"][0]
    assert entry == {"id": "claude", "name": "Claude", "kind": "claude",
                     "binary": "", "model": "sonnet", "effort": "high", "role": "",
                     # QAVector through QCR: nothing until somebody turns it on (ADR-003 D6).
                     "qavector_access": {"services": False, "scenarios": False, "cycles": False,
                                         "approvals": False, "jira": False, "jira_write": False, "usage": False},
                     # Other agents: a new connection asks the person each time (ADR-006 D3).
                     "others_default": "approve", "allowed_agents": [],
                     # When its tasks end, a conversation asks the person (ADR-009 D3).
                     "wake_default": "ask",
                     # No budget unless the person sets one (ADR-013).
                     "budget_task": {}, "budget_conversation": {}}


def test_other_agents_defaults_new_old_and_never_itself(path):
    first, _ = store.upsert({"name": "Claude", "kind": "claude"}, path)
    second, _ = store.upsert({"name": "Codex", "kind": "codex"}, path)
    assert second["others_default"] == "approve" and second["allowed_agents"] == ["claude"]
    # One added later is not allowed anywhere on its own.
    assert store.get("claude", path)["allowed_agents"] == []
    # A connection from before ADR-006: nothing changes for it.
    assert store.normalize({"id": "old", "name": "Old"})["others_default"] == "deny"
    assert store.normalize({"id": "old", "name": "Old"})["allowed_agents"] == []
    mine, _ = store.upsert(dict(second, allowed_agents=["codex", "claude", "claude", 7]), path)
    assert mine["allowed_agents"] == ["claude"]
    chosen, _ = store.upsert({"name": "Quiet", "kind": "codex", "others_default": "deny",
                              "allowed_agents": []}, path)
    assert chosen["others_default"] == "deny" and chosen["allowed_agents"] == []
    assert store.normalize({"others_default": "sometimes"})["others_default"] == "deny"


def test_it_lives_beside_the_users_data_whichever_project_is_open(tmp_path, monkeypatch):
    monkeypatch.setenv(runtime_paths.HOME_ENV, str(tmp_path))
    assert store.default_path() == str(tmp_path / "agents.json")
    assert store.resolve_path("") == store.default_path()
    assert store.resolve_path("~/elsewhere.json") == os.path.expanduser("~/elsewhere.json")


def test_no_file_yet_is_no_connections(path):
    assert store.load(path) == []
    assert store.get("claude", path) is None


def test_a_file_that_is_not_connections_says_so(path):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("[1, 2]")
    with pytest.raises(store.StoreError):
        store.load(path)


@pytest.mark.parametrize("entry, said", [
    ({"name": "", "kind": "claude"}, "Name is required."),
    ({"name": "Other", "kind": "telepathy"}, "Unknown agent 'telepathy'"),
    ({"name": "Other", "kind": "claude", "effort": "extreme"}, "Effort must be one of"),
])
def test_what_is_wrong_with_a_connection_is_said_and_nothing_is_written(path, entry, said):
    saved, problems = store.upsert(entry, path)
    assert any(said in one for one in problems), problems
    assert not os.path.exists(path)


def test_two_connections_cannot_share_a_name(path):
    _add(path)
    _saved, problems = store.upsert({"name": "claude", "kind": "claude"}, path)
    assert problems == ["A connection called 'claude' already exists."]


def test_an_id_that_names_nothing_is_not_quietly_added(path):
    _saved, problems = store.upsert({"id": "ghost", "name": "Ghost", "kind": "claude"}, path)
    assert problems == ["No connection 'ghost' to change."]


def test_a_second_connection_of_the_same_name_root_gets_its_own_id(path):
    _add(path, "Claude")
    assert _add(path, "Claude!")["id"] == "claude-2"


def test_what_a_check_found_survives_a_rename_but_not_another_program(path):
    saved = _add(path)
    checked = dict(saved, last_check={"ok": True, "account": "someone@example.invalid"})
    store.upsert(checked, path)
    # A form sends back the fields it shows, which never include the check.
    renamed, _ = store.upsert({"id": "claude", "name": "Renamed", "kind": "claude"}, path)
    assert renamed["last_check"]["ok"] is True
    moved, _ = store.upsert({"id": "claude", "name": "Renamed", "kind": "claude",
                             "binary": "/opt/other/claude"}, path)
    assert "last_check" not in moved
    assert "last_check" not in store.get("claude", path)


def test_a_field_from_a_newer_build_survives_being_saved_by_this_one(path):
    saved = _add(path, home="/somewhere/else")
    store.upsert(dict(saved, name="Renamed"), path)
    assert store.get("claude", path)["home"] == "/somewhere/else"


def test_removing_one_leaves_the_others(path):
    _add(path, "Claude")
    _add(path, "Second")
    assert store.remove("claude", path) is True
    assert store.remove("claude", path) is False
    assert [one["id"] for one in store.load(path)] == ["second"]



def test_qavector_access_keeps_only_its_switches_and_only_true_turns_one_on():
    normalized = store.normalize({"name": "C", "qavector_access": {
        "services": True, "approvals": "yes", "cycles": 1, "everything": True}})
    assert normalized["qavector_access"] == {"services": True, "scenarios": False, "cycles": False,
                                             "approvals": False, "jira": False, "jira_write": False, "usage": False}
    assert store.normalize({"name": "C", "qavector_access": "all"})["qavector_access"]["services"] is False


def test_a_connections_agent_cannot_change(path):
    made, _ = store.upsert({"name": "Codex", "kind": "codex"}, path)
    changed, found = store.upsert(dict(made, kind="claude"), path)
    assert found == ["A connection's agent cannot change (it is codex)."]
    assert store.get(made["id"], path)["kind"] == "codex"
