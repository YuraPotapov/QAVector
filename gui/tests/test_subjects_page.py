"""The Subjects page: one list of everything being worked on, any row opened in place."""

from PySide6.QtCore import Qt, QUrl
from PySide6.QtTest import QTest

from cms_gui import widgets
from cms_gui.pages import subjects as page_mod
from cms_gui.pages.subjects import (C_MARK, C_NOTE, C_STATE, C_SUBJECT, C_UPDATED,
                                    SubjectsPage)

LIVE = "r-live"


def one(sid, state="workflow_completed", needs_you=False, active=False, latest=None,
        resume=None, note=None, title="Title"):
    return {"id": sid, "key": sid, "kind": "task", "title": title, "state": state,
            "needs_you": needs_you, "active": active, "runs": 2, "started_at": 1,
            "updated_at": 2, "cycles": [{"id": "dev", "name": "Development"}],
            "note": note or {}, "notes": 1 if note else 0,
            "latest": latest if latest is not None else
            {"id": "r-" + sid, "cycle": "dev", "run_dir": "/runs/r-" + sid},
            "session": {"status": "success", "outcome": "workflow_completed",
                        "subject": {"key": sid}},
            "resume": resume or {}}


class Core:
    """Answers as the core's subject commands would, from memory."""

    offload = False

    def __init__(self, subjects=(), counts=None, fail=None):
        self.subjects = list(subjects)
        self.counts = counts
        self.fail = fail
        self.lists, self.histories, self.notes = [], [], []

    def cycle_subject_list(self, view="all", limit=0, offset=0):
        self.lists.append((view, limit, offset))
        if self.fail:
            return {"ok": False, "problems": [self.fail]}
        chosen = [s for s in self.subjects
                  if view == "all" or (view == "needs_you" and s["needs_you"])
                  or (view == "active" and s["active"])]
        page = chosen[offset:offset + limit] if limit else chosen[offset:]
        counts = self.counts or {"needs_you": sum(s["needs_you"] for s in self.subjects),
                                 "active": sum(s["active"] for s in self.subjects),
                                 "all": len(self.subjects)}
        return {"ok": True, "subjects": page, "total": len(chosen),
                "more": offset + len(page) < len(chosen), "counts": counts}

    def cycle_subject_history(self, subject_id, scope="all", limit=0):
        self.histories.append((subject_id, scope, limit))
        return {"ok": True, "subject": subject_id, "earlier": 3, "total": 4,
                "entries": [{"kind": "failed", "at": 5, "run": "r1", "run_dir": "/runs/r1",
                             "label": "Tests", "message": "exited 1",
                             "files": [{"name": "stdout.log",
                                        "path": "steps/tests/stdout.log"}]}],
                "runs": [{"run": "r1", "cycle": "dev", "run_dir": "/runs/r1",
                          "started_at": 1}], "files": []}

    def cycle_subject_note(self, subject_id, text):
        self.notes.append((subject_id, text))
        return {"ok": True}


class Settings:
    def __init__(self, folds=None):
        self._folds = dict(folds or {})

    def folds(self, page):
        return dict(self._folds.get(page, {}))

    def save_folds(self, page, states):
        self._folds[page] = dict(states)


def shown(qapp, core, settings=None, live="", busy=False):
    page = SubjectsPage(settings or Settings())
    page.resize(1240, 800)
    page.set_live(live, busy)
    page.set_core(core)
    page.show()
    page.refresh()
    qapp.processEvents()
    return page


def texts(page, column):
    return [page.table.item(row, column).text() for row in page.table.data_rows()]


# ------------------------------------------------------------------- the rows
def test_each_subject_is_a_row_saying_where_it_stands_and_who_worked_on_it(qapp):
    page = shown(qapp, Core([one("QA-1", note={"text": "Back tomorrow", "at": 1}),
                             one("QA-2", title="")]))
    assert texts(page, C_SUBJECT) == ["QA-1  Title", "QA-2"]
    assert texts(page, C_STATE) == ["Workflow completed"] * 2
    assert texts(page, C_NOTE) == ["Back tomorrow", "-"]
    assert page.count.text() == "2 subjects"
    assert page.table.item(0, C_UPDATED).text()


def test_the_page_opens_on_what_waits_for_you_when_anything_does(qapp):
    core = Core([one("QA-1", "asking", True, True), one("QA-2")])
    page = shown(qapp, core)
    assert core.lists[-1][0] == "needs_you"
    assert texts(page, C_SUBJECT) == ["QA-1  Title"]
    assert page.views.button("Needs you").text() == "Needs you 1"
    assert page.views.button("All").text() == "All 2"


