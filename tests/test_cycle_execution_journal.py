"""Execution-policy journal failures and decline during an existing pause."""

import json

import pytest

from cycle import checkpoints, executor
from test_cycle_execution_modes import Plugin, Plugins, answer, live, node, policy_file, workflow


def test_decline_during_a_user_pause_keeps_the_specific_decline_reason(tmp_path):
    with live(tmp_path, workflow(node("one")), mode="approve") as (events, plugin, result):
        request = events.request("one")
        executor.control_run("policy-live", "pause")
        events.wait("cycle.run.state", lambda e: e["action"] == "paused" and e["pause_reason"] == "user_request")
        assert answer(request, approved=False) == ""
        events.wait("cycle.run.state", lambda e: e["action"] == "paused" and e["pause_reason"] == "approval_declined")
        assert not plugin.calls
        assert checkpoints.Store(str(tmp_path)).load().steps["one"].status == "pending"


def test_checkpoint_reads_a_newer_policy_journal_without_a_metadata_update(tmp_path):
    with live(tmp_path, workflow(node("one")), mode="approve") as (events, plugin, result):
        events.request("one")
        executor.control_run("policy-live", "pause")
        events.wait("cycle.run.state", lambda e: e["action"] == "paused")
        before = (tmp_path / "metadata.json").read_text()
        executor.control_run("policy-live", "set_mode", mode="important")
        events.wait("cycle.run.state", lambda e: e["action"] == "policy_changed")
        (tmp_path / "metadata.json").write_text(before)
        saved = checkpoints.Store(str(tmp_path)).load()
        assert saved.execution_mode == "important" and saved.initial_execution_mode == "approve"
        assert saved.policy_changes[-1]["to"] == "important"
        assert not plugin.calls


def test_corrupt_node_receipt_is_refused_before_resuming_work(tmp_path):
    run = executor.run_cycle(workflow(node("one")), str(tmp_path), mode="approve", registry=Plugins(Plugin()))
    assert run.paused and run.steps["one"].status == "pending"
    journal = json.loads(policy_file(tmp_path).read_text())
    journal["node_approvals"] = {"broken": {"status": "approved"}}
    policy_file(tmp_path).write_text(json.dumps(journal))
    with pytest.raises(checkpoints.CheckpointError, match="unreadable node approval receipt"):
        checkpoints.Store(str(tmp_path)).load()
