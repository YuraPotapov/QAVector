"""Implementation reports explain saved results without changing their meaning."""

import copy
import json

import pytest
from PySide6.QtGui import QTextDocument

from cms_gui.cycleoutput import implementation_html


def failed_step():
    # Older runs store the provider response only in an error string, sometimes
    # cut in the middle of usage. No structured cost field is available there.
    message = ('attempt-1 exited 1: {"type":"result","subtype":"error_max_turns",'
               '"duration_ms":183106,"num_turns":31,"session_id":"private-session",'
               '"total_cost_usd":2.3720520000000005,"usage":{"input_tokens":3007,')
    return {"plugin": "agent.implement", "status": "failed", "message": message,
            "outputs": {"verified": False, "attempts": 1, "changed": 0,
                        "files_changed": [], "summary": "", "findings": [],
                        "failed_check": message, "tree": "912362fd1aeac1b33f84f26528aae6a2efa0df3c",
                        "report_path": "steps/implement/implement.json"}}


def plain(step):
    document = QTextDocument()
    document.setHtml(implementation_html(step))
    return document.toPlainText()


def test_saved_truncated_turn_limit_result_is_a_readable_explanation(qapp):
    step = failed_step()
    original = copy.deepcopy(step)
    text = plain(step)
    for expected in ("Agent reached its step limit", "31", "3 min 3 s", "$2.37",
                     "None reported", "Not verified", "Maximum tool iterations",
                     "may incur a charge"):
        assert expected in text
    for technical in ("error_max_turns", "exited 1", "private-session", "input_tokens",
                      "failed_check", "912362fd", "implement.json", "False"):
        assert technical not in text
    assert "checks failed" not in text.lower()
    assert step == original


def test_structured_total_cost_wins_over_one_call_cost(qapp):
    step = failed_step()
    step["outputs"]["cost_usd"] = 4.126
    text = plain(step)
    assert "$4.13" in text and "$2.37" not in text
    assert "Reported cost" in text and "Last agent call cost" not in text


@pytest.mark.parametrize("value", [None, True, -1, float("nan"), float("inf")])
def test_missing_or_invalid_metrics_are_not_presented_as_zero(qapp, value):
    text = plain({"plugin": "agent.implement", "status": "failed",
                  "message": "Something went wrong", "outputs": {"cost_usd": value}})
    assert "cost" not in text
    assert "File changes" not in text
    assert "Verification" not in text


def test_success_and_untrusted_agent_text_are_shown_without_html_execution(qapp):
    step = {"plugin": "agent.implement", "status": "success", "duration_ms": 1000,
            "outputs": {"verified": True, "attempts": 2, "cost_usd": 0,
                        "summary": '<img src="https://example.invalid/track">Changed <b>logic</b>',
                        "files_changed": ["models/a<b>.py"],
                        "findings": [{"description": "Consider an edge case"}]}}
    document = implementation_html(step)
    text = plain(step)
    assert "Change completed and verified" in text
    assert "1 file(s) reported" in text and "$0.00" in text
    assert "models/a<b>.py" in text and "Consider an edge case" in text
    assert '<img src=' not in document
    assert '<img src=' in text and "Changed <b>logic</b>" in text


def test_complete_provider_response_is_understood(qapp):
    step = failed_step()
    step["outputs"]["failed_check"] = json.dumps({
        "type": "result", "subtype": "error_max_turns", "num_turns": 11,
        "total_cost_usd": 0.09, "duration_ms": 9100})
    text = plain(step)
    assert "$0.09" in text and "9 s" in text and "11" in text


@pytest.mark.parametrize("status,title", [
    ("running", "Implementation in progress"), ("pending", "Waiting to start"),
    ("cancelled", "Implementation was stopped"), ("skipped", "Implementation was skipped"),
    ("timeout", "Implementation ran out of time"),
])
def test_lifecycle_states_do_not_claim_a_finished_change(qapp, status, title):
    text = plain({"plugin": "agent.implement", "status": status})
    assert title in text
    assert "Change completed" not in text


def test_other_plugins_keep_their_existing_output():
    assert not implementation_html({"plugin": "command.shell", "status": "failed"})
