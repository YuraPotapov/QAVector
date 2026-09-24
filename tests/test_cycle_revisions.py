"""Human feedback revisits a saved plan, never the whole paid workflow."""

import json
from pathlib import Path

import pytest

from cycle import ask, checkpoints, executor, model, registry
from cycle.operations import Operation
from domain.cycle import Artifact, PENDING, RUNNING, SUCCESS


class Services:
    def __init__(self):
        self.calls = []
        self.charged = []
        self.written = []
        outer = self

        class Review(registry.CyclePlugin):
            metadata = registry.PluginMetadata(id="agent.review", name="Review", recoverable=True)

            def execute(self, context, step):
                outer.calls.append(step.id)
                call = Operation(context, step, "review", step.settings)
                answer = call.cached()
                if answer is None:
                    call.begin()
                    outer.charged.append(step.id)
                    feedback = step.settings.get("inputs", {}).get("user_revision", {})
                    answer = {"summary": "revised: " + feedback["feedback"] if feedback
                              else "original plan" if step.id == "plan" else
                              "reviewed: " + step.settings["inputs"]["plan"]}
                    call.complete(answer, True)
                path = Path(context.step_dir(step.id), "review.json")
                path.write_text(json.dumps(answer))
                return registry.PluginResult(SUCCESS, outputs=answer, artifacts=[
                    Artifact("json", context.relative(str(path)), step.id)])

        class Command(registry.CyclePlugin):
            metadata = registry.PluginMetadata(id="command.shell", name="Local fake")

            def execute(self, context, step):
                outer.calls.append(step.id)
                if step.id == "write":
                    outer.written.append(step.settings["plan"])
                return registry.succeeded(task="the task")

        self.review, self.command = Review(), Command()

    def get(self, name):
        if name == "agent.review":
            return self.review
        if name == "command.shell":
            return self.command
        return registry.get(name)


def workflow(limit=3):
    return model.parse_cycle({"id": "demo", "steps": [
        {"id": "task", "plugin": "command.shell"},
        {"id": "plan", "plugin": "agent.review", "needs": ["task"],
         "with": {"task": "Plan the work", "inputs": {"task": "${steps.task.outputs.task}"}}},
        {"id": "review", "plugin": "agent.review", "needs": ["plan"],
         "with": {"task": "Check the plan", "inputs": {"plan": "${steps.plan.outputs.summary}"}}},
        {"id": "approve", "plugin": "approval.gate", "needs": ["review", "plan"],
         "with": {"question": "Start work?", "detail": "${steps.plan.outputs.summary}",
                  "revision_step": "plan", "max_revisions": limit}},
        {"id": "write", "plugin": "command.shell", "needs": ["approve", "plan"],
         "with": {"plan": "${steps.plan.outputs.summary}"}},
    ]}, "demo")


def answers(monkeypatch, feedbacks=("Cover the fallback",)):
    pending = list(feedbacks)
    seen = []

    def answer(question, **kwargs):
        seen.append(kwargs)
        if pending:
            return {"answered": True, "revision_requested": True,
                    "feedback": pending.pop(0), "who": "reviewer"}
        return {"answered": True, "answer": "Approve", "who": "reviewer"}

    monkeypatch.setattr(ask, "ask", answer)
    return seen


def test_feedback_revises_only_the_plan_and_review_before_asking_again(tmp_path, monkeypatch):
    services, cycle = Services(), workflow()
    seen = answers(monkeypatch)
    result = executor.run_cycle(cycle, str(tmp_path / "run"), registry=services)
    assert result.ok, result.message
    assert services.calls == ["task", "plan", "review", "plan", "review", "write"]
    assert services.charged == ["plan", "review", "plan", "review"]
    assert services.written == ["revised: Cover the fallback"]
    assert [one["detail"] for one in seen] == ["original plan", "revised: Cover the fallback"]
    assert seen[1]["revision"]["remaining"] == 2
    assert result.steps["plan"].attempts == 2
    assert result.revision_inputs["plan"]["previous_plan"]["summary"] == "original plan"
    archive = tmp_path / "run/.execution/revisions/0001"
    assert json.loads((archive / "steps/plan/review.json").read_text())["summary"] == "original plan"
    assert json.loads((archive / "feedback.json").read_text())["feedback"] == "Cover the fallback"
    assert not (tmp_path / "run/.execution/transition.json").exists()


