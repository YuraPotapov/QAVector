"""Core-owned per-node consent: durable decisions, parallelism and recovery.

These tests run only synthetic plugins. Conditions and events establish ordering;
none of the assertions depend on sleeps or a particular scheduler speed.
"""

import copy
import json
import threading
import time
from contextlib import contextmanager

import pytest

from cycle import checkpoints, executor, model, registry
from cycle.bus import EventObserver
from cycle.context import CancelToken


class Plugin(registry.CyclePlugin):
    metadata = registry.PluginMetadata(id="test.policy", name="Synthetic work")

    def __init__(self, action=None):
        self.action, self.calls = action, []

    def execute(self, context, step):
        self.calls.append(step.id)
        return self.action(context, step) if self.action else registry.succeeded()


class Plugins:
    def __init__(self, plugin):
        self.plugin = plugin

    def get(self, name):
        return self.plugin if name == "test.policy" else registry.get(name)


def node(name, **extra):
    return dict({"id": name, "plugin": "test.policy"}, **extra)


def workflow(*steps, default_mode=None):
    raw = {"id": "policy", "steps": list(steps)}
    if default_mode is not None:
        raw["execution"] = {"default_mode": default_mode}
    return model.parse_cycle(raw, "policy")


class Events(EventObserver):
    def __init__(self, callback=None):
        self.events, self.condition, self.callback = [], threading.Condition(), callback
        super().__init__(sink=self.emit)

    def emit(self, kind, **fields):
        event = dict(fields, kind=kind)
        with self.condition:
            self.events.append(copy.deepcopy(event))
            self.condition.notify_all()
        if self.callback:
            self.callback(event)

    def wait(self, kind, predicate=lambda event: True):
        def matching():
            return next((event for event in self.events
                         if event["kind"] == kind and predicate(event)), None)
        with self.condition:
            assert self.condition.wait_for(matching, timeout=7), self.events
            return copy.deepcopy(matching())

    def request(self, step=None, after=None):
        return self.wait("cycle.node.approval", lambda event:
                         event["receipt"]["status"] == "pending"
                         and (step is None or event["receipt"]["step"] == step)
                         and (after is None or event["receipt"]["id"] != after))["receipt"]

    def requests(self):
        return [event["receipt"] for event in self.events
                if event["kind"] == "cycle.node.approval"
                and event["receipt"]["status"] == "pending"]


def answer(receipt, approved=True, **extra):
    raw = {"run_id": receipt["run_id"], "request_id": receipt["id"],
           "fingerprint": receipt["fingerprint"], "approved": approved}
    raw.update(extra)
    return executor.answer_node_approval(raw)


def approve_pending(event):
    if event["kind"] == "cycle.node.approval" and event["receipt"]["status"] == "pending":
        assert answer(event["receipt"]) == ""


@contextmanager
def live(tmp_path, definition, *, mode=None, plugin=None, callback=None,
         jobs=4, resume=False):
    plugin, observer, token = plugin or Plugin(), Events(callback), CancelToken()
    result = {}

    def run():
        try:
            result["run"] = executor.run_cycle(
                definition, str(tmp_path), registry=Plugins(plugin), observer=observer,
                cancel=token, run_id="policy-live", controlled=True, mode=mode,
                jobs=jobs, resume=resume)
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield observer, plugin, result
    finally:
        token.set("test cleanup")
        thread.join(7)
        assert not thread.is_alive(), observer.events
        assert "error" not in result, result


def policy_file(tmp_path):
    return tmp_path / ".execution" / "execution-policy.json"


