"""Execution mode selection and approval commands cross the real CLI boundary."""

import io
import json

import pytest

import session_launcher as launcher
from cycle import executor


@pytest.mark.parametrize("mode", [None, "auto", "important", "approve"])
def test_cli_passes_explicit_mode_and_preserves_unspecified_mode(monkeypatch, mode):
    received = []
    monkeypatch.setattr(launcher, "run_cycle_mode", lambda cycle_id, **kw: received.append((cycle_id, kw)) or 0)
    args = ["session_launcher.py", "--cycle-run=demo"]
    if mode is not None:
        args.append("--cycle-mode=" + mode)
    monkeypatch.setattr(launcher.sys, "argv", args)
    with pytest.raises(SystemExit) as exited:
        launcher.main()
    assert exited.value.code == 0
    assert received[0][0] == "demo" and received[0][1]["mode"] == mode


@pytest.mark.parametrize("mode", ["", "Important", "unknown"])
def test_invalid_cli_mode_is_refused_before_launch(monkeypatch, mode):
    monkeypatch.setattr(launcher.sys, "argv", ["session_launcher.py", "--cycle-run=demo", "--cycle-mode=" + mode])
    with pytest.raises(SystemExit, match="auto, important or approve"):
        launcher.main()


def test_control_pipe_routes_fingerprinted_approval_and_paused_mode_change(monkeypatch):
    delivered, modes = [], []
    answer = {"command": "cycle.node.approval.answer", "run_id": "r", "request_id": "request", "fingerprint": "proof", "approved": False}
    mode = {"command": "cycle.mode.change", "run_id": "r", "mode": "important"}
    monkeypatch.setattr(executor, "answer_node_approval", lambda raw: delivered.append(raw) or "")
    monkeypatch.setattr(executor, "control_run", lambda *args, **kw: modes.append((args, kw)) or "")
    monkeypatch.setattr(launcher.sys, "stdin", io.StringIO(json.dumps(answer) + "\n" + json.dumps(mode) + "\n"))
    launcher.read_commands({})
    assert delivered == [answer]
    assert modes == [(("r", "set_mode"), {"mode": "important"})]
