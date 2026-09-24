"""The Subjects sidebar: what each session says, and what clicking it does."""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import cyclesubjects as cs
from cms_gui import widgets


def session(key="QA-1", cycle="dev", status="success", running=False,
            reached="", plugin="", runs=None, memory=None, pin="task_key"):
    subject = ({"kind": "task", "key": key, "title": "Fix login",
                "memory": "dev/" + key, "pin": pin} if key else {})
    return {"id": "%s:%s" % (cycle, key or "run:r1"), "cycle": cycle,
            "name": "Development", "subject": subject, "status": status,
            "running": running, "reached": reached, "reached_label": reached.title(),
            "reached_plugin": plugin, "message": "", "latest": "r1",
            "started_at": 1000.0, "updated_at": 2000.0,
            "memory": memory or {},
            "runs": runs if runs is not None else [
                {"id": "r1", "run_dir": "/runs/r1", "started_at": 1000.0,
                 "state": status, "resume_count": 0}]}


GRAPH = {"id": "dev", "subject": {"kind": "task", "key": "${steps.todo.outputs.key}",
                                  "pin": "task_key", "sources": ["todo"]},
         "nodes": [{"id": "todo", "label": "Take one task"},
                   {"id": "approve", "label": "Agree to the plan"}]}


# ------------------------------------------------------------------- words
def test_a_session_waiting_on_a_person_says_so():
    assert cs.status_word(session(running=True, reached="approve",
                                  plugin="approval.gate")) == "waiting for approval"


def test_a_running_session_says_where_it_is():
    assert cs.status_word(session(running=True, reached="plan")) == "running - Plan"


def test_a_stopped_session_says_where_it_stopped():
    assert cs.status_word(session(status="failed", reached="work")) == "stopped at Work"
    assert cs.status_word(session(status="interrupted", reached="")) == "interrupted"


def test_what_the_cycle_remembers_outranks_how_the_last_run_ended():
    """A task committed in an earlier run is done, whatever the latest run did."""
    done = session(status="failed", memory={"key": "dev/QA-1",
                                            "fields": {"state": "committed"}})
    assert cs.status_word(done) == "committed"


def test_a_row_is_named_by_its_subject_or_by_its_run():
    assert cs.subject_title(session()) == "QA-1  Fix login"
    assert cs.subject_title(session(key="")).startswith("Run ")


def test_today_is_an_hour_and_before_that_a_date():
    now = time.mktime((2026, 9, 24, 15, 0, 0, 0, 0, -1))
    assert cs.when(now - 3600, now) == "14:00"
    assert cs.when(now - 3 * 86400, now).startswith("21 Sep")
    assert cs.when(0, now) == ""


def test_who_picks_the_subject_is_said_in_the_step_s_own_words():
    labels = {"todo": "Take one task"}
    assert cs.new_run_text(GRAPH["subject"], labels) == \
        'Step "Take one task" picks the task.'
    assert cs.new_run_text(None, labels) == "Each run of this cycle stands on its own."


def test_the_details_say_how_a_resumed_run_began_and_why():
    live = {"run_id": "r1", "subject": {"key": "QA-1", "step": "todo"},
            "mode": {"mode": "resume", "resume_count": 1, "kept": ["todo"],
                     "rerun": [{"step": "approve", "reason": "asks again on every run"}]},
            "revisions": [{"number": 1, "feedback": "smaller change"}]}
    text = cs.details_html(session(), live, GRAPH["subject"],
                           {"todo": "Take one task", "approve": "Agree to the plan"})
    assert "Working on task <b>QA-1</b>" in text
    assert "Decided by Take one task." in text
    assert "Resumed - attempt 2 of this run." in text
    assert "Kept from before: Take one task." in text
    assert "Agree to the plan - asks again on every run" in text
    assert "smaller change" in text


def test_the_details_of_a_run_that_has_not_found_its_subject_say_who_will():
    text = cs.details_html(session(key=""), {"mode": {"mode": "fresh"}},
                           GRAPH["subject"], {"todo": "Take one task"})
    assert 'Not decided yet. Step &quot;Take one task&quot; picks the task.' in text


def test_the_details_show_what_is_remembered():
    remembered = {"key": "dev/QA-1", "fields": {"attempts": 2, "state": "committed"},
                  "updated_at": None, "claim": {}}
    text = cs.details_html(session(memory=remembered))
    assert "dev/QA-1" in text and "attempts" in text and "committed" in text


def test_text_from_a_run_is_escaped():
    text = cs.details_html(dict(session(), message="<b>boom</b>", status="failed"))
    assert "&lt;b&gt;boom" in text


# ------------------------------------------------------------------- the panel
@pytest.fixture
def panel(qapp, dispose):
    made = cs.SubjectsPanel()
    yield made
    dispose(made)


def test_the_open_cycle_s_sessions_come_first_after_the_new_run_row(panel):
    panel.set_sessions([session(key="OTHER-1", cycle="other"), session()])
    panel.set_cycle("dev", GRAPH)
    assert panel.row_ids() == [cs.NEW_RUN, "dev:QA-1", "other:OTHER-1"]


def test_the_run_on_screen_picks_its_session(panel):
    panel.set_sessions([session()])
    panel.set_live({"run_id": "r1", "cycle": "dev"})
    assert panel.picked()["id"] == "dev:QA-1"