@pytest.mark.parametrize("mode,expected", [
    ("auto", []), ("important", ["important"]),
    ("approve", ["normal", "important"]),
])
def test_mode_matrix_skips_inapplicable_nodes_and_approves_only_its_own_work(tmp_path, mode, expected):
    definition = workflow(node("normal"), node("important", important=True,
                          importance_reason="Writes a reviewed local result"),
                          node("disabled", disabled=True, important=True),
                          node("condition", important=True, **{"if": "1 == 0"}))
    with live(tmp_path, definition, mode=mode, callback=approve_pending) as (events, plugin, result):
        events.wait("cycle.run.end")
        assert sorted(plugin.calls) == ["important", "normal"]
        assert sorted(receipt["step"] for receipt in events.requests()) == sorted(expected)
        saved = checkpoints.Store(str(tmp_path)).load()
        assert saved.ok and saved.execution_mode == mode
        assert saved.initial_execution_mode == mode
        assert saved.steps["disabled"].status == saved.steps["condition"].status == "skipped"


@pytest.mark.parametrize("mode", ["auto", "important", "approve"])
def test_explicit_gate_keeps_one_domain_prompt_without_implicit_prompt(tmp_path, monkeypatch, mode):
    asked = []
    def domain_question(*args, **kwargs):
        asked.append(kwargs.get("step"))
        return {"answered": True, "answer": "Approve", "who": "test"}
    monkeypatch.setattr("cycle.ask.ask", domain_question)
    definition = workflow({"id": "gate", "plugin": "approval.gate", "important": True,
                           "with": {"question": "Publish this reviewed result?"}},
                          node("work", needs=["gate"]))
    with live(tmp_path, definition, mode=mode, callback=approve_pending) as (events, plugin, result):
        events.wait("cycle.run.end")
        assert len(asked) == 1 and plugin.calls == ["work"]
        assert all(receipt["step"] != "gate" for receipt in events.requests())


def test_cycle_default_is_used_but_explicit_launch_mode_overrides_it(tmp_path):
    definition = workflow(node("one", important=True), default_mode="important")
    first = executor.run_cycle(definition, str(tmp_path / "default"), registry=Plugins(Plugin()))
    assert first.execution_mode == first.initial_execution_mode == "important"
    assert first.paused and first.pause_reason == "approval_required"
    assert first.steps["one"].status == "pending"
    plugin = Plugin()
    second = executor.run_cycle(definition, str(tmp_path / "explicit"), registry=Plugins(plugin), mode="auto")
    assert second.ok and plugin.calls == ["one"]
    assert second.execution_mode == second.initial_execution_mode == "auto"


def test_legacy_cycle_keeps_auto_without_new_prompts(tmp_path):
    observer, plugin = Events(), Plugin()
    run = executor.run_cycle(workflow(node("legacy")), str(tmp_path),
                             registry=Plugins(plugin), observer=observer)
    assert run.ok and plugin.calls == ["legacy"] and not observer.requests()
    assert run.execution_mode == run.initial_execution_mode == "auto"


def test_parallel_ready_nodes_have_distinct_pending_cards_and_decisions(tmp_path):
    with live(tmp_path, workflow(node("a"), node("b")), mode="approve", jobs=2) as (events, plugin, result):
        first, second = events.request("a"), events.request("b")
        assert first["id"] != second["id"] and first["fingerprint"] != second["fingerprint"]
        waiting = events.wait("cycle.run.state", lambda event: event["action"] == "waiting_for_node_approval")
        assert waiting["execution_status"] == "waiting_for_node_approval"
        saved = checkpoints.Store(str(tmp_path)).load()
        assert saved.steps["a"].status == saved.steps["b"].status == "pending"
        assert not plugin.calls
        assert answer(first) == ""
        events.wait("cycle.step.end", lambda event: event["step"] == "a")
        assert plugin.calls == ["a"]
        assert answer(second) == ""
        events.wait("cycle.run.end")
        assert sorted(plugin.calls) == ["a", "b"]