def test_a_view_chosen_by_hand_is_kept(qapp):
    core = Core([one("QA-1", "asking", True, True), one("QA-2")])
    page = shown(qapp, core)
    page.views.set_current("All")
    qapp.processEvents()
    assert core.lists[-1][0] == "all" and len(texts(page, C_SUBJECT)) == 2
    page.refresh()
    assert core.lists[-1][0] == "all"


def test_more_than_a_page_is_offered_and_fetched(qapp, monkeypatch):
    monkeypatch.setattr(page_mod, "PAGE", 2)
    core = Core([one("QA-%d" % n) for n in range(5)])
    page = shown(qapp, core)
    assert page.more_button.isVisibleTo(page) and page.more_button.text() == "Show 2 more"
    page.more_button.click()
    qapp.processEvents()
    assert core.lists[-1] == ("all", 2, 2)
    assert len(texts(page, C_SUBJECT)) == 4


# ------------------------------------------------------------- the empty list
def test_every_way_of_being_empty_says_so_in_its_own_words(qapp):
    page = shown(qapp, Core([]))
    assert "No subjects yet" in page.empty.text() and page.empty.isVisible()

    page = shown(qapp, Core([one("QA-1")], counts={"needs_you": 0, "active": 0, "all": 1}))
    page.views.set_current("Needs you")
    qapp.processEvents()
    assert "Nothing is waiting for you" in page.empty.text()
    page.views.set_current("Active")
    qapp.processEvents()
    assert "Nothing is going or waiting" in page.empty.text()

    page.views.set_current("All")
    qapp.processEvents()
    page.search.box(C_SUBJECT).setText("nothing like it")
    qapp.processEvents()
    assert page.empty.text() == "No subject matches the search."


def test_a_list_the_core_could_not_read_says_why(qapp):
    page = shown(qapp, Core(fail="the index is unreadable"))
    assert "Subjects could not be read: the index is unreadable" in page.empty.text()


# ---------------------------------------------------------- opening in place
def test_a_row_opens_under_itself_across_every_column(qapp):
    core = Core([one("QA-1"), one("QA-2")])
    page = shown(qapp, core)

    page.table.cellClicked.emit(0, C_MARK)
    qapp.processEvents()

    assert page.opened() == ["QA-1"]
    assert page.table.is_detail_row(1)
    assert page.table.columnSpan(1, 0) == page.table.columnCount()
    assert texts(page, C_SUBJECT) == ["QA-1  Title", "QA-2  Title"]
    assert core.histories[-1] == ("QA-1", "conversation", page_mod.HISTORY_PAGE)
    detail = page.detail("QA-1")
    assert "Tests - exited 1" in detail.text.toPlainText()
    assert page.table.rowHeight(1) <= page.table.detail_cap()


def test_several_rows_may_be_open_and_which_is_remembered(qapp):
    settings = Settings()
    page = shown(qapp, Core([one("QA-1"), one("QA-2"), one("QA-3")]), settings)
    page.table.cellDoubleClicked.emit(0, C_SUBJECT)
    page.table.cellDoubleClicked.emit(page.table.data_rows()[2], C_SUBJECT)
    qapp.processEvents()
    assert page.opened() == ["QA-1", "QA-3"]
    assert settings.folds("subjects") == {"QA-1": True, "QA-3": True}

    again = shown(qapp, Core([one("QA-1"), one("QA-2"), one("QA-3")]), settings)
    assert again.opened() == ["QA-1", "QA-3"]


def test_an_open_row_stays_open_when_the_list_is_read_again(qapp):
    page = shown(qapp, Core([one("QA-1"), one("QA-2")]))
    page.table.cellClicked.emit(1, C_MARK)
    page.refresh()
    qapp.processEvents()
    assert page.opened() == ["QA-2"]


