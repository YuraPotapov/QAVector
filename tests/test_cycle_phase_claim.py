"""The Claim phase migration changes execution boundaries only."""

import copy
from collections import Counter

import pytest

from cycle import phase_claim, prepare_claim


@pytest.fixture
def prepared_claim():
    """A public, synthetic version of the reviewed 69-node shape."""
    nodes = []
    for names in phase_claim.MEMBERS.values():
        for name in names:
            nodes.append({
                "id": name,
                "plugin": "command.shell",
                "needs": [nodes[-1]["id"]] if nodes else [],
                "group": "Existing visual group",
                "timeout": 120,
                "with": {"command": "printf 'synthetic %s\\n'" % name},
            })
    by_id = {step["id"]: step for step in nodes}
    # Include real counter settings so a migration regression cannot silently
    # move, duplicate or rewrite the implementation budget.
    by_id["budget"].update(plugin="memory.remember", needs=["preflight", "baseline"])
    by_id["budget"]["with"] = {"key": "synthetic/task", "bump": ["attempts"]}
    by_id["spent"].update(plugin="memory.recall")
    by_id["spent"]["with"] = {"key": "synthetic/task", "field": "attempts", "default": "0"}
    by_id["admit"].update(plugin="check.gate")
    by_id["admit"]["with"] = {"condition": "${steps.spent.outputs.value} < 3"}
    by_id["implement"]["needs"] = ["budget"]
    return {
        "id": "claim_development",
        "name": "Synthetic Claim",
        "variables": {"attempt_limit": 3},
        "actions": {"prepare_task": {
            "title": "Prepare task",
            "steps": list(prepare_claim.PREPARE_STEPS),
            "result": "task_brief",
        }},
        "steps": nodes,
    }


def test_migration_assigns_all_69_nodes_to_the_six_reviewed_phases(prepared_claim):
    result = phase_claim.upgrade(prepared_claim)
    assert len(result["steps"]) == len({step["id"] for step in result["steps"]}) == 69
    assert result["phases"] == phase_claim.DEFINITIONS
    assert [phase["id"] for phase in result["phases"]] == [
        "prepare", "plan", "implement", "integrate", "publish", "verify"]
    assignments = {name: phase for phase, names in phase_claim.MEMBERS.items() for name in names}
    assert {step["id"]: step["phase"] for step in result["steps"]} == assignments
    assert Counter(step["phase"] for step in result["steps"]) == {
        "prepare": 14, "plan": 7, "implement": 11,
        "integrate": 24, "publish": 4, "verify": 9,
    }
    assert [phase["policy"] for phase in result["phases"]] == ["stop_after"] * 5 + ["automatic"]


def test_migration_preserves_the_bounded_13_step_prepare_action(prepared_claim):
    result = phase_claim.upgrade(prepared_claim)
    assert result["actions"] == prepared_claim["actions"]
    selected = result["actions"]["prepare_task"]["steps"]
    assert len(selected) == len(set(selected)) == 13
    assert "base_resolve" not in selected
    assert set(selected) == set(phase_claim.MEMBERS["prepare"]) - {"base_resolve"}


def test_migration_changes_only_phase_contract_and_step_phase(prepared_claim):
    before = copy.deepcopy(prepared_claim)
    result = phase_claim.upgrade(prepared_claim)
    stripped = copy.deepcopy(result)
    stripped.pop("phases")
    for step in stripped["steps"]:
        step.pop("phase")
    assert stripped == before
    assert prepared_claim == before


def test_migration_is_idempotent_and_returns_detached_data(prepared_claim):
    first = phase_claim.upgrade(prepared_claim)
    second = phase_claim.upgrade(first)
    assert second == first
    second["steps"][0]["with"]["command"] = "changed in the caller"
    second["phases"][0]["deliverables"].append("changed")
    second["actions"]["prepare_task"]["steps"].append("changed")
    assert first["steps"][0]["with"]["command"] != "changed in the caller"
    assert "changed" not in first["phases"][0]["deliverables"]
    assert "changed" not in phase_claim.DEFINITIONS[0]["deliverables"]
    assert "changed" not in prepared_claim["actions"]["prepare_task"]["steps"]


@pytest.mark.parametrize("change", ["missing", "extra", "renamed"])
def test_unknown_claim_graph_is_refused(prepared_claim, change):
    if change == "missing":
        prepared_claim["steps"].pop()
    elif change == "extra":
        prepared_claim["steps"].append({"id": "unreviewed", "plugin": "command.shell"})
    else:
        prepared_claim["steps"][0]["id"] = "unreviewed"
    with pytest.raises(ValueError, match="Unknown Claim graph"):
        phase_claim.upgrade(prepared_claim)


def test_migration_requires_the_existing_prepare_action(prepared_claim):
    prepared_claim.pop("actions")
    with pytest.raises(ValueError, match="Prepare task migration first"):
        phase_claim.upgrade(prepared_claim)


@pytest.mark.parametrize("policy", ["automatic", "manual", "approval_required"])
def test_existing_phase_policy_changes_are_refused(prepared_claim, policy):
    prepared_claim["phases"] = copy.deepcopy(phase_claim.DEFINITIONS)
    prepared_claim["phases"][0]["policy"] = policy
    before = copy.deepcopy(prepared_claim)
    with pytest.raises(ValueError, match="Existing phase policies differ"):
        phase_claim.upgrade(prepared_claim)
    assert prepared_claim == before


def test_existing_different_phase_definition_is_refused(prepared_claim):
    prepared_claim["phases"] = copy.deepcopy(phase_claim.DEFINITIONS)
    prepared_claim["phases"][0]["deliverables"] = ["facts"]
    with pytest.raises(ValueError, match="Existing phase policies differ"):
        phase_claim.upgrade(prepared_claim)