def test_request_is_durable_before_event_and_decision_before_plugin_work(tmp_path):
    checks = []
    def inspect_event(event):
        if event["kind"] == "cycle.node.approval" and event["receipt"]["status"] == "pending":
            receipt = event["receipt"]
            saved = json.loads(policy_file(tmp_path).read_text())
            checks.append(saved["node_approvals"][receipt["id"]] == receipt)
            answer(receipt)
    def inspect_work(context, step):
        saved = json.loads(policy_file(tmp_path).read_text())
        matching = [receipt for receipt in saved["node_approvals"].values()
                    if receipt["step"] == step.id and receipt.get("approved") is True]
        assert len(matching) == 1
        assert matching[0]["decided_at"] >= matching[0]["created_at"]
        assert matching[0]["settings_digest"] == checkpoints.digest(step.settings)
        return registry.succeeded()
    with live(tmp_path, workflow(node("work", **{"with": {"value": "reviewed"}})),
              mode="approve", callback=inspect_event, plugin=Plugin(inspect_work)) as (events, plugin, result):
        events.wait("cycle.run.end")
        assert checks == [True]
        assert checkpoints.Store(str(tmp_path)).load().ok


def test_stale_fingerprint_is_refused_without_starting_node(tmp_path):
    with live(tmp_path, workflow(node("one")), mode="approve") as (events, plugin, result):
        receipt = events.request("one")
        refused = answer(receipt, fingerprint="0" * 64)
        if not refused:
            events.wait("cycle.control.refused", lambda event: event["action"] == "node_approval")
        assert not plugin.calls
        assert answer(receipt) == ""
        events.wait("cycle.run.end")
        assert plugin.calls == ["one"]


def test_changed_inputs_between_decision_and_submit_require_new_consent(tmp_path):
    definition = workflow(node("one", **{"with": {"value": "before"}}))
    first = {}
    def change_after_approval(event):
        if event["kind"] != "cycle.node.approval":
            return
        receipt = event["receipt"]
        if receipt["status"] == "pending" and not first:
            first.update(receipt)
            answer(receipt)
        elif receipt["status"] == "approved" and receipt["id"] == first.get("id"):
            definition.step("one").settings["value"] = "after"
    with live(tmp_path, definition, mode="approve", callback=change_after_approval) as (events, plugin, result):
        old = events.request("one")
        current = events.request("one", after=old["id"])
        assert current["fingerprint"] != old["fingerprint"] and not plugin.calls
        answer(current)
        events.wait("cycle.run.end")
        assert plugin.calls == ["one"]


@pytest.mark.parametrize("bad", [{"approved": "true"}, {"approved": 1},
                                 {"run_id": "another-run"}, {"request_id": "missing"}])
def test_invalid_or_foreign_answers_cannot_authorize_work(tmp_path, bad):
    with live(tmp_path, workflow(node("one")), mode="approve") as (events, plugin, result):
        receipt = events.request("one")
        refused = answer(receipt, **bad)
        if not refused:
            events.wait("cycle.control.refused", lambda event: event["action"] == "node_approval")
        assert not plugin.calls
        answer(receipt)
        events.wait("cycle.run.end")


def test_decline_is_a_safe_pause_and_continue_requires_a_new_decision(tmp_path):
    with live(tmp_path, workflow(node("one", important=True)), mode="important") as (events, plugin, result):
        first = events.request("one")
        assert answer(first, approved=False) == ""
        paused = events.wait("cycle.run.state", lambda event: event["action"] == "paused")
        assert paused["pause_reason"] == "approval_declined"
        saved = checkpoints.Store(str(tmp_path)).load()
        assert saved.paused and saved.execution_status == "paused" and saved.steps["one"].status == "pending"
        assert saved.steps["one"].started_at is None and not plugin.calls
        assert executor.control_run("policy-live", "continue") == ""
        next_request = events.request("one", after=first["id"])
        assert next_request["id"] != first["id"] and not plugin.calls
        answer(next_request)
        events.wait("cycle.run.end")
        assert plugin.calls == ["one"]