def test_the_keyboard_opens_and_closes_a_row_and_steps_over_its_details(qapp):
    page = shown(qapp, Core([one("QA-1"), one("QA-2")]))
    page.table.setFocus()
    page.table.setCurrentCell(0, C_SUBJECT)
    QTest.keyClick(page.table, Qt.Key_Right)
    qapp.processEvents()
    assert page.opened() == ["QA-1"]
    QTest.keyClick(page.table, Qt.Key_Down)
    assert page.table.currentRow() == 2              # QA-2, past the details
    QTest.keyClick(page.table, Qt.Key_Up)
    assert page.table.currentRow() == 0
    QTest.keyClick(page.table, Qt.Key_Left)
    qapp.processEvents()
    assert page.opened() == []
    QTest.keyClick(page.table, Qt.Key_Return)
    qapp.processEvents()
    assert page.opened() == ["QA-1"]


def test_a_search_hides_an_open_row_with_its_details(qapp):
    page = shown(qapp, Core([one("QA-1"), one("QA-2")]))
    page.table.cellClicked.emit(0, C_MARK)
    page.search.box(C_SUBJECT).setText("QA-2")
    qapp.processEvents()
    assert page.table.isRowHidden(0) and page.table.isRowHidden(1)
    assert not page.table.isRowHidden(2)
    page.search.box(C_SUBJECT).setText("QA-1")
    qapp.processEvents()
    assert not page.table.isRowHidden(0) and not page.table.isRowHidden(1)


# ------------------------------------------------------- what an open row does
def _open(qapp, page, sid):
    page.table.cellClicked.emit(page._row_of(sid), C_MARK)
    qapp.processEvents()
    return page.detail(sid)


def _live_model(approval=False):
    """The window's run model, holding the live run - with a node waiting, if asked."""
    from cms_gui.runner import RunState

    state = RunState()
    state.handle({"kind": "cycle.run.start", "run_id": LIVE, "cycle": "dev", "name": "Dev",
                  "execution_mode": "approve", "graph": {"nodes": [
                      {"id": "plan", "label": "Plan it", "plugin": "agent.review", "needs": []},
                      {"id": "push", "label": "Push the branch", "plugin": "command.shell",
                       "needs": ["plan"]}]}})
    state.handle({"kind": "cycle.step.end", "step": "plan", "status": "success",
                  "active_elapsed_ms": 1000, "message": "Planned"})
    if approval:
        state.handle({"kind": "cycle.node.approval", "run_id": LIVE, "receipt": {
            "id": "req-1", "fingerprint": "proof", "step": "push", "status": "pending",
            "label": "Push the branch", "purpose": "Publish the work",
            "resolved_inputs": {"remote": "origin"}}})
    return state


def _asking(state="asking"):
    return one("QA-1", state, True, True,
               latest={"id": LIVE, "running": True, "cycle": "dev", "run_dir": "/runs/live"})


def test_a_question_in_the_live_run_is_answered_in_the_row_s_cockpit_and_it_can_be_parked(qapp):
    page = shown(qapp, Core([_asking()]), live=LIVE, busy=True)
    page.set_run_state(_live_model())
    detail = _open(qapp, page, "QA-1")
    told = []
    page.park_requested.connect(lambda run: told.append(("park", run)))
    assert detail.next_button.text() == "Answer"
    assert detail.park_button.isVisibleTo(detail)
    detail.next_button.click()
    assert detail.tab == "Cockpit" and detail.stage_run() == LIVE
    assert detail.body.currentWidget() is detail.cockpit_host
    assert detail.cockpit.isVisibleTo(detail) and "Plan it" in detail.cockpit.browser.toPlainText()
    detail.park_button.click()
    assert told == [("park", LIVE)]


def test_a_node_waiting_for_approval_is_approved_in_the_row(qapp):
    page = shown(qapp, Core([_asking("node_approval")]), live=LIVE, busy=True)
    page.set_run_state(_live_model(approval=True))
    detail = _open(qapp, page, "QA-1")
    answered = []
    page.approval_answered.connect(lambda *args: answered.append(args))
    detail.next_button.click()
    actions = detail.cockpit.approval_actions["req-1"]
    assert actions.isVisibleTo(detail)
    actions.approve_button.click()
    assert answered == [("req-1", "proof", True)]


