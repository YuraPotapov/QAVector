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

def _item(panel, index):
    """The index-th row that stands for something - headings are skipped."""
    rows = [panel.list.item(one) for one in range(panel.list.count())
            if panel.list.item(one).data(cs.ROLE_SESSION)]
    return rows[index]

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
    panel._clicked(_item(panel, 1))
    assert [one["id"] for one in asked] == ["dev:QA-1"]


def test_the_new_run_row_offers_to_start_one(panel):
    panel.set_cycle("dev", GRAPH)
    started = []
    panel.new_requested.connect(lambda: started.append(True))
    panel._clicked(_item(panel, 0))
    assert not panel.new_button.isHidden()
    panel.new_button.click()
    assert started == [True]


def test_the_bin_asks_to_delete_its_own_session(panel):
    panel.set_sessions([session(), session(key="QA-2")])
    asked = []
    panel.delete_requested.connect(asked.append)
    row = panel.list.itemWidget(_item(panel, 1))
    row.bin.click()
    assert [one["id"] for one in asked] == ["dev:QA-2"]


def test_a_running_session_cannot_be_deleted(panel):
    panel.set_sessions([session(running=True)])
    assert not panel.list.itemWidget(_item(panel, 0)).bin.isEnabled()


def test_nothing_is_deleted_or_started_while_a_cycle_runs(panel):
    panel.set_sessions([session()])
    panel.set_cycle("dev", GRAPH)
    panel.set_busy(True)
    assert not panel.list.itemWidget(_item(panel, 1)).bin.isEnabled()
    assert not panel.new_button.isEnabled()


def test_a_run_in_the_details_can_be_opened(panel):
    panel.set_sessions([session()])
    panel.set_live({"run_id": "r1", "cycle": "dev"})
    opened = []
    panel.run_picked.connect(lambda *args: opened.append(args))
    from PySide6.QtCore import QUrl
    panel._link(QUrl("run:r1"))
    assert opened == [("dev", "r1")]


def test_each_run_in_the_details_links_to_its_artifacts():
    html = cs.details_html(session())
    assert "href='files:r1'" in html


def test_a_run_with_no_folder_offers_no_artifacts_link():
    one = session(runs=[{"id": "r1", "run_dir": "", "started_at": 1000.0,
                         "state": "success", "resume_count": 0}])
    assert "files:" not in cs.details_html(one)


def test_the_artifacts_link_asks_for_that_runs_files(panel):
    panel.set_sessions([session()])
    panel.set_live({"run_id": "r1", "cycle": "dev"})
    asked = []
    panel.files_picked.connect(lambda *args: asked.append(args))
    from PySide6.QtCore import QUrl
    panel._link(QUrl("files:r1"))
    assert asked == [("dev", "r1")]


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


# ------------------------------------------------------------------ planned
def planned(key="QA-7", cycle="dev", title="Count the items"):
    return {"key": key, "title": title, "cycle": cycle, "trigger": "new_task",
            "url": "https://x.invalid/" + key, "at": time.time()}


def test_planned_tasks_sit_under_the_new_run_row_the_open_cycle_s_first(panel):
    panel.set_cycle("dev", {"nodes": []})
    panel.set_sessions([session()])
    panel.set_planned([planned(key="OPS-1", cycle="ops"), planned()])
    assert panel.row_ids() == [cs.NEW_RUN, "plan:dev:QA-7", "plan:ops:OPS-1",
                               "dev:QA-1"]


def test_a_planned_row_says_what_and_that_it_waits(panel):
    panel.set_cycle("dev", {"nodes": []})
    panel.set_planned([planned(key="OPS-1", cycle="ops")])
    row = panel.list.itemWidget(_item(panel, 1))
    assert row.title.text() == "OPS-1  Count the items"
    assert row.detail.text().startswith("ops - planned ")
    assert row.detail.text().endswith("waiting for you")


def test_picking_a_planned_task_offers_to_start_work_on_it(panel):
    panel.set_cycle("dev", {"nodes": []})
    panel.set_planned([planned()])
    started, fresh = [], []
    panel.planned_start.connect(started.append)
    panel.new_requested.connect(lambda: fresh.append(True))
    panel._clicked(_item(panel, 1))

    assert panel.new_button.isVisible() or not panel.isVisible()
    assert panel.new_button.text() == "Start work"
    assert "Count the items" in panel.details.toPlainText()
    panel.new_button.click()
    assert [one["key"] for one in started] == ["QA-7"] and fresh == []


def test_the_bin_takes_it_off_the_plan(panel):
    panel.set_cycle("dev", {"nodes": []})
    panel.set_planned([planned()])
    removed = []
    panel.planned_remove.connect(removed.append)
    panel.list.itemWidget(_item(panel, 1)).bin.click()
    assert [one["key"] for one in removed] == ["QA-7"]