def test_decline_drains_an_active_branch_and_prevents_its_next_submission(tmp_path):
    started, release = threading.Event(), threading.Event()
    def work(context, step):
        if step.id == "active":
            started.set()
            assert release.wait(7)
        return registry.succeeded()
    definition = workflow(node("active"), node("protected", important=True),
                          node("next", needs=["active"]))
    try:
        with live(tmp_path, definition, mode="important", plugin=Plugin(work)) as (events, plugin, result):
            receipt = events.request("protected")
            assert started.wait(5)
            answer(receipt, approved=False)
            events.wait("cycle.run.state", lambda event: event["action"] == "pausing"
                        and event["pause_reason"] == "approval_declined")
            release.set()
            events.wait("cycle.run.state", lambda event: event["action"] == "paused")
            saved = checkpoints.Store(str(tmp_path)).load()
            assert plugin.calls == ["active"] and saved.steps["active"].status == "success"
            assert saved.steps["next"].status == saved.steps["protected"].status == "pending"
    finally:
        release.set()


def test_stop_while_awaiting_node_approval_cancels_pending_without_work(tmp_path):
    with live(tmp_path, workflow(node("one")), mode="approve") as (events, plugin, result):
        events.request("one")
        assert executor.request_stop("policy-live")
        events.wait("cycle.run.end")
        saved = checkpoints.Store(str(tmp_path)).load()
        assert saved.status == "cancelled" and saved.steps["one"].status == "cancelled"
        assert not plugin.calls


def test_mode_change_requires_pause_and_does_not_implicitly_continue(tmp_path):
    with live(tmp_path, workflow(node("one")), mode="approve") as (events, plugin, result):
        request = events.request("one")
        assert executor.control_run("policy-live", "set_mode", mode="auto") == ""
        events.wait("cycle.control.refused", lambda event: event["action"] == "set_mode")
        assert executor.control_run("policy-live", "pause") == ""
        events.wait("cycle.run.state", lambda event: event["action"] == "paused")
        assert executor.control_run("policy-live", "set_mode", mode="auto") == ""
        changed = events.wait("cycle.policy.changed")["receipt"]
        assert changed["from"] == "approve" and changed["to"] == "auto"
        assert request["id"] in changed["invalidated"]
        saved = json.loads(policy_file(tmp_path).read_text())
        assert saved["initial_execution_mode"] == "approve" and saved["execution_mode"] == "auto"
        assert saved["node_approvals"][request["id"]]["status"] == "invalidated"
        assert not plugin.calls and checkpoints.Store(str(tmp_path)).load().paused
        assert executor.control_run("policy-live", "continue") == ""
        events.wait("cycle.run.end")
        assert plugin.calls == ["one"]


def test_paused_policy_change_invalidates_an_approved_but_unstarted_node(tmp_path):
    def pause_after_approval(event):
        if event["kind"] != "cycle.node.approval":
            return
        if event["receipt"]["status"] == "pending":
            answer(event["receipt"])
        elif event["receipt"]["status"] == "approved":
            executor.control_run("policy-live", "pause")
    with live(tmp_path, workflow(node("one", important=True)), mode="approve",
              callback=pause_after_approval) as (events, plugin, result):
        old = events.request("one")
        events.wait("cycle.run.state", lambda event: event["action"] == "paused")
        assert not plugin.calls
        executor.control_run("policy-live", "set_mode", mode="important")
        changed = events.wait("cycle.policy.changed")["receipt"]
        assert old["id"] in changed["invalidated"]
        events.callback = None
        executor.control_run("policy-live", "continue")
        current = events.request("one", after=old["id"])
        assert current["mode"] == "important" and not plugin.calls
        answer(current)
        events.wait("cycle.run.end")
        assert plugin.calls == ["one"]


def test_unstarted_approved_receipt_recovers_from_stale_metadata_on_resume(tmp_path):
    definition, plugin = workflow(node("one")), Plugin()
    first = executor.run_cycle(definition, str(tmp_path), registry=Plugins(plugin),
                               run_id="policy-live", mode="approve")
    assert first.paused and not plugin.calls
    path = policy_file(tmp_path)
    policy = json.loads(path.read_text())
    receipt = next(iter(policy["node_approvals"].values()))
    receipt.update(status="approved", approved=True, decided_at=time.time())
    # A crash after the dedicated decision write, before the metadata update.
    path.write_text(json.dumps(policy))
    with live(tmp_path, definition, plugin=plugin, resume=True) as (events, plugin, result):
        events.wait("cycle.run.end")
        assert not events.requests() and plugin.calls == ["one"]
        saved = checkpoints.Store(str(tmp_path)).load()
        assert saved.ok and saved.id == first.id and saved.resume_count == 1