def test_the_live_cockpit_follows_the_model_and_its_questions(qapp):
    state = _live_model()
    page = shown(qapp, Core([_asking()]), live=LIVE, busy=True)
    page.set_run_state(state)
    detail = _open(qapp, page, "QA-1")
    detail.next_button.click()
    assert detail.cockpit.question_actions == {}

    asked = []
    page.question_requested.connect(asked.append)
    page.set_questions([{"id": "3", "step": "push", "question": "Push now?", "since": 1.0}])
    assert "Push now?" in detail.cockpit.browser.toPlainText()
    detail.cockpit.question_actions["3"].show_button.click()
    assert asked == ["3"]

    state.handle({"kind": "cycle.step.start", "step": "push", "started_at": 5,
                  "active_elapsed_ms": 0})
    qapp.processEvents()
    assert detail.cockpit.current.step_id == "push", "redrawn once the model moved"
    assert page._cockpit_tick.isActive(), "and every second while the run is going"

    detail.tabs.set_current("History")
    assert "QA-1" not in page._cockpits
    page.table.cellClicked.emit(page._row_of("QA-1"), C_MARK)
    page._refresh_cockpits()
    assert not page._cockpit_tick.isActive()


def test_a_step_clicked_in_a_cockpit_is_shown_on_the_canvas(qapp):
    page = shown(qapp, Core([_asking()]), live=LIVE, busy=True)
    page.set_run_state(_live_model())
    detail = _open(qapp, page, "QA-1")
    detail.next_button.click()
    asked = []
    page.step_requested.connect(lambda *args: asked.append(args))
    detail.cockpit.node_selected.emit("plan")
    assert asked == [("dev", "/runs/live", LIVE, "plan")]


def test_a_past_run_s_cockpit_is_what_it_recorded_and_answers_nothing(qapp, tmp_path):
    runs = [{"run": "r1", "cycle": "dev", "cycle_name": "Dev", "run_dir": _run_dir(tmp_path),
             "started_at": 1}]
    page = shown(qapp, Staged(runs, [one("QA-1")]))
    detail = _open(qapp, page, "QA-1")
    detail.tabs.set_current("Cockpit")
    qapp.processEvents()
    assert detail.stage_runs.isVisibleTo(detail) and detail.stage_runs.currentData() == "r1"
    assert "Cockpit" in detail.stage_runs.toolTip()
    assert detail.cockpit.isVisibleTo(detail)
    assert "COMPLETED" in detail.cockpit.browser.toPlainText()
    assert detail.cockpit.approval_actions == {} and detail.cockpit.question_actions == {}
    asked = []
    page.step_requested.connect(lambda *args: asked.append(args))
    detail.cockpit.node_selected.emit("plan")
    assert asked == [("dev", runs[0]["run_dir"], "r1", "plan")]
    # Stages of the same run come from the same reading.
    detail.tabs.set_current("Stages")
    assert detail.recorded.titles() == ["Thinking", "Answered"]


def test_saved_cockpit_result_opens_its_file_without_switching_to_the_graph(qapp, tmp_path):
    run_dir = _run_dir(tmp_path)
    from pathlib import Path

    report = Path(run_dir) / "result.html"
    report.write_text("<h1>Saved result</h1>", encoding="utf-8")
    runs = [{"run": "r1", "cycle": "dev", "run_dir": run_dir, "started_at": 1}]
    page = shown(qapp, Staged(runs, [one("QA-1")]))
    detail = _open(qapp, page, "QA-1")
    detail.tabs.set_current("Cockpit")
    qapp.processEvents()
    cycle = detail.cockpit._render_context[0]
    cycle["steps"]["plan"].update(status="success", artifacts=[{"path": "result.html"}])
    files, nodes = [], []
    page.artifact_requested.connect(lambda *args: files.append(args))
    page.step_requested.connect(lambda *args: nodes.append(args))
    detail.cockpit.browser.anchorClicked.emit(QUrl("result:plan"))
    assert files == [(run_dir, "result.html")]
    assert nodes == []


def test_a_cockpit_of_a_run_nobody_knows_the_folder_of_says_so(qapp):
    page = shown(qapp, Staged([{"run": "r9", "cycle": "dev", "run_dir": "", "started_at": 1}],
                              [one("QA-1")]))
    detail = _open(qapp, page, "QA-1")
    detail.tabs.set_current("Cockpit")
    assert not detail.cockpit.isVisibleTo(detail)
    assert "folder is not known" in detail.cockpit_note.label.text()


def test_a_section_in_a_window_keeps_its_cockpit(qapp):
    page = shown(qapp, Core([_asking()]), live=LIVE, busy=True)
    page.set_run_state(_live_model())
    detail = _open(qapp, page, "QA-1")
    detail.next_button.click()
    detail.detach_button.click()
    qapp.processEvents()
    moved = page.detail("QA-1")
    assert moved is not detail and moved.tab == "Cockpit" and moved.stage_run() == LIVE
    assert "Plan it" in moved.cockpit.browser.toPlainText()
    page.hide()
    page._refresh_cockpits()
    assert page._cockpit_tick.isActive(), "a window is still on screen while the page is not"
    page.shut_windows()