def test_a_planned_task_that_went_away_is_no_longer_picked(panel):
    panel.set_cycle("dev", {"nodes": []})
    panel.set_planned([planned()])
    panel._clicked(_item(panel, 1))
    panel.set_planned([])
    assert panel.planned_item() is None and panel.new_button.text() == "Start"


def test_an_approval_nobody_answered_is_resumed_from_the_plan(panel):
    waiting = {"id": "approval:r1:approve", "kind": "approval", "cycle": "dev",
               "run": "r1", "step": "approve", "key": "QA-4",
               "question": "Start work on QA-4?", "at": time.time()}
    panel.set_cycle("dev", {"nodes": []})
    panel.set_planned([waiting])
    assert panel.row_ids()[1] == "plan:approval:r1:approve"
    row = panel.list.itemWidget(_item(panel, 1))
    assert row.title.text() == "QA-4  waiting for your approval"
    assert "resume to be asked again" in row.detail.text()
    started = []
    panel.planned_start.connect(started.append)
    panel._clicked(_item(panel, 1))
    assert panel.new_button.text() == "Resume"
    assert "Start work on QA-4?" in panel.details.toPlainText()
    panel.new_button.click()
    assert started[0]["run"] == "r1"


def test_several_changes_at_once_are_drawn_once(qapp, dispose, monkeypatch):
    """A hundred and fifty sessions take a fifth of a second to draw; opening
    a cycle used to draw them four times over."""
    from cms_gui.cyclesubjects import SubjectsPanel
    panel = SubjectsPanel()
    try:
        drawn = []
        real = panel._rebuild
        monkeypatch.setattr(panel, "_rebuild", lambda: drawn.append(1) or real())
        panel.set_state(cycle="demo", graph={}, sessions=[
            {"id": "demo:QA-1", "cycle": "demo", "subject": {"key": "QA-1"},
             "status": "failed", "runs": [{"id": "r1"}]}],
            planned=[], live={"run_id": "r1", "cycle": "demo"})
        assert drawn == [1]
        assert panel.picked()["id"] == "demo:QA-1"
    finally:
        dispose(panel)


def test_a_long_list_is_drawn_a_screenful_first_and_the_rest_after(qapp, dispose):
    from cms_gui import cyclesubjects
    from cms_gui.cyclesubjects import SubjectsPanel
    panel = SubjectsPanel()
    try:
        many = [{"id": "demo:QA-%d" % one, "cycle": "demo",
                 "subject": {"key": "QA-%d" % one}, "status": "success",
                 "runs": [{"id": "r%d" % one}]} for one in range(100)]
        panel.set_state(cycle="demo", graph={}, sessions=many)
        now = len(panel.row_ids())
        assert now == 1 + cyclesubjects.ROWS_AT_ONCE, "+ New run, then a screenful"
        for _ in range(10):
            qapp.processEvents()
        assert len(panel.row_ids()) == 101
        panel.set_state(sessions=many[:3])
        for _ in range(10):
            qapp.processEvents()
        assert len(panel.row_ids()) == 4, "a newer drawing drops the older's rest"
    finally:
        dispose(panel)


def test_a_planned_whole_answer_shows_how_many_and_every_task():
    item = {"kind": "task", "cycle": "rel", "key": "QA-1,QA-2", "title": "2 tasks",
            "items": [{"key": "QA-1", "title": "First", "url": "https://x.invalid/QA-1"},
                      {"key": "QA-2", "title": "Second", "url": ""}], "at": 0}
    assert cs.planned_title(item) == "2 tasks  QA-1, QA-2"
    shown = cs.planned_html(item)
    assert "runs the cycle on all of them" in shown
    assert "<a href='https://x.invalid/QA-1'>QA-1  First</a>" in shown
    assert "<li>QA-2  Second</li>" in shown


def test_a_planned_single_task_reads_as_before():
    item = {"kind": "task", "cycle": "dev", "key": "QA-3", "title": "One",
            "items": [{"key": "QA-3", "title": "One"}], "at": 0}
    assert cs.planned_title(item) == "QA-3  One"


def test_a_run_started_on_several_tasks_is_named_as_the_plan_named_it():
    session = {"subject": {"kind": "task", "key": "QA-1,QA-2,QA-3", "title": "3 tasks"},
               "started_at": 0}
    assert cs.subject_title(session) == "3 tasks  QA-1, QA-2, QA-3"
    one = {"subject": {"key": "QA-1", "title": "Fix the login"}, "started_at": 0}
    assert cs.subject_title(one) == "QA-1  Fix the login"