def test_changed_pending_inputs_invalidate_the_old_approval_on_resume(tmp_path):
    original = workflow(node("one", **{"with": {"value": "before"}}))
    first = executor.run_cycle(original, str(tmp_path), registry=Plugins(Plugin()),
                               run_id="policy-live", mode="approve")
    old = next(iter(json.loads(policy_file(tmp_path).read_text())["node_approvals"].values()))
    changed = workflow(node("one", **{"with": {"value": "after"}}))
    with live(tmp_path, changed, resume=True) as (events, plugin, result):
        current = events.request("one", after=old["id"])
        assert current["fingerprint"] != old["fingerprint"] and not plugin.calls
        refused = answer(old)
        if not refused:
            events.wait("cycle.control.refused", lambda event: event["action"] == "node_approval")
        assert not plugin.calls
        answer(current)
        events.wait("cycle.run.end")


def test_failed_resume_needs_new_consent_but_keeps_successful_work(tmp_path):
    attempts = []
    def fail_once(context, step):
        if step.id == "flaky":
            attempts.append(step.id)
            if len(attempts) == 1:
                return registry.failed("temporary synthetic failure")
        return registry.succeeded()
    definition = workflow(node("paid"), node("flaky", needs=["paid"]))
    plugin = Plugin(fail_once)
    with live(tmp_path, definition, mode="approve", plugin=plugin,
              callback=approve_pending) as (events, plugin, result):
        events.wait("cycle.run.end")
        old = events.request("flaky")
    with live(tmp_path, definition, plugin=plugin, resume=True) as (events, plugin, result):
        current = events.request("flaky")
        assert current["id"] != old["id"] and current["fingerprint"] != old["fingerprint"]
        assert [receipt["step"] for receipt in events.requests()] == ["flaky"]
        assert plugin.calls == ["paid", "flaky"]
        answer(current)
        events.wait("cycle.run.end")
        assert plugin.calls == ["paid", "flaky", "flaky"]
        assert checkpoints.Store(str(tmp_path)).load().ok


def test_retry_phase_requires_new_consent_without_reapproving_successful_nodes(tmp_path):
    attempts = []
    def fail_once(context, step):
        if step.id == "flaky":
            attempts.append(step.id)
            if len(attempts) == 1:
                return registry.failed("temporary synthetic failure")
        return registry.succeeded()
    definition = model.parse_cycle({"id": "policy", "phases": [{"id": "work"}],
                                    "steps": [node("paid", phase="work"),
                                              node("flaky", phase="work")]}, "policy")
    with live(tmp_path, definition, mode="approve", plugin=Plugin(fail_once),
              callback=approve_pending) as (events, plugin, result):
        events.wait("cycle.run.state", lambda event: event["action"] == "paused"
                    and event["pause_reason"] == "phase_failed")
        old = events.request("flaky")
        events.callback = None
        executor.control_run("policy-live", "retry_phase", "work")
        current = events.request("flaky", after=old["id"])
        assert current["execution_generation"] == old["execution_generation"] + 1
        assert current["fingerprint"] != old["fingerprint"]
        assert sorted(plugin.calls) == ["flaky", "paid"]
        assert len([receipt for receipt in events.requests() if receipt["step"] == "paid"]) == 1
        answer(current)
        events.wait("cycle.run.end")
        assert plugin.calls.count("paid") == 1 and plugin.calls.count("flaky") == 2
        assert checkpoints.Store(str(tmp_path)).load().ok


def test_configured_plugin_retries_belong_to_one_approved_logical_execution(tmp_path):
    calls = []
    def retry(context, step):
        calls.append(step.id)
        return registry.failed("try again") if len(calls) == 1 else registry.succeeded()
    definition = workflow(node("one", retry={"attempts": 2, "delay": 0}))
    with live(tmp_path, definition, mode="approve", plugin=Plugin(retry),
              callback=approve_pending) as (events, plugin, result):
        events.wait("cycle.run.end")
        assert len(events.requests()) == 1 and calls == ["one", "one"]
        assert checkpoints.Store(str(tmp_path)).load().steps["one"].attempts == 2