def test_a_stopped_run_is_resumed_unless_the_window_is_busy(qapp):
    offer = {"run_id": "r-1", "cycle": "dev", "run_dir": "/runs/r-1", "problem": ""}
    page = shown(qapp, Core([one("QA-1", "cancelled", resume=offer)]))
    detail = _open(qapp, page, "QA-1")
    asked = []
    page.resume_requested.connect(lambda *args: asked.append(args))
    assert detail.next_button.text() == "Resume" and detail.next_button.isEnabled()
    assert not detail.park_button.isVisibleTo(detail)
    detail.next_button.click()
    assert asked == [("dev", "/runs/r-1", "r-1")]

    page.set_live("r-other", busy=True)
    assert not detail.next_button.isEnabled()
    assert "Park or stop it first" in detail.next_button.toolTip()


def test_a_run_resume_would_refuse_is_opened_and_the_reason_is_shown(qapp):
    offer = {"run_id": "r-1", "cycle": "dev", "run_dir": "/runs/r-1",
             "problem": "The cycle's steps changed since this run."}
    page = shown(qapp, Core([one("QA-1", "failed", resume=offer)]))
    detail = _open(qapp, page, "QA-1")
    opened = []
    page.open_run_requested.connect(lambda *args: opened.append(args))
    assert detail.next_button.text() == "Open run"
    assert detail.problem.isVisibleTo(detail) and "steps changed" in detail.problem.text()
    detail.next_button.click()
    assert opened == [("dev", "/runs/r-QA-1")]


def test_a_file_in_the_history_opens_on_the_artifacts_page(qapp):
    page = shown(qapp, Core([one("QA-1")]))
    detail = _open(qapp, page, "QA-1")
    wanted = []
    page.artifact_requested.connect(lambda *args: wanted.append(args))
    index = next(n for n, action in enumerate(detail.actions) if action.get("do") == "file")
    detail.text.anchorClicked.emit(QUrl("a:%d" % index))
    assert wanted == [("/runs/r1", "steps/tests/stdout.log")]


def test_earlier_entries_and_the_other_scope_are_asked_for_again(qapp):
    core = Core([one("QA-1")])
    page = shown(qapp, core)
    detail = _open(qapp, page, "QA-1")
    detail.text.anchorClicked.emit(QUrl("a:0"))      # "Show 3 earlier entries"
    assert core.histories[-1] == ("QA-1", "conversation",
                                  page_mod.HISTORY_PAGE + page_mod.HISTORY_MORE)
    detail.scopes.set_current("All")
    assert core.histories[-1] == ("QA-1", "all", page_mod.HISTORY_PAGE)


def test_a_note_is_kept_and_the_list_read_again(qapp, monkeypatch):
    core = Core([one("QA-1")])
    page = shown(qapp, core)
    detail = _open(qapp, page, "QA-1")
    monkeypatch.setattr(widgets, "ask_text", lambda *a, **k: "Waiting for the analyst")
    before = len(core.lists)
    detail.note_button.click()
    assert core.notes == [("QA-1", "Waiting for the analyst")]
    assert len(core.lists) > before


def test_a_note_refused_or_left_empty_writes_nothing(qapp, monkeypatch):
    core = Core([one("QA-1")])
    page = shown(qapp, core)
    detail = _open(qapp, page, "QA-1")
    for answer in (None, ""):
        monkeypatch.setattr(widgets, "ask_text", lambda *a, answer=answer, **k: answer)
        detail.note_button.click()
    assert core.notes == []


# ----------------------------------------------------------------- finding one
def test_search_finds_a_subject_and_opens_it_even_behind_another_view(qapp):
    core = Core([one("QA-1", "asking", True, True), one("QA-2")])
    page = shown(qapp, core)
    assert [hit.target for hit in page.search_rows()] == [("subject", "QA-1")]
    assert page.show_found(("subject", "QA-2")) is True
    qapp.processEvents()
    assert page.views.current() == "All" and "QA-2" in page.opened()