def test_a_scheduled_entry_says_when_it_starts():
    now = time.mktime((2026, 10, 6, 9, 30, 0, 0, 0, -1))
    run_at = time.mktime((2026, 10, 6, 18, 0, 0, 0, 0, -1))
    item = {"kind": "task", "cycle": "rel", "key": "QA-1", "title": "t",
            "run_at": run_at, "at": 0}
    assert cs.planned_detail(item, "rel", now=now) == "scheduled for 06 Oct 18:00"
    old = cs.planned_detail(item, "rel", now=run_at + 4 * 24 * 3600)
    assert old == ("was scheduled for 06 Oct 18:00 - more than 3 days ago, "
                   "will not run")


def test_double_clicking_a_planned_entry_asks_when(qapp):
    panel = cs.SubjectsPanel()
    opened = []
    panel.planned_open.connect(opened.append)
    item = {"id": "task:rel:t:QA-1", "kind": "task", "cycle": "rel", "key": "QA-1",
            "title": "t", "at": 0}
    panel.set_planned([item])
    row = next(panel.list.item(index) for index in range(panel.list.count())
               if panel.list.item(index).data(cs.ROLE_SESSION) == cs.plan_id(item))
    panel.list.itemDoubleClicked.emit(row)
    assert opened == [item]
    panel.deleteLater()


# -- pages, and where each stretch of time begins ------------------------------
def test_a_session_falls_in_a_stretch_a_heading_can_name():
    now = time.mktime((2026, 10, 12, 15, 0, 0, 0, 0, -1))
    hour = 3600
    assert cs.period(now - hour, now) == "Today"
    assert cs.period(now - 20 * hour, now) == "Yesterday"
    assert cs.period(now - 4 * 24 * hour, now) == "Last 7 days"
    assert cs.period(now - 20 * 24 * hour, now) == "Last 30 days"
    assert cs.period(time.mktime((2026, 8, 3, 9, 0, 0, 0, 0, -1)), now) == "August 2026"


def _dated(key, cycle, days_ago):
    one = session(key, cycle)
    one["updated_at"] = time.time() - days_ago * 86400
    return one


def test_headings_say_where_a_stretch_begins_and_where_other_cycles_start(panel):
    panel.set_cycle("dev", GRAPH)
    panel.set_state(sessions=[_dated("QA-1", "dev", 0), _dated("QA-2", "dev", 40),
                              _dated("OPS-1", "ops", 0)])
    assert panel.headings()[0] == "Today"
    assert "Other cycles" in panel.headings()
    assert panel.headings()[panel.headings().index("Other cycles") + 1] == "Today"
    assert panel.row_ids() == [cs.NEW_RUN, "dev:QA-1", "dev:QA-2", "ops:OPS-1"]


def test_more_on_the_core_puts_a_row_at_the_foot_that_asks_for_them(panel):
    asked = []
    panel.more_requested.connect(lambda: asked.append(True))
    panel.set_state(sessions=[session("QA-1")], more=True, total=80)
    assert panel.row_ids()[-1] == cs.MORE
    assert "79 older" in panel.list.item(panel.list.count() - 1).text()

    panel._clicked(panel.list.item(panel.list.count() - 1))
    panel._clicked(panel.list.item(panel.list.count() - 1))
    assert asked == [True], "asked once, not again while it is loading"
    assert panel.list.item(panel.list.count() - 1).text().startswith("Loading 50")


def test_scrolling_to_the_end_asks_for_the_next_page(panel):
    asked = []
    panel.more_requested.connect(lambda: asked.append(True))
    panel.resize(300, 200)
    panel.show()
    panel.set_state(sessions=[_dated("QA-%d" % one, "dev", 0) for one in range(30)],
                    more=True, total=200)
    bar = panel.list.verticalScrollBar()
    assert bar.maximum() > 0
    bar.setValue(bar.maximum())
    assert asked == [True]


def test_the_next_page_goes_under_the_list_without_drawing_it_again(panel):
    panel.set_state(sessions=[_dated("QA-1", "dev", 0)], more=True, total=3)
    first = panel.list.itemWidget(_item(panel, 0))
    panel.append_sessions([_dated("QA-1", "dev", 0), _dated("QA-2", "dev", 45)],
                          more=False)
    assert panel.row_ids() == ["dev:QA-1", "dev:QA-2"], "no footer, no duplicate"
    assert panel.list.itemWidget(_item(panel, 0)) is first
    assert panel.headings()[-1] not in ("Today", "Yesterday")


def test_a_page_that_could_not_be_read_is_offered_again(panel):
    asked = []
    panel.more_requested.connect(lambda: asked.append(True))
    panel.set_state(sessions=[session("QA-1")], more=True, total=60)
    panel._ask_more()
    panel.more_failed()
    panel._ask_more()
    assert asked == [True, True]
