"""A local failure must not buy the completed part of a DAG again."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cycle import checkpoints, executor, model, registry
from cycle.operations import Operation
from domain.cycle import Artifact, FAILED, SUCCESS


class Plugin(registry.CyclePlugin):
    def __init__(self, action, reusable=True, recoverable=False):
        self.metadata = registry.PluginMetadata(id="test.node", name="Node",
                                                reusable=reusable, recoverable=recoverable)
        self.action, self.calls = action, []

    def execute(self, context, step):
        self.calls.append(step.id)
        return self.action(context, step)


class Plugins:
    def __init__(self, **plugins):
        self.plugins = plugins

    def get(self, name):
        return self.plugins.get(name, registry.get(name))


def node(name, **extra):
    return dict(id=name, plugin=name, **extra)


def workflow(*steps, **extra):
    return model.parse_cycle(dict(id="demo", steps=list(steps), **extra), "demo")


def run(cycle, plugins, tmp_path, **kwargs):
    return executor.run_cycle(cycle, str(tmp_path / "run"), registry=plugins, **kwargs)


def test_writer_failure_keeps_paid_results_across_repeated_resumes(tmp_path):
    paid = Plugin(lambda c, s: registry.succeeded(plan="already paid for"))
    writer = Plugin(lambda c, s: registry.failed("disk unavailable"))
    plugins = Plugins(paid=paid, writer=writer)
    cycle = workflow(node("paid"), node("writer", needs=["paid"],
                                       **{"with": {"plan": "${steps.paid.outputs.plan}"}}))
    first = run(cycle, plugins, tmp_path)
    second = run(cycle, plugins, tmp_path, resume=True)
    writer.action = lambda c, s: registry.succeeded(written=s.settings["plan"])
    final = run(cycle, plugins, tmp_path, resume=True)

    assert first.status == second.status == FAILED
    assert final.status == SUCCESS
    assert final.id == first.id and final.resume_count == 2
    assert paid.calls == ["paid"]
    assert writer.calls == ["writer"] * 3
    assert final.steps["writer"].outputs["written"] == "already paid for"
    assert final.steps["writer"].attempts == 3
    assert len(list((tmp_path / "run/.execution/history").glob("*.json"))) == 2


def test_failure_in_one_parallel_branch_does_not_rebuy_the_other(tmp_path):
    a = Plugin(lambda c, s: registry.succeeded(answer=1))
    b = Plugin(lambda c, s: registry.failed("unavailable"))
    join = Plugin(lambda c, s: registry.succeeded())
    plugins = Plugins(a=a, b=b, join=join)
    cycle = workflow(node("a"), node("b", on_failure="continue"),
                     node("join", needs=["a", "b"]))
    run(cycle, plugins, tmp_path)
    b.action = lambda c, s: registry.succeeded(answer=2)
    assert run(cycle, plugins, tmp_path, resume=True).ok
    assert a.calls == ["a"] and b.calls == ["b", "b"] and join.calls == ["join"]


def test_node_finishes_from_service_receipt_when_its_file_write_failed(tmp_path):
    charged, writes = [], []

    def action(context, step):
        call = Operation(context, step, "provider", {"prompt": "expensive"})
        answer = call.cached()
        if answer is None:
            call.begin()
            charged.append(1)
            answer = {"plan": "the provider's response"}
            call.complete(answer, True)
        writes.append(1)
        if len(writes) == 1:
            raise OSError("could not write report")
        return registry.succeeded(**answer)

    paid = Plugin(action, recoverable=True)
    cycle, plugins = workflow(node("paid")), Plugins(paid=paid)
    assert not run(cycle, plugins, tmp_path).ok
    assert run(cycle, plugins, tmp_path, resume=True).ok
    assert charged == [1] and len(writes) == 2


@pytest.mark.parametrize("changed", [False, True])
def test_unknown_service_outcome_is_not_called_again(tmp_path, changed):
    charged = []

    def action(context, step):
        call = Operation(context, step, "provider", step.settings)
        call.begin()
        charged.append(1)
        raise RuntimeError("connection lost after request was sent")

    paid = Plugin(action, recoverable=True)
    cycle, plugins = workflow(node("paid", **{"with": {"prompt": "expensive"}})), Plugins(paid=paid)
    run(cycle, plugins, tmp_path)
    if changed:
        cycle.step("paid").settings["prompt"] = "different request"
    with pytest.raises(checkpoints.CheckpointError, match="Uncertain outcome"):
        run(cycle, plugins, tmp_path, resume=True)
    assert charged == [1]


def test_interrupted_recoverable_node_can_finish_a_recorded_response(tmp_path):
    charged = []

    def action(context, step):
        operation = Operation(context, step, "provider", {"input": "task"})
        answer = operation.cached()
        if answer is None:
            operation.begin()
            charged.append(1)
            answer = {"answer": "saved"}
            operation.complete(answer, True)
            raise OSError("local writer failed")
        return registry.succeeded(**answer)

    paid = Plugin(action, recoverable=True)
    cycle, plugins = workflow(node("paid")), Plugins(paid=paid)
    record = run(cycle, plugins, tmp_path)
    record.steps["paid"].status = "running"
    checkpoints.Store(record.workspace).step(record, record.steps["paid"])
    assert run(cycle, plugins, tmp_path, resume=True).ok
    assert charged == [1]


def test_known_failed_call_receipt_and_cost_are_archived_before_explicit_retry(tmp_path):
    def action(context, step):
        operation = Operation(context, step, "provider", {"input": "task"})
        operation.begin()
        operation.complete({"total_cost_usd": 0.7, "error": "max turns"}, False)
        return registry.failed("max turns")

    paid = Plugin(action, recoverable=True)
    cycle, plugins = workflow(node("paid")), Plugins(paid=paid)
    run(cycle, plugins, tmp_path)
    run(cycle, plugins, tmp_path, resume=True)
    archived, = (tmp_path / "run/steps/paid/operations").glob("*/history/0000.json")
    assert json.loads(archived.read_text())["answer"]["total_cost_usd"] == 0.7


def test_service_receipt_failure_stops_even_a_continue_node_before_retry(tmp_path, monkeypatch):
    charged = []

    def action(context, step):
        operation = Operation(context, step, "provider", {})
        operation.begin()
        charged.append(1)
        operation.complete({"plan": "paid"}, True)
        return registry.succeeded()

    write = checkpoints.Store.write

    def refuse_response(self, path, data):
        if str(path).endswith("receipt.json") and data.get("state") == "complete":
            raise checkpoints.CheckpointError("Cannot save execution state: full disk")
        return write(self, path, data)

    monkeypatch.setattr(checkpoints.Store, "write", refuse_response)
    paid, after = Plugin(action), Plugin(lambda c, s: registry.succeeded())
    cycle = workflow(node("paid", retry={"attempts": 3}, on_failure="continue"),
                     node("after", needs=["paid"], **{"if": "${run.id}"}))
    record = run(cycle, Plugins(paid=paid, after=after), tmp_path)
    assert not record.ok
    assert charged == [1] and paid.calls == ["paid"] and after.calls == []


@pytest.mark.parametrize("change", ["settings", "variable", "plugin_version"])
def test_changed_completed_inputs_refuse_before_any_service_runs(tmp_path, change):
    paid = Plugin(lambda c, s: registry.succeeded())
    bad = Plugin(lambda c, s: registry.failed("fail"))
    plugins = Plugins(paid=paid, bad=bad)
    cycle = workflow(node("paid", **{"with": {"task": "${vars.task}"}}),
                     node("bad", needs=["paid"]), variables={"task": "original"})
    run(cycle, plugins, tmp_path)
    if change == "settings":
        cycle.step("paid").settings["task"] = "changed"
    elif change == "variable":
        cycle.variables["task"] = "changed"
    else:
        paid.metadata = registry.PluginMetadata(id="test.node", name="Node", version="2")
    with pytest.raises(checkpoints.CheckpointError, match="Completed steps changed"):
        run(cycle, plugins, tmp_path, resume=True)
    assert paid.calls == ["paid"] and bad.calls == ["bad"]


def test_failed_node_limit_can_change_without_invalidating_completed_plan(tmp_path):
    paid = Plugin(lambda c, s: registry.succeeded())
    bad = Plugin(lambda c, s: registry.failed("max turns") if s.settings["limit"] == 1
                 else registry.succeeded())
    plugins = Plugins(paid=paid, bad=bad)
    cycle = workflow(node("paid"), node("bad", needs=["paid"], **{"with": {"limit": 1}}))
    run(cycle, plugins, tmp_path)
    cycle.step("bad").settings["limit"] = 2
    assert run(cycle, plugins, tmp_path, resume=True).ok
    assert paid.calls == ["paid"]


def test_partial_run_keeps_original_definition_and_result_provenance(tmp_path):
    paid = Plugin(lambda c, s: registry.succeeded(plan="saved"))
    writer = Plugin(lambda c, s: registry.failed("disk"))
    cycle = workflow(node("paid"), node("writer", needs=["paid"]))
    plugins = Plugins(paid=paid, writer=writer)
    previous = run(cycle, plugins, tmp_path)
    partial = executor.run_cycle(cycle, str(tmp_path / "partial"), registry=plugins,
                                 only={"writer"}, reuse=previous)
    assert partial.steps["paid"].source_run == previous.id
    assert partial.steps["paid"].definition_digest == previous.steps["paid"].definition_digest
    writer.action = lambda c, s: registry.succeeded()
    assert executor.run_cycle(cycle, partial.workspace, registry=plugins, resume=True).ok
    assert paid.calls == ["paid"]


def test_partial_run_refuses_changed_completed_definition_before_work(tmp_path):
    paid = Plugin(lambda c, s: registry.succeeded())
    writer = Plugin(lambda c, s: registry.failed("disk"))
    cycle = workflow(node("paid"), node("writer", needs=["paid"]))
    plugins = Plugins(paid=paid, writer=writer)
    previous = run(cycle, plugins, tmp_path)
    cycle.step("paid").settings["prompt"] = "another task"
    with pytest.raises(checkpoints.CheckpointError, match="Saved settings for paid changed"):
        executor.run_cycle(cycle, str(tmp_path / "partial"), registry=plugins,
                           only={"writer"}, reuse=previous)
    assert writer.calls == ["writer"]


def test_partial_run_does_not_invent_provenance_for_legacy_results(tmp_path):
    paid = Plugin(lambda c, s: registry.succeeded())
    writer = Plugin(lambda c, s: registry.failed("disk"))
    cycle = workflow(node("paid"), node("writer", needs=["paid"]))
    plugins = Plugins(paid=paid, writer=writer)
    previous = run(cycle, plugins, tmp_path)
    previous.steps["paid"].definition_digest = ""  # pre-manifest saved result
    partial = executor.run_cycle(cycle, str(tmp_path / "partial"), registry=plugins,
                                 only={"writer"}, reuse=previous)
    assert partial.steps["paid"].definition_digest == ""


def test_an_imported_legacy_result_is_resumable_against_the_run_s_own_manifest(tmp_path):
    """Every run descended from a pre-manifest one used to be unresumable."""
    paid = Plugin(lambda c, s: registry.succeeded())
    writer = Plugin(lambda c, s: registry.failed("disk"))
    cycle = workflow(node("paid"), node("writer", needs=["paid"]))
    plugins = Plugins(paid=paid, writer=writer)
    previous = run(cycle, plugins, tmp_path)
    previous.steps["paid"].definition_digest = ""
    partial = executor.run_cycle(cycle, str(tmp_path / "partial"), registry=plugins,
                                 only={"writer"}, reuse=previous)
    writer.action = lambda c, s: registry.succeeded()

    assert executor.run_cycle(cycle, partial.workspace, registry=plugins, resume=True).ok
    assert paid.calls == ["paid"], "the imported paid result is not bought again"


def test_a_step_run_again_can_see_what_it_did_last_time(tmp_path):
    """Resume and Run from here both hand it over; nothing else changes."""
    seen = []

    def failing(context, step):
        seen.append(context.earlier_of(step.id))
        return registry.PluginResult("failed", outputs={"tree": "abc"}, message="bad check")

    paid = Plugin(lambda c, s: registry.succeeded())
    writer = Plugin(failing)
    cycle = workflow(node("paid"), node("writer", needs=["paid"]))
    plugins = Plugins(paid=paid, writer=writer)
    first = run(cycle, plugins, tmp_path)
    assert seen[-1] == (None, None)

    run(cycle, plugins, tmp_path, resume=True)
    source, was = seen[-1]
    assert source == first.id and was.outputs == {"tree": "abc"}

    executor.run_cycle(cycle, str(tmp_path / "partial"), registry=plugins,
                       only={"writer"}, reuse=first)
    source, was = seen[-1]
    assert source == first.id and was.outputs == {"tree": "abc"}


def test_an_imported_legacy_result_whose_settings_changed_still_refuses(tmp_path):
    paid = Plugin(lambda c, s: registry.succeeded())
    writer = Plugin(lambda c, s: registry.failed("disk"))
    cycle = workflow(node("paid"), node("writer", needs=["paid"]))
    plugins = Plugins(paid=paid, writer=writer)
    previous = run(cycle, plugins, tmp_path)
    previous.steps["paid"].definition_digest = ""
    partial = executor.run_cycle(cycle, str(tmp_path / "partial"), registry=plugins,
                                 only={"writer"}, reuse=previous)
    cycle.step("paid").settings["prompt"] = "another task"

    with pytest.raises(checkpoints.CheckpointError, match="Completed steps changed: paid"):
        executor.run_cycle(cycle, partial.workspace, registry=plugins, resume=True)
    assert paid.calls == ["paid"]


def test_skipped_optional_branch_does_not_block_resumption(tmp_path):
    paid = Plugin(lambda c, s: registry.succeeded())
    bad = Plugin(lambda c, s: registry.failed("fail"))
    report = Plugin(lambda c, s: registry.succeeded())
    plugins = Plugins(paid=paid, bad=bad, optional=paid, report=report)
    cycle = workflow(node("paid"), node("bad", needs=["paid"], on_failure="continue"),
                     node("optional", needs=["paid"], **{"if": "'yes' == 'no'"}),
                     node("report", needs=["bad", "optional"], **{"if": "${run.id}"}))
    run(cycle, plugins, tmp_path)
    bad.action = lambda c, s: registry.succeeded()
    final = run(cycle, plugins, tmp_path, resume=True)
    assert final.ok and final.steps["optional"].status == "skipped"
    assert paid.calls == ["paid"]


def test_approval_is_refreshed_without_replaying_completed_paid_work(tmp_path):
    paid = Plugin(lambda c, s: registry.succeeded())
    approve = Plugin(lambda c, s: registry.succeeded(), reusable=False)
    writer = Plugin(lambda c, s: registry.failed("disk"))
    plugins = Plugins(paid=paid, approve=approve, work=paid, writer=writer)
    cycle = workflow(node("paid"), node("approve", needs=["paid"]),
                     node("work", needs=["approve"]), node("writer", needs=["work"]))
    run(cycle, plugins, tmp_path)
    writer.action = lambda c, s: registry.succeeded()
    assert run(cycle, plugins, tmp_path, resume=True).ok
    assert paid.calls == ["paid", "work"]
    assert approve.calls == ["approve", "approve"]


def test_refused_fresh_approval_blocks_work_behind_a_cached_intermediate(tmp_path):
    paid = Plugin(lambda c, s: registry.succeeded())
    approve = Plugin(lambda c, s: registry.succeeded(), reusable=False)
    writer = Plugin(lambda c, s: registry.failed("disk"))
    plugins = Plugins(approve=approve, paid=paid, writer=writer)
    cycle = workflow(node("approve", on_failure="continue"),
                     node("paid", needs=["approve"]), node("writer", needs=["paid"]))
    run(cycle, plugins, tmp_path)
    approve.action = lambda c, s: registry.failed("declined")
    resumed = run(cycle, plugins, tmp_path, resume=True)
    assert resumed.steps["writer"].status == "skipped"
    assert writer.calls == ["writer"] and paid.calls == ["paid"]


def test_changed_fresh_input_stops_consumers_without_rebuying_cached_result(tmp_path):
    fresh = Plugin(lambda c, s: registry.succeeded(value="old"), reusable=False)
    paid = Plugin(lambda c, s: registry.succeeded(derived=s.settings["input"]))
    writer = Plugin(lambda c, s: registry.failed("disk"))
    plugins = Plugins(fresh=fresh, paid=paid, writer=writer)
    cycle = workflow(node("fresh"), node("paid", needs=["fresh"],
                     **{"with": {"input": "${steps.fresh.outputs.value}"}}),
                     node("writer", needs=["paid"]))
    run(cycle, plugins, tmp_path)
    fresh.action = lambda c, s: registry.succeeded(value="new")
    resumed = run(cycle, plugins, tmp_path, resume=True)
    assert "Saved inputs for paid changed" in resumed.steps["writer"].message
    assert writer.calls == ["writer"] and paid.calls == ["paid"]


def test_no_provider_starts_when_initial_checkpoint_cannot_be_written(tmp_path, monkeypatch):
    paid = Plugin(lambda c, s: registry.succeeded())
    monkeypatch.setattr(checkpoints.records, "_atomic",
                        lambda *a: (_ for _ in ()).throw(OSError("full disk")))
    with pytest.raises(checkpoints.CheckpointError):
        run(workflow(node("paid")), Plugins(paid=paid), tmp_path)
    assert paid.calls == []


def test_completed_receipt_survives_summary_write_failure(tmp_path, monkeypatch):
    paid = Plugin(lambda c, s: registry.succeeded(answer="saved"))
    writer = Plugin(lambda c, s: registry.succeeded())
    plugins = Plugins(paid=paid, writer=writer)
    cycle = workflow(node("paid"), node("writer", needs=["paid"]))
    original = checkpoints.Store.save

    def fail_after_paid(self, record):
        if record.steps["paid"].ok:
            raise checkpoints.CheckpointError("Cannot save execution state: full disk")
        return original(self, record)

    with monkeypatch.context() as patch:
        patch.setattr(checkpoints.Store, "save", fail_after_paid)
        assert not run(cycle, plugins, tmp_path).ok
    assert writer.calls == []
    assert run(cycle, plugins, tmp_path, resume=True).ok
    assert paid.calls == ["paid"]


def test_two_executors_cannot_resume_one_run(tmp_path):
    with checkpoints.lock(str(tmp_path / "run")):
        with pytest.raises(checkpoints.CheckpointError, match="already being executed"):
            run(workflow(node("paid")), Plugins(paid=Plugin(lambda c, s: registry.succeeded())), tmp_path)


@pytest.mark.parametrize("change", ["delete", "modify"])
def test_completed_artifacts_are_checked_before_spending_more(tmp_path, change):
    def answer(context, step):
        path = Path(context.step_dir(step.id), "plan.txt")
        path.write_text("paid plan")
        return registry.PluginResult(SUCCESS, artifacts=[Artifact("text", context.relative(str(path)), step.id)])

    paid = Plugin(answer)
    bad = Plugin(lambda c, s: registry.failed("disk"))
    cycle, plugins = workflow(node("paid"), node("bad", needs=["paid"])), Plugins(paid=paid, bad=bad)
    run(cycle, plugins, tmp_path)
    artifact = tmp_path / "run/steps/paid/plan.txt"
    artifact.unlink() if change == "delete" else artifact.write_text("different")
    with pytest.raises(checkpoints.CheckpointError, match="artifact"):
        run(cycle, plugins, tmp_path, resume=True)
    assert paid.calls == ["paid"] and bad.calls == ["bad"]


def test_hard_process_exit_after_paid_result_does_not_rebuy_it(tmp_path):
    script = tmp_path / "crash.py"
    root = Path(__file__).resolve().parents[1]
    script.write_text('''
import os, sys
sys.path.insert(0, sys.argv[1])
from cycle import bus, executor, model, registry
class Paid(registry.CyclePlugin):
    metadata = registry.PluginMetadata(id="paid", name="paid")
    def execute(self, context, step):
        return registry.succeeded(answer="durable")
class Plugins:
    def get(self, name): return Paid()
class Crash(bus.NullObserver):
    def step_end(self, step):
        if step.step_id == "paid": os._exit(77)
c = model.parse_cycle({"steps": [{"id":"paid","plugin":"paid"},
    {"id":"writer","plugin":"writer","needs":["paid"]}]}, "demo")
executor.run_cycle(c, sys.argv[2], registry=Plugins(), observer=Crash())
''')
    process = subprocess.run([sys.executable, str(script), str(root), str(tmp_path / "run")])
    assert process.returncode == 77
    paid = Plugin(lambda c, s: pytest.fail("paid node was run twice"))
    writer = Plugin(lambda c, s: registry.succeeded())
    cycle = workflow(node("paid"), node("writer", needs=["paid"]))
    final = run(cycle, Plugins(paid=paid, writer=writer), tmp_path, resume=True)
    assert final.ok and final.steps["paid"].outputs["answer"] == "durable"


def test_running_node_without_external_receipts_requires_reconciliation(tmp_path):
    paid = Plugin(lambda c, s: registry.failed("fail"))
    cycle = workflow(node("paid"))
    plugins = Plugins(paid=paid)
    record = run(cycle, plugins, tmp_path)
    record.steps["paid"].status = "running"
    checkpoints.Store(record.workspace).step(record, record.steps["paid"])
    with pytest.raises(checkpoints.CheckpointError, match="Uncertain outcome"):
        run(cycle, plugins, tmp_path, resume=True)
    assert paid.calls == ["paid"]


def test_launcher_resume_keeps_one_run_and_partial_execution_pins_its_source(tmp_path):
    import shlex
    import yaml

    root = Path(__file__).resolve().parents[1]
    helper = tmp_path / "service.py"
    counter, ready = tmp_path / "paid.txt", tmp_path / "ready"
    helper.write_text('''
import pathlib, sys
path = pathlib.Path(sys.argv[2])
if sys.argv[1] == "paid":
    with path.open("a") as f: f.write("called\\n")
    print("the saved response")
else:
    sys.exit(0 if path.exists() else 1)
''')
    command = lambda action, path: shlex.join([sys.executable, str(helper), action, str(path)])
    cycles = tmp_path / "cycles"
    cycles.mkdir()
    (cycles / "demo.yaml").write_text(yaml.safe_dump({"id": "demo", "steps": [
        {"id": "paid", "plugin": "command.shell", "with": {"command": command("paid", counter)}},
        {"id": "writer", "plugin": "command.shell", "needs": ["paid"],
         "with": {"command": command("writer", ready)}},
    ]}))
    args = [sys.executable, str(root / "session_launcher.py"), "--cycle-run=demo",
            "--cycles-dir=" + str(cycles), "--events=-"]
    env = dict(os.environ, CMS_HOME=str(tmp_path / "home"))
    first = subprocess.run(args, cwd=root, env=env, capture_output=True, text=True, timeout=30)
    assert first.returncode == 1, first.stderr
    folder, = (tmp_path / "home/cycle-runs").glob("*-demo")
    ready.touch()
    second = subprocess.run(args + ["--cycle-resume=" + folder.name], cwd=root, env=env,
                            capture_output=True, text=True, timeout=30)
    assert second.returncode == 0, second.stdout + second.stderr
    assert counter.read_text() == "called\n"
    assert list((tmp_path / "home/cycle-runs").glob("*-demo")) == [folder]
    saved = json.loads((folder / "metadata.json").read_text())
    assert saved["resume_count"] == 1
    assert saved["steps"]["paid"]["attempts"] == 1

    # A newer run must not replace the explicitly selected source.
    fresh = subprocess.run(args, cwd=root, env=env, capture_output=True, text=True, timeout=30)
    assert fresh.returncode == 0, fresh.stdout + fresh.stderr
    partial = subprocess.run(args + ["--cycle-from=writer", "--cycle-reuse=" + folder.name],
                             cwd=root, env=env, capture_output=True, text=True, timeout=30)
    assert partial.returncode == 0, partial.stdout + partial.stderr
    records = [json.loads(path.read_text())
               for path in (tmp_path / "home/cycle-runs").glob("*/metadata.json")]
    imported, = [one for one in records if one["steps"]["paid"].get("source_run")]
    assert imported["steps"]["paid"]["source_run"] == folder.name
    assert counter.read_text() == "called\ncalled\n"
