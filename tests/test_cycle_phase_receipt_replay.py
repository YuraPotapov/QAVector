"""Phase receipts remain tied to the executions restored from checkpoints."""

import copy
from dataclasses import asdict

import pytest

from cycle.checkpoints import Store
from domain.cycle import CycleRun, StepRun


def saved_phase(tmp_path, metadata_generation=1, receipt_generation=1):
    step = StepRun("one", status="success", execution_generation=1,
                   definition_digest="definition", input_digest="input")
    evidence = {key: getattr(step, key) for key in
                ("status", "execution_generation", "definition_digest", "input_digest")}
    receipt = {"phase": "prepare", "status": "success", "generation": receipt_generation,
               "evidence": {"one": evidence}}
    saved = dict(copy.deepcopy(receipt), generation=metadata_generation, invalidated=True)
    run = CycleRun("r", cycle_id="demo", status="running", steps={"one": step},
                   phase_definition=[{"id": "prepare"}], phase_receipts={"prepare": saved})
    store = Store(str(tmp_path))
    store.save(run)
    store.write(str(tmp_path / ".execution/phases/prepare.json"), receipt)
    return store, step


@pytest.mark.parametrize("metadata_generation,receipt_generation,expected_generation,invalidated", [
    (1, 1, 1, True), (1, 2, 2, False), (2, 1, 2, True)])
def test_phase_receipt_overlay_preserves_invalidation_and_newest_generation(
        tmp_path, metadata_generation, receipt_generation, expected_generation, invalidated):
    store, _ = saved_phase(tmp_path, metadata_generation, receipt_generation)
    receipt = store.load().phase_receipts["prepare"]
    assert receipt["generation"] == expected_generation
    assert bool(receipt.get("invalidated")) is invalidated


@pytest.mark.parametrize("changed", [
    {"status": "pending"}, {"execution_generation": 2},
    {"definition_digest": "new definition"}, {"input_digest": "new input"}])
def test_phase_receipt_is_invalidated_by_a_newer_individual_step_receipt(tmp_path, changed):
    store, step = saved_phase(tmp_path, metadata_generation=0)
    current = dict(asdict(step), **changed)
    store.write(str(tmp_path / ".execution/steps/one.json"), current)
    restored = store.load()
    assert restored.phase_receipts["prepare"]["invalidated"]


def test_new_phase_receipt_can_validate_a_new_step_execution(tmp_path):
    store, step = saved_phase(tmp_path, receipt_generation=2)
    step.execution_generation = 2
    store.write(str(tmp_path / ".execution/steps/one.json"), asdict(step))
    receipt = {"phase": "prepare", "status": "success", "generation": 2,
               "evidence": {"one": {"status": "success", "execution_generation": 2,
                                     "definition_digest": "definition", "input_digest": "input"}}}
    store.write(str(tmp_path / ".execution/phases/prepare.json"), receipt)
    assert not store.load().phase_receipts["prepare"].get("invalidated")
