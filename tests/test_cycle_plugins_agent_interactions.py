"""Agent adapters keep message delivery consistent with operation receipts."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cycle import checkpoints, interactions, model, registry
from cycle.context import RunContext
from cycle.plugins import agent, agent_run
from domain.cycle import CycleRun, StepRun


TASK = "Review the original task and keep the required JSON answer."
NOTE = "This method is also called from cron."
REVIEW = {"summary": "Checked", "risk": "low", "issues": [], "recommendations": []}


@pytest.fixture(params=["cli", "worker"])
def adapter(request, tmp_path, monkeypatch):
    kind = request.param
    cycle = model.parse_cycle({"id": "demo", "steps": [{
        "id": "review", "plugin": "agent.review", "with": {
            "framework": "crewai", "python": "/fake/python", "model": "provider/model",
            "task": TASK}}]}, "demo")
    step = cycle.steps[0]
    run = CycleRun(id="r", cycle_id="demo", workspace=str(tmp_path), steps={
        "review": StepRun("review", plugin="agent.review", status="running")})
    context = RunContext(run, cycle, str(tmp_path))
    fixture = SimpleNamespace(kind=kind, context=context, calls=[], outcome="success")

    def reload():
        store = interactions.RunInteractions(str(tmp_path / ".execution"), "r",
                                              workspace=str(tmp_path))
        store.load()
        store.open(run, cycle, registry)
        store.begin("review", 1, registry.get("agent.review"))
        context.interactions = fixture.store = store

    def send(message_id="m1", text=NOTE):
        message, problem = interactions.parse_request({
            "id": message_id, "run_id": "r", "scope": "step", "step": "review",
            "execution": 1, "kind": "text", "payload": {"text": text}})
        assert not problem
        record, problem = fixture.store.accept(message)
        assert not problem
        return record

    def process(ctx, one, argv, cwd, name, **kwargs):
        prompt = (argv[argv.index("-p") + 1] if kind == "cli" else
                  json.loads(Path(argv[-2]).read_text())["task"])
        fixture.calls.append(prompt)
        if fixture.outcome == "not_started":
            return registry.failed("missing executable", started=False)
        if fixture.outcome == "no_result":
            return registry.failed("connection lost after starting")
        if kind == "cli":
            Path(ctx.step_dir(one.id), "stdout.log").write_text(json.dumps({
                "type": "result", "is_error": False, "result": json.dumps(REVIEW)}))
        else:
            Path(argv[-1]).write_text(json.dumps(REVIEW))
        return registry.succeeded()

    monkeypatch.setattr(agent_run if kind == "cli" else agent, "run_process", process)

    def invoke():
        if kind == "cli":
            return agent_run.run_claude(context, step, TASK, str(tmp_path),
                                        agent_run.READ_TOOLS, "/fake/claude",
                                        clarify=True, last=True)
        return agent.AgentReview().execute(context, step)

    fixture.invoke, fixture.reload, fixture.send = invoke, reload, send
    reload()
    send()
    return fixture


def test_cached_spawn_failure_never_claims_message_delivery(adapter):
    adapter.outcome = "not_started"
    assert not adapter.invoke().ok
    assert not adapter.invoke().ok
    assert len(adapter.calls) == 1
    [record] = adapter.store.messages()
    assert record["state"] == "pending" and record["deliveries"] == []
    adapter.store.finish("review", "failed")
    assert adapter.store.messages()[0]["state"] == "undelivered"


@pytest.mark.parametrize("outcome, expected", [("success", "delivered"),
                                               ("not_started", "pending")])
def test_crash_after_receipt_recovers_delivery_without_another_call(
        adapter, monkeypatch, outcome, expected):
    adapter.outcome = outcome

    def crash(*_args, **_kwargs):
        raise RuntimeError("crash before delivery acknowledgement")

    method = "delivered" if outcome == "success" else "release"
    with monkeypatch.context() as patch:
        patch.setattr(interactions.Inbox, method, crash)
        with pytest.raises(RuntimeError, match="before delivery acknowledgement"):
            adapter.invoke()
    [receipt_path] = Path(adapter.context.workspace).glob(
        "steps/review/operations/*/receipt.json")
    receipt = json.loads(receipt_path.read_text())
    assert receipt["state"] == "complete"
    assert receipt["input_delivered"] is (outcome == "success")
    adapter.reload()
    assert adapter.store.messages()[0]["state"] == expected
    adapter.invoke()
    assert len(adapter.calls) == 1
    assert adapter.store.messages()[0]["state"] == expected


def test_missing_result_is_uncertain_and_resume_does_not_launch_again(adapter):
    adapter.outcome = "no_result"
    assert not adapter.invoke().ok
    adapter.store.finish("review", "failed")
    assert adapter.store.messages()[0]["state"] == "uncertain"
    adapter.reload()
    with pytest.raises(checkpoints.CheckpointError, match="uncertain outcome"):
        adapter.invoke()
    assert len(adapter.calls) == 1
    [record] = adapter.store.messages()
    assert record["state"] == "uncertain"
    assert record["deliveries"][0]["state"] == "uncertain"


def test_resume_keeps_original_prompt_and_defers_messages_after_its_snapshot(adapter):
    assert adapter.invoke().ok
    prompt = adapter.calls[0]
    assert prompt.startswith(TASK)
    assert prompt.index(TASK) < prompt.index(agent_run.CLARIFICATIONS) < prompt.index(NOTE)
    adapter.reload()
    adapter.send("m2", "Check timezone handling as well.")
    assert adapter.invoke().ok
    assert adapter.calls == [prompt]
    records = {one["id"]: one for one in adapter.store.messages()}
    assert records["m1"]["state"] == "delivered"
    assert records["m2"]["state"] == "pending"
    adapter.store.finish("review", "success")
    assert adapter.store.messages()[1]["state"] == "undelivered"