# ------------------------------------------------------------------ symmetry
def test_titles_marks_and_search_boxes_line_up_with_their_columns(qapp):
    page = shown(qapp, Core([one("QA-%d" % n) for n in range(4)]))
    table, header = page.table, page.table.horizontalHeader()
    assert header.defaultAlignment() & Qt.AlignLeft
    assert page.search.box(C_MARK) is None
    for column in range(1, table.columnCount()):
        box = page.search.box(column)
        assert box.geometry().x() == header.sectionViewportPosition(column)
        assert box.geometry().width() == header.sectionSize(column) + 1
    heights = {table.rowHeight(row) for row in table.data_rows()}
    assert len(heights) == 1
    # The open/closed mark is centred in its column, on every row alike.
    centres = {table.visualRect(table.model().index(row, C_MARK)).center().x()
               for row in table.data_rows()}
    assert len(centres) == 1
    mark = table.visualRect(table.model().index(0, C_MARK))
    assert abs(mark.center().x() - (header.sectionViewportPosition(C_MARK)
                                    + header.sectionSize(C_MARK) // 2)) <= 1


def test_any_cycle_can_be_started_on_a_subject_with_a_key(qapp):
    page = shown(qapp, Core([one("QA-1"), dict(one("run:r9"), key="", title="")]))
    detail = _open(qapp, page, "QA-1")
    started = []
    page.run_on_requested.connect(started.append)
    assert detail.run_on_button.isVisibleTo(detail) and detail.run_on_button.isEnabled()
    detail.run_on_button.click()
    assert started[0]["key"] == "QA-1" and started[0]["cycles"][0]["id"] == "dev"

    alone = _open(qapp, page, "run:r9")
    assert not alone.run_on_button.isVisibleTo(alone)

    page.set_live("r-other", busy=True)
    assert not detail.run_on_button.isEnabled()


def test_an_agent_s_account_is_read_when_asked_for_and_shown_in_place(qapp):
    class Told(Core):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.transcripts = []

        def cycle_subject_history(self, subject_id, scope="all", limit=0):
            answer = super().cycle_subject_history(subject_id, scope, limit)
            answer["entries"] = [{"kind": "agent", "at": 1, "run": "r1", "step": "plan",
                                  "run_dir": "/runs/r1", "label": "Plan it",
                                  "status": "success", "summary": "Two packages."}]
            answer["earlier"] = 0
            return answer

        def cycle_subject_transcript(self, run_id, step_id):
            self.transcripts.append((run_id, step_id))
            return {"ok": True, "cut": 0, "calls": [{"prompt": "", "stages": [
                {"kind": "text", "detail": "The resolution field."}]}]}

    core = Told([one("QA-1")])
    page = shown(qapp, core)
    detail = _open(qapp, page, "QA-1")
    index = next(n for n, action in enumerate(detail.actions)
                 if action.get("do") == "transcript")
    detail.text.anchorClicked.emit(QUrl("a:%d" % index))
    assert core.transcripts == [("r1", "plan")]
    assert "Said: The resolution field." in detail.text.toPlainText()
    detail.text.anchorClicked.emit(QUrl("a:%d" % index))           # hide it again
    assert "Said: The resolution field." not in detail.text.toPlainText()
    assert core.transcripts == [("r1", "plan")]                   # not read twice


def test_a_subject_not_paged_in_yet_is_found_in_the_whole_list(qapp, monkeypatch):
    monkeypatch.setattr(page_mod, "PAGE", 2)
    core = Core([one("QA-%d" % n) for n in range(5)])
    page = shown(qapp, core)
    assert page._row_of("QA-4") is None
    assert page.reveal("QA-4") is True
    qapp.processEvents()
    assert core.lists[-1] == ("all", 0, 0)
    assert "QA-4" in page.opened()
    # With the whole list read, a subject it does not hold is not looked for again.
    asked = len(core.lists)
    assert page.reveal("QA-404") is False
    assert len(core.lists) == asked


# ------------------------------------------------------------------------ Stages
class Lender:
    """The Cycles page's side of lending the live Stages panel."""

    def __init__(self, detached=False):
        from PySide6.QtWidgets import QLabel

        self.panel = QLabel("the live stages")
        self.home = True
        self.is_detached = detached
        self.windows = 0
        self.reclaim = None

    def lend(self, reclaim):
        if not self.home or self.is_detached:
            return None
        self.home, self.reclaim = False, reclaim
        return self.panel

    def take_back(self, panel):
        assert panel is self.panel
        self.home = True
        panel.setParent(None)

    def detached(self):
        return self.is_detached

    def show_window(self):
        self.windows += 1


def _run_dir(tmp_path, run_id="r1"):
    import json

    where = tmp_path / run_id
    (where / "logs").mkdir(parents=True)
    events = [{"kind": "cycle.run.start", "run_id": run_id, "cycle": "dev", "name": "Dev",
               "graph": {"nodes": [{"id": "plan", "label": "Plan it", "plugin": "agent.review"}]}},
              {"kind": "cycle.step.stage", "run_id": run_id, "step": "plan", "phase": "thinking",
               "title": "Thinking", "detail": "Which field?", "status": "done"},
              {"kind": "cycle.step.stage", "run_id": run_id, "step": "plan", "phase": "text",
               "title": "Answered", "detail": "The resolution field.", "status": "done"},
              {"kind": "cycle.run.end", "run_id": run_id, "status": "success"}]
    (where / "logs" / "cycle.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return str(where)


class Staged(Core):
    def __init__(self, runs, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.runs = runs

    def cycle_subject_history(self, subject_id, scope="all", limit=0):
        answer = super().cycle_subject_history(subject_id, scope, limit)
        answer["runs"] = self.runs
        return answer


def test_a_past_run_s_stages_are_read_back_from_what_it_recorded(qapp, tmp_path):
    runs = [{"run": "r1", "cycle": "dev", "cycle_name": "Dev", "run_dir": _run_dir(tmp_path),
             "started_at": 1}]
    page = shown(qapp, Staged(runs, [one("QA-1")]))
    page.set_stages_lender(Lender())
    detail = _open(qapp, page, "QA-1")
    detail.tabs.set_current("Stages")
    qapp.processEvents()
    assert detail.stage_runs.isVisibleTo(detail) and detail.stage_runs.currentData() == "r1"
    assert detail.recorded.titles() == ["Thinking", "Answered"]
    row = page._row_of("QA-1")
    assert page.table.rowHeight(row + 1) == page.table.detail_cap(row)
    assert page.table.detail_cap(row) == (page.table.viewport().height()
                                          - page.table.rowHeight(row)), "the whole table"
    detail.tabs.set_current("History")
    assert page.table.detail_cap(row) < page.table.viewport().height() - page.table.rowHeight(row)


def test_the_live_run_s_stages_are_the_live_panel_lent_and_always_given_back(qapp, tmp_path):
    runs = [{"run": LIVE, "cycle": "dev", "cycle_name": "Dev", "run_dir": "/runs/live",
             "started_at": 2},
            {"run": "r1", "cycle": "dev", "cycle_name": "Dev", "run_dir": _run_dir(tmp_path),
             "started_at": 1}]
    live = one("QA-1", "running", active=True,
               latest={"id": LIVE, "running": True, "cycle": "dev", "run_dir": "/runs/live"})
    core = Staged(runs, [live, one("QA-2")])
    page = shown(qapp, core, live=LIVE, busy=True)
    lender = Lender()
    page.set_stages_lender(lender)
    detail = _open(qapp, page, "QA-1")
    detail.tabs.set_current("Stages")
    assert detail.stage_runs.currentText().startswith("Run 2 (live)")
    assert detail.holds_panel() and not lender.home

    # The list made anew - another subject's order changed - gives it back
    # first, and the row reopened on Stages takes it again.
    core.subjects.reverse()
    page.refresh()
    qapp.processEvents()
    assert page.detail("QA-1").holds_panel() and not lender.home

    # Another run picked: the panel goes home, the recorded stages show.
    detail = page.detail("QA-1")
    detail.stage_runs.setCurrentIndex(detail.stage_runs.findData("r1"))
    assert lender.home and detail.recorded.titles() == ["Thinking", "Answered"]
    detail.stage_runs.setCurrentIndex(detail.stage_runs.findData(LIVE))
    assert not lender.home

    # Leaving the page, leaving the tab, closing the row: home every time.
    page.hide()
    assert lender.home
    page.show()
    qapp.processEvents()
    assert not lender.home
    detail.tabs.set_current("History")
    assert lender.home
    detail.tabs.set_current("Stages")
    assert not lender.home
    page.table.cellClicked.emit(page._row_of("QA-1"), C_MARK)
    assert lender.home


def test_the_cycles_page_asking_for_its_panel_back_gets_it(qapp):
    live = one("QA-1", "running", active=True,
               latest={"id": LIVE, "running": True, "cycle": "dev", "run_dir": "/runs/live"})
    page = shown(qapp, Staged([{"run": LIVE, "run_dir": "/runs/live", "started_at": 1}],
                              [live]), live=LIVE, busy=True)
    lender = Lender()
    page.set_stages_lender(lender)
    detail = _open(qapp, page, "QA-1")
    detail.tabs.set_current("Stages")
    lender.reclaim()
    assert lender.home and not detail.holds_panel()
    assert "back on the Cycles page" in detail.stage_note.label.text()


def test_stages_in_a_window_of_their_own_are_pointed_to_not_taken(qapp):
    live = one("QA-1", "running", active=True,
               latest={"id": LIVE, "running": True, "cycle": "dev", "run_dir": "/runs/live"})
    page = shown(qapp, Staged([{"run": LIVE, "run_dir": "/runs/live", "started_at": 1}],
                              [live]), live=LIVE, busy=True)
    lender = Lender(detached=True)
    page.set_stages_lender(lender)
    detail = _open(qapp, page, "QA-1")
    detail.tabs.set_current("Stages")
    assert lender.home and not detail.holds_panel()
    assert "separate window" in detail.stage_note.label.text()
    detail.stage_note.show_button.click()
    assert lender.windows == 1


# ------------------------------------------------------- a section in its own window
def test_a_section_opens_in_a_window_of_its_own_and_the_row_says_where(qapp):
    from cms_gui.pages.subjects import SubjectWindow

    page = shown(qapp, Core([one("QA-1"), one("QA-2")]))
    detail = _open(qapp, page, "QA-1")
    assert detail.detach_button.text() == "" and detail.detach_button.toolTip()
    assert detail.detach_button.toolButtonStyle() == Qt.ToolButtonIconOnly
    detail.tabs.set_current("Runs")
    detail.detach_button.click()
    qapp.processEvents()

    window = page.window_of("QA-1")
    assert isinstance(window, SubjectWindow) and window.isVisible()
    moved = page.detail("QA-1")
    assert moved is not detail and moved.window() is window and moved.detached
    assert moved.tab == "Runs", "it opens on what the row was showing"
    held = page.table.detail_of(page._row_of("QA-1"))
    assert "separate window" in held.label.text()
    assert moved.answer is not None, "its history is there at once, from what was read"

    # The list read again keeps the window, and the row keeps pointing to it.
    page.refresh()
    qapp.processEvents()
    assert page.window_of("QA-1") is window and page.detail("QA-1") is moved
    assert "separate window" in page.table.detail_of(page._row_of("QA-1")).label.text()

    # A person closing the window puts the section back in its row, as it was.
    window.close()
    qapp.processEvents()
    assert page.window_of("QA-1") is None
    back = page.detail("QA-1")
    assert back is not moved and not back.detached and back.tab == "Runs"
    assert page.table.detail_of(page._row_of("QA-1")) is back


def test_the_mark_in_a_window_brings_it_back_and_closing_the_row_closes_it(qapp):
    page = shown(qapp, Core([one("QA-1")]))
    _open(qapp, page, "QA-1").detach_button.click()
    moved = page.detail("QA-1")
    assert "Back into the list" in moved.detach_button.toolTip()
    moved.detach_button.click()
    assert page.window_of("QA-1") is None and not page.detail("QA-1").detached

    page.detail("QA-1").detach_button.click()
    window = page.window_of("QA-1")
    page.table.cellClicked.emit(page._row_of("QA-1"), C_MARK)
    qapp.processEvents()
    assert page.window_of("QA-1") is None and not window.isVisible()
    assert page.opened() == []


def test_a_window_keeps_the_live_stages_while_the_page_is_away(qapp):
    runs = [{"run": LIVE, "cycle": "dev", "run_dir": "/runs/live", "started_at": 1}]
    live = one("QA-1", "running", active=True,
               latest={"id": LIVE, "running": True, "cycle": "dev", "run_dir": "/runs/live"})
    page = shown(qapp, Staged(runs, [live]), live=LIVE, busy=True)
    lender = Lender()
    page.set_stages_lender(lender)
    detail = _open(qapp, page, "QA-1")
    detail.tabs.set_current("Stages")
    assert detail.holds_panel()
    detail.detach_button.click()
    qapp.processEvents()
    moved = page.detail("QA-1")
    assert moved.holds_panel() and not lender.home
    page.hide()
    assert moved.holds_panel() and not lender.home, "still on screen, in its window"
    page.shut_windows()
    assert lender.home