def test_parked_plugin_polls_keep_the_same_logical_approval(tmp_path):
    calls = []
    def poll(context, step):
        calls.append(step.id)
        return registry.waiting(0.001, "Still looking") if len(calls) == 1 else registry.succeeded()
    with live(tmp_path, workflow(node("one", timeout=5)), mode="approve",
              plugin=Plugin(poll), callback=approve_pending) as (events, plugin, result):
        events.wait("cycle.run.end")
        assert len(events.requests()) == 1 and calls == ["one", "one"]
        saved = checkpoints.Store(str(tmp_path)).load()
        assert saved.ok and saved.steps["one"].attempts == 2


def test_changed_parked_poll_inputs_require_approval_before_its_next_call(tmp_path):
    definition = workflow(node("one", timeout=5, **{"with": {"target": "original"}}))
    seen, first = [], {}
    def approve_first(event):
        if event["kind"] == "cycle.node.approval" and event["receipt"]["status"] == "pending" and not first:
            first.update(event["receipt"])
            answer(event["receipt"])
    def poll(context, step):
        seen.append(step.settings["target"])
        if len(seen) == 1:
            definition.step("one").settings["target"] = "changed"
            return registry.waiting(0.001, "Poll the same logical operation")
        return registry.succeeded()
    with live(tmp_path, definition, mode="approve", plugin=Plugin(poll),
              callback=approve_first) as (events, plugin, result):
        old = events.request("one")
        current = events.request("one", after=old["id"])
        assert seen == ["original"] and current["fingerprint"] != old["fingerprint"]
        assert current["attempt"] == old["attempt"] + 1
        answer(current)
        events.wait("cycle.run.end")
        assert seen == ["original", "changed"]
        assert checkpoints.Store(str(tmp_path)).load().ok


def test_paused_mode_change_preserves_an_already_started_unchanged_poll(tmp_path):
    calls = []
    def poll(context, step):
        calls.append(step.id)
        return registry.waiting(0.001, "Wait briefly") if len(calls) == 1 else registry.succeeded()
    def pause_parked(event):
        if event["kind"] == "cycle.step.waiting":
            executor.control_run("policy-live", "pause")
    with live(tmp_path, workflow(node("one", important=True, timeout=5)), mode="auto",
              plugin=Plugin(poll), callback=pause_parked) as (events, plugin, result):
        events.wait("cycle.run.state", lambda event: event["action"] == "paused")
        assert calls == ["one"]
        executor.control_run("policy-live", "set_mode", mode="approve")
        events.wait("cycle.policy.changed")
        executor.control_run("policy-live", "continue")
        events.wait("cycle.run.end")
        assert calls == ["one", "one"] and not events.requests()
        assert checkpoints.Store(str(tmp_path)).load().ok


@pytest.mark.parametrize("fail_on_decision", [False, True])
def test_receipt_write_failure_cannot_start_protected_work(tmp_path, monkeypatch, fail_on_decision):
    original = checkpoints.Store.write
    def write(self, path, data):
        if str(path).endswith("execution-policy.json"):
            receipts = list(data.get("node_approvals", {}).values())
            if any((receipt.get("approved") is True) if fail_on_decision
                   else receipt.get("status") == "pending" for receipt in receipts):
                raise checkpoints.CheckpointError("Cannot save execution state: synthetic full disk")
        return original(self, path, data)
    monkeypatch.setattr(checkpoints.Store, "write", write)
    with live(tmp_path, workflow(node("one")), mode="approve",
              callback=approve_pending) as (events, plugin, result):
        events.wait("cycle.run.end")
        assert not plugin.calls
        saved = json.loads((tmp_path / "metadata.json").read_text())
        assert saved["status"] == "failed" and "synthetic full disk" in saved["message"]