def test_multiple_rounds_keep_feedback_history_and_do_not_rebuy_the_task(tmp_path, monkeypatch):
    services = Services()
    answers(monkeypatch, ("Cover the fallback", "Also cover retries"))
    result = executor.run_cycle(workflow(), str(tmp_path / "run"), registry=services)
    assert result.ok, result.message
    assert services.calls.count("task") == 1
    assert services.calls.count("plan") == 3
    assert services.calls.count("write") == 1
    assert result.revision_inputs["plan"]["previous_feedback"] == ["Cover the fallback"]
    assert len(result.revisions) == 2


def test_revision_limit_does_not_allow_unbounded_agent_calls(tmp_path, monkeypatch):
    services = Services()
    seen = answers(monkeypatch, ("First request", "Another request"))
    result = executor.run_cycle(workflow(limit=1), str(tmp_path / "run"), registry=services)
    assert not result.ok
    assert not seen[-1]["revision"]["enabled"]
    assert "limit" in result.steps["approve"].message
    assert services.calls.count("plan") == 2
    assert services.written == []


def test_interrupted_revision_reset_recovers_feedback_before_any_new_paid_work(tmp_path, monkeypatch):
    services, cycle = Services(), workflow()
    seen = answers(monkeypatch)
    materialize = checkpoints.Store._materialize
    failed = []

    def fail_once(self, transition):
        if not failed:
            failed.append(True)
            # Imitate a power loss after only the first reset receipt reached disk.
            name = transition["changed"][0]
            self.write(str(Path(self.directory, "steps", name + ".json")),
                       transition["run"]["steps"][name])
            raise checkpoints.CheckpointError("Cannot save execution state: interrupted reset")
        return materialize(self, transition)

    monkeypatch.setattr(checkpoints.Store, "_materialize", fail_once)
    first = executor.run_cycle(cycle, str(tmp_path / "run"), registry=services)
    assert not first.ok
    assert services.charged == ["plan", "review"]
    final = executor.run_cycle(cycle, first.workspace, registry=services, resume=True)
    assert final.ok, final.message
    assert services.calls.count("task") == 1
    assert services.charged == ["plan", "review", "plan", "review"]
    assert services.written == ["revised: Cover the fallback"]
    assert seen[-1]["detail"] == "revised: Cover the fallback"


def test_resume_after_interrupted_approval_keeps_the_revised_plan(tmp_path, monkeypatch):
    services, cycle = Services(), workflow()
    answers(monkeypatch)
    first = executor.run_cycle(cycle, str(tmp_path / "run"), registry=services)
    first.steps["approve"].status = RUNNING
    first.steps["write"].status = PENDING
    store = checkpoints.Store(first.workspace)
    store.step(first, first.steps["approve"])
    store.step(first, first.steps["write"])
    final = executor.run_cycle(cycle, first.workspace, registry=services, resume=True)
    assert final.ok, final.message
    assert services.calls.count("plan") == 2
    assert services.calls.count("review") == 2
    assert services.written == ["revised: Cover the fallback"] * 2


@pytest.mark.parametrize("unsafe", ["agent.edit", "command.shell", "git.commit"])
def test_revision_cannot_replay_mutating_work(unsafe):
    cycle = workflow()
    cycle.step("review").plugin = unsafe
    assert any("read-only" in message for message in model.problems(cycle))


def test_revision_target_must_be_an_upstream_review():
    cycle = workflow()
    cycle.step("approve").settings["revision_step"] = "write"
    assert any("agent.review" in message for message in model.problems(cycle))
