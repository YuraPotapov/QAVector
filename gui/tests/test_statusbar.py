"""The status bar: what sits where."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_cpu_and_ram_are_always_at_the_far_right(qapp, dispose):
    """A label added later in construction once pushed "Listening" past the
    load readout; the readout is placed last so nothing can."""
    from PySide6.QtWidgets import QLabel

    from cms_gui import main_window

    window = main_window.MainWindow()
    try:
        window.resize(1600, 900)
        window.show()
        qapp.processEvents()
        bar = window.statusBar()
        labels = [one for one in bar.findChildren(QLabel)
                  if one.isVisibleTo(window) and one.parent() is not None]
        rightmost = max(labels, key=lambda one: one.mapTo(window, one.rect().topLeft()).x())
        assert rightmost is window.metrics_label
        x = {name: getattr(window, name).mapTo(window, getattr(window, name).rect().topLeft()).x()
             for name in ("services_label", "listen_label", "metrics_label")}
        assert x["services_label"] < x["listen_label"] < x["metrics_label"], \
            "Listening sits beside the services, CPU and RAM last"
    finally:
        window.listener.shutdown()
        dispose(window)


def test_a_new_task_nobody_answers_goes_on_the_plan(qapp, monkeypatch):
    """The window counts down on its plan button and presses it at zero.

    No main window and no event loop: the countdown is stepped by hand, so the
    test cannot process the suite's other pending deletes - which is what
    crashed it when it ran a real loop over a real window.
    """
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QDialog

    from cms_gui import cyclewatch, widgets

    seen = []

    def run_the_countdown(dialog):
        timer = dialog.findChild(QTimer, "plan-countdown")
        for _ in range(cyclewatch.PLAN_AFTER_SECONDS):
            seen.append(dialog.choice_buttons[-1].text())
            timer.timeout.emit()
        return QDialog.Accepted if dialog.chosen else QDialog.Rejected

    monkeypatch.setattr(widgets.Message, "exec", run_the_countdown)
    answer = cyclewatch.ask_about(None, {"cycle": "dev", "trigger": "t", "key": "QA-7",
                                         "title": "Count", "name": "Dev", "pin": "k"})
    assert answer == cyclewatch.PLAN_LABEL
    assert seen[0] == "In plan (20)" and seen[1] == "In plan (19)"