def test_clicking_a_row_asks_for_its_session(panel):
    panel.set_sessions([session()])
    panel.set_cycle("dev", GRAPH)
    asked = []
    panel.activated.connect(asked.append)
    panel._clicked(panel.list.item(1))
    assert [one["id"] for one in asked] == ["dev:QA-1"]


def test_the_new_run_row_offers_to_start_one(panel):
    panel.set_cycle("dev", GRAPH)
    started = []
    panel.new_requested.connect(lambda: started.append(True))
    panel._clicked(panel.list.item(0))
    assert not panel.new_button.isHidden()
    panel.new_button.click()
    assert started == [True]


def test_the_bin_asks_to_delete_its_own_session(panel):
    panel.set_sessions([session(), session(key="QA-2")])
    asked = []
    panel.delete_requested.connect(asked.append)
    row = panel.list.itemWidget(panel.list.item(1))
    row.bin.click()
    assert [one["id"] for one in asked] == ["dev:QA-2"]


def test_a_running_session_cannot_be_deleted(panel):
    panel.set_sessions([session(running=True)])
    assert not panel.list.itemWidget(panel.list.item(0)).bin.isEnabled()


def test_nothing_is_deleted_or_started_while_a_cycle_runs(panel):
    panel.set_sessions([session()])
    panel.set_cycle("dev", GRAPH)
    panel.set_busy(True)
    assert not panel.list.itemWidget(panel.list.item(1)).bin.isEnabled()
    assert not panel.new_button.isEnabled()


def test_a_run_in_the_details_can_be_opened(panel):
    panel.set_sessions([session()])
    panel.set_live({"run_id": "r1", "cycle": "dev"})
    opened = []
    panel.run_picked.connect(lambda *args: opened.append(args))
    from PySide6.QtCore import QUrl
    panel._link(QUrl("run:r1"))
    assert opened == [("dev", "r1")]


# ---------------------------------------------------------------- the splitter
@pytest.fixture
def splitter(qapp, dispose):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget
    made = widgets.FoldingSplitter(Qt.Horizontal)
    for _ in range(3):
        made.addWidget(QWidget())
    made.resize(900, 300)
    made.setSizes([200, 500, 200])
    made.show()
    yield made
    dispose(made)


def _marks(splitter):
    return [mark for mark in splitter._marks.values() if not mark.isHidden()]


def test_an_open_panel_has_no_mark(splitter):
    assert _marks(splitter) == []


def test_a_folded_panel_leaves_a_bold_line_at_its_edge(splitter):
    splitter.setSizes([0, 700, 200])
    marks = _marks(splitter)
    assert len(marks) == 1
    assert marks[0].geometry().x() == 0
    assert marks[0].width() == widgets.FOLD_MARK
    # Half the edge, centred, so marks folded at one corner do not meet.
    assert marks[0].height() == splitter.height() // 2
    assert marks[0].geometry().center().y() in range(splitter.height() // 2 - 1,
                                                     splitter.height() // 2 + 2)


def test_the_last_panel_folds_against_the_far_edge(splitter):
    splitter.setSizes([200, 700, 0])
    mark = _marks(splitter)[0]
    assert mark.geometry().right() == splitter.width() - 1


def test_clicking_the_line_brings_the_panel_back(splitter):
    splitter.setSizes([0, 700, 200])
    _marks(splitter)[0].clicked.emit()
    assert splitter.sizes()[0] > 0
    assert _marks(splitter) == []


def test_a_mark_is_never_a_panel_of_its_own(splitter):
    """A splitter adopts every child widget it is given as another pane."""
    splitter.setSizes([0, 700, 200])
    splitter.setSizes([200, 700, 0])
    assert splitter.count() == 3


class _Settings:
    def __init__(self):
        self.stored = {}

    def splitter(self, key):
        return self.stored.get(key, {})

    def save_splitter(self, key, state):
        self.stored[key] = state


def test_a_folded_panel_stays_folded_after_a_restart(qapp, dispose):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget
    settings = _Settings()

    def make():
        made = widgets.FoldingSplitter(Qt.Horizontal)
        for _ in range(3):
            made.addWidget(QWidget())
        made.resize(900, 300)
        made.setSizes([200, 500, 200])
        made.remember_in(settings, "page/across")
        made.show()
        return made

    first = make()
    first.moveSplitter(0, 1)             # a person drags the first panel shut
    assert first.sizes()[0] == 0 and settings.stored["page/across"]["open"][0] == 200
    dispose(first)

    again = make()
    assert again.sizes()[0] == 0
    assert _marks(again)
    _marks(again)[0].clicked.emit()
    assert again.sizes()[0] > 0
    dispose(again)


def test_a_stored_state_for_other_panels_is_ignored(qapp, dispose):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget
    settings = _Settings()
    settings.stored["k"] = {"sizes": [0, 100], "open": [50, 50]}
    made = widgets.FoldingSplitter(Qt.Horizontal)
    for _ in range(3):
        made.addWidget(QWidget())
    made.setSizes([200, 500, 200])
    made.remember_in(settings, "k")
    assert made.sizes()[0] > 0
    dispose(made)
