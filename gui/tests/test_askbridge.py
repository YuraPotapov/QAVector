"""The GUI half of an approval gate: the window it puts up, and what it sends back.

The core half is ``cycle/ask.py`` and is tested there. What only this side can
be wrong about is the window: that pressing an answer sends that answer, that
closing it sends something rather than leaving a run blocked for ten minutes,
and that closing it never sends an approval. The last of those is the one worth
having a test for - a bug there would look like nothing at all until a run had
already committed something nobody agreed to.
"""

import os
import sys
import time

import pytest
from PySide6.QtWidgets import QPlainTextEdit, QPushButton

from cms_gui.askbridge import AskBridge, AskDialog

# The core lives one directory up and is not installed; the GUI never imports it
# at runtime, but a test may - to check the two agree about the wire.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))


def _pump(qapp, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline and not predicate():
        qapp.processEvents()
        time.sleep(0.02)
    qapp.processEvents()
    return predicate()


def _event(**fields):
    one = {"kind": "cycle.ask", "id": 1, "question": "Commit this?",
           "detail": "", "options": ["Approve", "Reject"], "timeout_ms": 600000}
    one.update(fields)
    return one


@pytest.fixture
def bridge(qapp):
    sent = []
    made = AskBridge(lambda **kwargs: sent.append(kwargs))
    made.sent = sent
    yield made
    made.cancel_all()
    qapp.processEvents()


def _dialog(bridge):
    return bridge._open[1][0]


# -------------------------------------------------------------- the window
def test_a_question_puts_up_a_window_with_the_answers_on_it(qapp, bridge):
    assert bridge.handle(_event()) is True

    dialog = _dialog(bridge)
    labels = [one.text() for one in dialog.findChildren(QPushButton)]
    assert labels == ["Approve", "Reject"]
    assert dialog.isVisible()


def test_it_is_not_modal_so_a_second_gate_can_still_be_answered(qapp, bridge):
    """Steps run in parallel. A modal window would deadlock two of them against
    a decision about how the first one was drawn."""
    bridge.handle(_event(id=1))
    bridge.handle(_event(id=2, question="And this?"))

    assert _dialog(bridge).isModal() is False
    assert len(bridge._open) == 2


def test_the_same_question_twice_does_not_put_up_two_windows(qapp, bridge):
    bridge.handle(_event())
    bridge.handle(_event())
    assert len(bridge._open) == 1


def test_anything_that_is_not_a_question_is_left_alone(qapp, bridge):
    assert bridge.handle({"kind": "cycle.step.end", "id": 1}) is False
    assert bridge._open == {}


# -------------------------------------------------------------- the answer
def test_pressing_an_answer_sends_that_answer(qapp, bridge):
    bridge.handle(_event())
    _dialog(bridge)._choose("Approve")
    _pump(qapp, lambda: bridge.sent)

    assert bridge.sent[0]["command"] == "ask.result"
    assert bridge.sent[0]["id"] == 1
    assert bridge.sent[0]["answer"] == "Approve"


def test_the_answer_says_who_gave_it(qapp, bridge):
    bridge.handle(_event())
    _dialog(bridge)._choose("Approve")
    _pump(qapp, lambda: bridge.sent)

    assert bridge.sent[0]["who"], "a run record that cannot say who is weaker"


def test_closing_it_answers_rather_than_leaving_the_run_blocked(qapp, bridge):
    """Ten minutes of a run held up by somebody who has already decided not to
    look at it."""
    bridge.handle(_event())
    _dialog(bridge).reject()
    _pump(qapp, lambda: bridge.sent)

    assert bridge.sent[0]["answer"] == ""


def test_closing_it_never_sends_one_of_the_answers(qapp, bridge):
    """The one that matters. None of the options can be assumed from a window
    being closed, and assuming the first would approve something silently."""
    bridge.handle(_event(options=["Ship it", "Hold"]))
    _dialog(bridge).reject()
    _pump(qapp, lambda: bridge.sent)

    assert bridge.sent[0]["answer"] not in ("Ship it", "Hold")


def test_what_it_sends_is_what_the_control_reader_takes(qapp, bridge):
    """The two halves agreeing about the wire, checked rather than assumed.
    The core's reading of a dismissal is tested on its own side."""
    bridge.handle(_event())
    _dialog(bridge)._choose("Approve")
    _pump(qapp, lambda: bridge.sent)

    assert set(bridge.sent[0]) == {"command", "id", "answer", "who"}


def test_one_answer_each_when_two_are_open(qapp, bridge):
    bridge.handle(_event(id=4))
    bridge.handle(_event(id=7, question="And this?"))
    bridge._open[7][0]._choose("Reject")
    bridge._open[4][0]._choose("Approve")
    _pump(qapp, lambda: len(bridge.sent) == 2)

    assert {one["id"]: one["answer"] for one in bridge.sent} == {
        4: "Approve", 7: "Reject"}


def test_an_answered_question_sends_nothing_a_second_time(qapp, bridge):
    bridge.handle(_event())
    dialog = _dialog(bridge)
    dialog._choose("Approve")
    _pump(qapp, lambda: bridge.sent)
    dialog.reject()
    qapp.processEvents()

    assert len(bridge.sent) == 1


# ------------------------------------------------------------ the deadline
def test_the_window_counts_the_core_s_deadline_down(qapp, bridge):
    bridge.handle(_event(timeout_ms=180000))
    assert "minutes left" in _dialog(bridge)._left.text()


def test_a_question_whose_time_ran_out_stops_standing_on_screen(qapp, bridge):
    """A button that no longer decides anything should not be there to press."""
    bridge.handle(_event(timeout_ms=1000))
    bridge._tick(1, [1])
    qapp.processEvents()

    assert 1 not in bridge._open


def test_the_run_ending_takes_every_question_with_it(qapp, bridge):
    bridge.handle(_event(id=1))
    bridge.handle(_event(id=2))
    bridge.cancel_all()
    qapp.processEvents()

    assert bridge._open == {}
    assert bridge.sent == [], "nothing is listening on the other end any more"


def test_a_withdrawn_question_closes_its_window_and_sends_nothing(qapp, bridge):
    """The step it was about finished while the window was up."""
    bridge.handle(_event(id=1))
    bridge.handle(_event(id=2))
    assert bridge.handle({"kind": "cycle.ask.withdrawn", "id": 1}) is True
    qapp.processEvents()

    assert list(bridge._open) == [2]
    assert bridge.sent == []


def test_withdrawing_a_question_that_is_not_open_is_harmless(qapp, bridge):
    assert bridge.handle({"kind": "cycle.ask.withdrawn", "id": 7}) is True
    assert bridge._open == {}


# ---------------------------------------------------------------- the look
def test_the_detail_is_shown_where_a_line_of_it_can_be_selected(qapp):
    dialog = AskDialog("Commit?", detail="--- a/x\n+++ b/x\n", parent=None)
    try:
        box = dialog.findChild(QPlainTextEdit)
        assert box is not None and box.isReadOnly()
        assert "+++ b/x" in box.toPlainText()
    finally:
        dialog.deleteLater()


def test_a_question_with_no_options_still_offers_two(qapp, bridge):
    bridge.handle(_event(options=[]))
    labels = [one.text() for one in _dialog(bridge).findChildren(QPushButton)]
    assert labels == ["Approve", "Reject"]


def test_long_approval_has_room_and_keeps_original_text(qapp):
    detail = "## Plan\n\n" + "Read the proposed change carefully.\n\n" * 100
    dialog = AskDialog("Start work?", detail, ["Start work", "Leave it"])
    try:
        dialog.show()
        qapp.processEvents()
        assert dialog.width() >= 640 and dialog.height() >= 440
        assert dialog.detail_tabs.currentWidget() is dialog.review
        assert dialog.original.toPlainText() == detail
        assert dialog.review.height() > 320
        for button in dialog.findChildren(QPushButton):
            assert button.isVisible()
            assert dialog.rect().contains(button.mapTo(dialog, button.rect().bottomRight()))
    finally:
        dialog.close()
        dialog.deleteLater()


def test_enter_does_not_accidentally_approve_an_open_question(qapp, bridge):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    bridge.handle(_event(detail="Read the plan before deciding."))
    dialog = _dialog(bridge)
    QTest.keyClick(dialog.review, Qt.Key_Return)
    qapp.processEvents()
    assert bridge.sent == []
    assert dialog.isVisible()
    dialog.findChildren(QPushButton)[0].click()
    assert bridge.sent[0]["answer"] == "Approve"


def test_review_counts_are_visible_above_the_long_document(qapp):
    detail = ("A long plan.\n\nRisk: medium\n"
              "First review: 4 blocking of 4 note(s).\n"
              "After the second pass: 1 blocking of 2 note(s).\n")
    dialog = AskDialog("Start work?", detail)
    try:
        assert [(label, value.text()) for label, value in dialog.facts] == [
            ("Risk", "medium"), ("First review", "4 blocking of 4 note(s)."),
            ("Second review", "1 blocking of 2 note(s).")]
        assert dialog.original.toPlainText() == detail
    finally:
        dialog.deleteLater()


def test_feedback_requests_revision_without_sending_approval(qapp, bridge):
    bridge.handle(_event(revision={"enabled": True, "remaining": 3}))
    dialog = _dialog(bridge)
    assert not dialog.revision_button.isEnabled()
    dialog.feedback_editor.setPlainText("Cover the fallback")
    assert dialog.revision_button.isEnabled()
    assert not dialog.answer_buttons[0].isEnabled()
    dialog.revision_button.click()
    assert bridge.sent[0]["revise"] is True
    assert bridge.sent[0]["feedback"] == "Cover the fallback"
    assert bridge.sent[0]["answer"] == ""


def test_dismissing_typed_feedback_does_not_send_it(qapp, bridge):
    bridge.handle(_event(revision={"enabled": True, "remaining": 3}))
    dialog = _dialog(bridge)
    dialog.feedback_editor.setPlainText("Cover the fallback")
    dialog.reject()
    assert bridge.sent[0]["answer"] == ""
    assert "revise" not in bridge.sent[0]


def test_exhausted_revisions_keep_ordinary_decisions_available(qapp, bridge):
    bridge.handle(_event(revision={"enabled": False, "remaining": 0}))
    dialog = _dialog(bridge)
    assert not dialog.feedback_editor.isEnabled()
    assert not dialog.revision_button.isEnabled()
    assert all(button.isEnabled() for button in dialog.answer_buttons)
