"""Subjects: everything being worked on, one row each, opened in place.

A long piece of work is left and come back to - the plan approved before
lunch, the review answered tomorrow, a second task started in between. Coming
back means knowing three things at once: where it stopped, what happened on the
way there, and what to do next. The Cycles page answers them one cycle at a
time; this answers them for the thing itself, whichever cycles worked on it.

**One list, opened in place.** Every subject is a row, and a row opens right
where it is, across the whole table (``widgets.Table(expandable=True)``),
rather than into a page beside the list: what somebody came to the list for is
to compare and to switch, and the rows around the open one stay in sight. Any
number may be open at once, and which are open is remembered.

An open row says what to do next as one button - answer the question in the
run's Cockpit, resume where it stopped, or open the run - and offers **Park**
for a run going in this window: it stops at a point where nothing is lost and
can be resumed from here later, which is what frees the window for another
subject. Under that are the person's own note, and the subject's history from
every run, as a conversation or with everything the steps did around it.

Nothing here decides anything about a run. Answers are given in the Cockpit,
runs are opened on the Cycles page, files on the Artifacts page; this page
only says where each of those is.
"""

import html

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView,
                               QLabel, QPushButton, QSizePolicy, QStackedWidget,
                               QTableWidgetItem, QTextBrowser, QToolButton, QVBoxLayout,
                               QWidget)

from .. import (background, core as core_mod, cyclereplay, cyclesubjects, icons, search,
                theme, widgets)
from .. import subjecthistory as story
from ..stages import StageList
from ..choicetable import text_inset

COLUMNS = ["", "Subject", "State", "Cycle", "Note", "Updated"]
C_MARK, C_SUBJECT, C_STATE, C_CYCLE, C_NOTE, C_UPDATED = range(len(COLUMNS))
#: The width of the column the open/closed mark sits in.
MARK_WIDTH = 28

#: The list's three views, as the core names them and as the buttons say them.
NEEDS_YOU, ACTIVE, ALL = "needs_you", "active", "all"
VIEW_LABELS = {NEEDS_YOU: "Needs you", ACTIVE: "Active", ALL: "All"}
VIEWS = (NEEDS_YOU, ACTIVE, ALL)

#: Subjects asked for at a time, and history entries an opened row starts with.
PAGE = 50
HISTORY_PAGE = 10
#: How many more history entries "show earlier" asks for each time.
HISTORY_MORE = 50
#: How long the list waits after something happened in a run before reading
#: itself again: a run moving step by step should not reread it on every step.
REFRESH_MS = 1500

#: The key a row's subject id is kept under, on its Subject cell.
ROLE_ID = Qt.UserRole
#: The settings page under which open rows are remembered (see Settings.folds).
FOLDS = "subjects"


class SubjectDetail(QWidget):
    """What one row holds when it is open: next step, note, history.

    It asks for nothing itself. Every click becomes ``acted(verb, payload)`` and
    the page decides what happens; the history it shows is handed in with
    :meth:`set_answer`. ``resized`` says its height may have changed, so the
    table can make room.
    """

    acted = Signal(str, object)
    resized = Signal()

    def __init__(self, subject, parent=None):
        super().__init__(parent)
        self.setProperty("role", "subjectdetail")
        # A QWidget paints no background from a stylesheet without this.
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.subject = subject
        self.tab = story.HISTORY
        self.scope = story.CONVERSATION
        self.opened = set()
        #: Agents whose own account is shown, by entry, and the accounts read so
        #: far by (run, step) - None while one is on its way.
        self.told = set()
        self.transcripts = {}
        self.answer = None
        self.actions = []
        #: Whether the history is held at its newest entry: until the person
        #: scrolls up, a conversation stays where it got to, through every
        #: redraw and every change of the row's height.
        self._at_end = True

        column = QVBoxLayout(self)
        column.setContentsMargins(14, 8, 12, 10)
        column.setSpacing(6)

        self.next_button = QPushButton("")
        self.next_button.setProperty("variant", "primary")
        self.next_button.clicked.connect(self._next)
        self.park_button = icons.button(QPushButton("Park"), "pause")
        self.park_button.setToolTip(
            "Stop the run at a point where nothing is lost and keep it to resume\n"
            "from here later - which frees this window for another subject.")
        self.park_button.clicked.connect(lambda: self.acted.emit("park", None))
        self.run_on_button = icons.button(QPushButton("Run a cycle on it..."), "cycles")
        self.run_on_button.setProperty("variant", "ghost")
        self.run_on_button.setToolTip(
            "Start any cycle on this subject - a single agent to ask something,\n"
            "a review - so what it says joins this history.")
        self.run_on_button.clicked.connect(lambda: self.acted.emit("run_on", None))
        self.meta = QLabel("")
        self.meta.setProperty("role", "mono")
        # Only its mark, the Stages rule: the words are in the tooltip.
        self.detach_button = icons.button(QToolButton(), "popout")
        self.detach_button.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.detach_button.setProperty("variant", "icon")
        self.detach_button.setCursor(Qt.PointingHandCursor)
        self.detach_button.clicked.connect(
            lambda: self.acted.emit("dock" if self.detached else "detach", None))
        self.detached = False
        self.set_detached(False)
        self._top = widgets.row(self.next_button, self.park_button, self.run_on_button,
                                None, self.meta, self.detach_button)
        column.addWidget(self._top)

        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.note.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.note_button = icons.button(QPushButton("Note"), "plus")
        self.note_button.setProperty("variant", "ghost")
        self.note_button.setToolTip("Write down where you stopped or what comes next.")
        self.note_button.clicked.connect(lambda: self.acted.emit("note", None))
        self._note_row = widgets.row(self.note, self.note_button, stretch_last=False)
        column.addWidget(self._note_row)

        self.problem = QLabel("")
        self.problem.setWordWrap(True)
        self.problem.setProperty("role", "hint")
        column.addWidget(self.problem)

        self.tabs = widgets.Segmented(list(story.TABS), story.HISTORY)
        self.tabs.changed.connect(self._tab)
        self.scopes = widgets.Segmented(
            [story.SCOPE_LABELS[story.CONVERSATION], story.SCOPE_LABELS[story.ALL]],
            story.SCOPE_LABELS[story.CONVERSATION])
        self.scopes.changed.connect(self._scope)
        self._scope_label = QLabel("Show")
        self._scope_label.setProperty("role", "hint")
        # Which run's Stages: the one going in this window is the live feed,
        # any other is what it recorded.
        self.stage_runs = widgets.capped(QComboBox())
        self.stage_runs.setToolTip("Whose stages to show: the live run's as they come, "
                                   "any other run's as it recorded them.")
        self.stage_runs.currentIndexChanged.connect(lambda _index: self._stage_run_changed())
        self._tab_row = widgets.row(self.tabs, None, self._scope_label, self.scopes,
                                    self.stage_runs)
        column.addWidget(self._tab_row)

        for band in (self._top, self._note_row, self._tab_row, self.tabs, self.scopes):
            band.setProperty("role", "subjectband")

        self.text = QTextBrowser()
        self.text.setOpenLinks(False)
        self.text.setFrameShape(QTextBrowser.NoFrame)
        self.text.anchorClicked.connect(self._link)
        self.text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        bar = self.text.verticalScrollBar()
        bar.valueChanged.connect(self._scrolled_to)
        bar.rangeChanged.connect(lambda _low, _high: self._hold_end())
        self.skeleton = widgets.Skeleton(self.text, "lines")

        # Stages: the live panel lent by the Cycles page, a recorded run's
        # stages, or a line saying where they are.
        self.stage_host = QWidget()
        self._stage_layout = QVBoxLayout(self.stage_host)
        self._stage_layout.setContentsMargins(0, 4, 0, 0)
        self._stage_layout.setSpacing(0)
        self.recorded = StageList()
        self._stage_layout.addWidget(self.recorded, 1)
        self.stage_note = widgets.Elsewhere("")
        self.stage_note.show_requested.connect(lambda: self.acted.emit("stages_window", None))
        self._stage_layout.addWidget(self.stage_note)
        self.stage_note.hide()
        self._panel = None
        self._live_run = ""
        self._stage_choice = ""

        self.body = QStackedWidget()
        self.body.addWidget(self.text)
        self.body.addWidget(self.stage_host)
        column.addWidget(self.body, 1)
        self._sync_tab()
        self.update_subject(subject)

    # -- what it shows ------------------------------------------------------------
    def update_subject(self, subject, live_run="", busy=False):
        """The row's subject as the list now has it, and what this window is doing."""
        self.subject = subject
        if live_run != self._live_run:
            self._live_run = live_run
            self._fill_stage_runs()
        action = story.next_action(subject, live_run, busy)
        self._action = action
        if action is None:
            self.next_button.hide()
        else:
            verb, label, enabled, why = action
            self.next_button.setText(label)
            self.next_button.setEnabled(enabled)
            self.next_button.setToolTip(why)
            icons.button(self.next_button, {story.RESUME: "run", story.COCKPIT: "dot"}
                         .get(verb, "browse"))
            self.next_button.show()
        self.park_button.setVisible(story.can_park(subject, live_run))
        # Only a subject with a key can be named to a new run; a run that stands
        # alone has nothing to start another one on.
        self.run_on_button.setVisible(bool(subject.get("key")))
        self.run_on_button.setEnabled(not busy)
        self.run_on_button.setToolTip(
            "A run is going in this window." if busy else
            "Start any cycle on this subject - a single agent to ask something,\n"
            "a review - so what it says joins this history.")
        self.meta.setText(story.meta_text(subject))
        note = subject.get("note") or {}
        if note.get("text"):
            self.note.setText("<span style='color:%s'>Note - %s</span>&nbsp; %s" % (
                theme.NEUTRAL[500], cyclesubjects.when(note.get("at")),
                html.escape(note["text"]).replace("\n", "<br>")))
        else:
            self.note.setText("<span style='color:%s'>No note yet.</span>"
                              % theme.NEUTRAL[500])
        problem = (subject.get("resume") or {}).get("problem") or ""
        self.problem.setText(problem)
        self.problem.setVisible(bool(problem))
        self.resized.emit()

    def set_detached(self, detached):
        """In a window of its own, or in its row: what the mark offers."""
        self.detached = bool(detached)
        icons.button(self.detach_button, "dock" if detached else "popout")
        tip = ("Back into the list.\nClosing this window does the same." if detached else
               "Open in a separate window, to read a long history beside the list.")
        self.detach_button.setToolTip(tip)
        self.detach_button.setAccessibleName(tip.split("\n")[0])

    def loading(self, on=True):
        self.skeleton.start() if on else self.skeleton.stop()

    def set_answer(self, answer):
        """The subject's history as the core told it: drawn on the tab showing."""
        self.loading(False)
        self.answer = answer
        self._fill_stage_runs()
        self.render()

    def set_problem(self, text):
        """Say the history could not be read, where it would have been."""
        self.loading(False)
        self.answer = None
        self.actions = []
        self.text.setHtml("<span style='color:%s'>%s</span>" % (theme.BAD, html.escape(text)))
        self.resized.emit()

    def render(self):
        self._sync_tab()
        if self.answer is None or self.tab == story.STAGES:
            return
        bar = self.text.verticalScrollBar()
        kept = bar.value()
        if self.tab == story.FILES:
            page, self.actions = story.files_html(self.answer)
        elif self.tab == story.RUNS:
            page, self.actions = story.runs_html(self.answer)
        else:
            page, self.actions = story.history_html(self.answer, self.opened,
                                                    transcripts=self.transcripts,
                                                    told=self.told)
        # Read now, not kept: the links' ink has to follow the palette, and a
        # browser's own default is a blue that disappears on the dark theme.
        self.text.document().setDefaultStyleSheet(story.LINK_CSS % theme.ACCENT)
        held = self._at_end
        self.text.setHtml(page)
        self.resized.emit()
        if self.tab == story.HISTORY and held:
            # A conversation is read from where it got to: the newest entry is
            # at the bottom, and that is where it opens and stays.
            self._at_end = True
            self._hold_end()
        else:
            self._at_end = False
            QTimer.singleShot(0, lambda: bar.setValue(min(kept, bar.maximum())))

    def _scrolled_to(self, value):
        bar = self.text.verticalScrollBar()
        if not getattr(self, "_holding", False):
            self._at_end = value >= bar.maximum() - 2

    def _hold_end(self):
        if self.tab != story.HISTORY or not self._at_end:
            return
        bar = self.text.verticalScrollBar()
        self._holding = True
        try:
            bar.setValue(bar.maximum())
        finally:
            self._holding = False

    def sizeHint(self):                      # noqa: N802 - Qt's own spelling
        """As tall as everything it holds; the table caps it and it scrolls inside.

        Stages always asks for the most it may have: a feed is read from the
        bottom and grows while it is read, so it fills its share of the table.
        """
        if self.detached:
            # Its window decides its size; this only says what it would like.
            return QSize(*SubjectWindow.FIRST_SIZE)
        if self.tab == story.STAGES:
            return QSize(max(self.width(), 300), 100000)
        margins = self.layout().contentsMargins()
        spacing = self.layout().spacing()
        top = [one for one in (self._top, self._note_row, self.problem, self._tab_row)
               if not one.isHidden()]
        height = (margins.top() + margins.bottom() + spacing * len(top)
                  + sum(one.sizeHint().height() for one in top))
        document = self.text.document()
        width = self.text.viewport().width() or max(200, self.width() - 40)
        document.setTextWidth(width)
        height += int(document.size().height()) + 8
        return QSize(max(self.width(), 300), height)

    def changeEvent(self, event):             # noqa: N802 - Qt's own spelling
        super().changeEvent(event)
        if event.type() in (QEvent.StyleChange, QEvent.PaletteChange) and self.answer is not None:
            # The history's inks were written into it when it was drawn: a new
            # palette has to be drawn again to reach them.
            QTimer.singleShot(0, self.render)

    # -- what it is asked -----------------------------------------------------------
    def _next(self):
        if self._action is not None:
            self.acted.emit(self._action[0], None)

    def _tab(self, tab):
        was, self.tab = self.tab, tab
        self.render()
        self.resized.emit()
        if tab == story.STAGES:
            self.acted.emit("stages", self.stage_run())
        elif was == story.STAGES:
            self.acted.emit("stages_off", None)

    def set_tab(self, tab, run_id=""):
        """Show ``tab`` - for a row opened again with what it last showed."""
        if run_id:
            self._stage_choice = run_id
            self._fill_stage_runs()
        if tab != self.tab:
            self.tabs.set_current(tab)

    def _sync_tab(self):
        stages = self.tab == story.STAGES
        self.body.setCurrentIndex(1 if stages else 0)
        self._scope_label.setVisible(self.tab == story.HISTORY)
        self.scopes.setVisible(self.tab == story.HISTORY)
        self.stage_runs.setVisible(stages)

    # -- Stages ------------------------------------------------------------------
    def stage_run(self):
        """The run whose stages are asked for: the picked one, or the newest."""
        data = self.stage_runs.currentData()
        return data or self._stage_choice or ""

    def _fill_stage_runs(self):
        choices = story.run_choices(self.answer or {}, self._live_run)
        wanted = self.stage_runs.currentData() or self._stage_choice
        self.stage_runs.blockSignals(True)
        self.stage_runs.clear()
        for run_id, label in choices:
            self.stage_runs.addItem(label, run_id)
        index = self.stage_runs.findData(wanted) if wanted else -1
        if index < 0 and choices:
            # The live run first when there is one, else the newest.
            index = next((n for n, (run_id, _l) in enumerate(choices)
                          if run_id == self._live_run), 0)
        self.stage_runs.setCurrentIndex(max(index, 0) if choices else -1)
        self.stage_runs.blockSignals(False)
        chosen = self.stage_runs.currentData() or ""
        if self.tab == story.STAGES and chosen and chosen != self._stage_choice:
            self._stage_choice = chosen
            self.acted.emit("stages", chosen)
        elif chosen:
            self._stage_choice = chosen

    def _stage_run_changed(self):
        self._stage_choice = self.stage_runs.currentData() or ""
        if self.tab == story.STAGES:
            self.acted.emit("stages", self._stage_choice)

    def hold_panel(self, panel):
        """Show the live Stages panel here, until :meth:`release_panel`."""
        if self._panel is panel:
            return
        self.recorded.hide()
        self.stage_note.stop()
        self.stage_note.hide()
        self._stage_layout.insertWidget(0, panel, 1)
        panel.show()
        self._panel = panel

    def release_panel(self):
        """Give the live panel up - before this row goes, always. Returns it."""
        panel, self._panel = self._panel, None
        if panel is not None:
            self._stage_layout.removeWidget(panel)
            panel.setParent(None)
            self.recorded.show()
        return panel

    def holds_panel(self):
        return self._panel is not None

    def show_recorded(self, stages, labels=None):
        """A run's stages as it recorded them."""
        self.stage_note.stop()
        self.stage_note.hide()
        self.recorded.show()
        self.recorded.set_stages(stages, named=True, labels=labels)

    def show_stage_note(self, text, window=False):
        """Say where the stages are, or that they are on their way."""
        self.recorded.set_stages([])
        self.stage_note.set_text(text)
        self.stage_note.show_button.setVisible(window)
        self.stage_note.show()
        if window:
            self.stage_note.start()
        else:
            self.stage_note.stop()

    def _scope(self, label):
        self.scope = (story.ALL if label == story.SCOPE_LABELS[story.ALL]
                      else story.CONVERSATION)
        self.opened = set()
        self._at_end = True
        self.acted.emit("scope", self.scope)

    def _link(self, url):
        text = url.toString()
        if not text.startswith("a:"):
            return
        try:
            action = self.actions[int(text[2:])]
        except (ValueError, IndexError):
            return
        if action.get("do") == "toggle":
            index = action.get("index")
            self.opened.symmetric_difference_update({index})
            self.render()
            return
        if action.get("do") == "transcript":
            index, key = action.get("index"), (action.get("run"), action.get("step"))
            self.told.symmetric_difference_update({index})
            if index in self.told and key not in self.transcripts:
                self.transcripts[key] = None
                self.acted.emit("transcript", action)
            self.render()
            return
        self.acted.emit(action.get("do", ""), action)

    def set_transcript(self, run_id, step_id, told):
        """An agent's own account arrived: shown where it was asked for."""
        self.transcripts[(run_id, step_id)] = told
        self.render()


class SubjectWindow(QWidget):
    """One subject's opened section, in a window of its own.

    A long run says a great deal, and a row of the list has a share of the
    table to say it in; a window can have a whole screen. It holds a section of
    its own - the same subject, read from the same answers - while the row it
    came from says where it went. Like the Stages window it has no parent, so it
    can sit on another screen and be minimized by itself, it does not keep the
    application running, and closing it - only a person doing so - puts the
    section back in its row.
    """

    closed_by_person = Signal()

    FIRST_SIZE = (900, 820)

    def __init__(self, detail, title=""):
        super().__init__(None)
        self.detail = detail
        self._shutting = False
        self._closed = False
        self.setAttribute(Qt.WA_QuitOnClose, False)
        self.setWindowTitle(title or "Subject")
        self.frame = widgets.dress(self, title or "Subject", minimizable=True)
        self.setWindowFlag(Qt.Window, True)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(detail, 1)
        self.resize(*self.FIRST_SIZE)

    def set_title(self, title):
        title = title or "Subject"
        if title != self.windowTitle():
            self.setWindowTitle(title)
            if self.frame is not None:
                self.frame.bar.name.setText(title)

    def bring_forward(self):
        state = self.windowState()
        if state & Qt.WindowMinimized:
            self.setWindowState(state & ~Qt.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()

    def shut(self):
        """Close it because the application or the row is going, not a person."""
        self._shutting = True
        if not self._closed:
            self.close()

    def closeEvent(self, event):             # noqa: N802 - Qt's own spelling
        super().closeEvent(event)
        self._closed = True
        if not self._shutting:
            self.closed_by_person.emit()


class _Elsewhere(widgets.Elsewhere):
    """What a row holds while its section is in a window: where, and the way back."""

    dock_requested = Signal()

    def __init__(self):
        super().__init__("Open in a separate window")
        self.setProperty("role", "subjectdetail")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setContentsMargins(14, 2, 12, 2)
        dock = icons.button(QToolButton(), "dock")
        dock.setToolButtonStyle(Qt.ToolButtonIconOnly)
        dock.setProperty("variant", "icon")
        dock.setCursor(Qt.PointingHandCursor)
        dock.setToolTip("Back into the list.")
        dock.clicked.connect(self.dock_requested)
        self.add_widget(dock)
        self.dock_button = dock


class SubjectsPage(QWidget):
    """Every subject, what waits for its person first; any row opens in place."""

    #: cycle id, run directory: put this run on the Cycles page.
    open_run_requested = Signal(str, str)
    #: cycle id, run directory, run id: continue this run where it stopped.
    resume_requested = Signal(str, str, str)
    #: run id: park the run this window is executing.
    park_requested = Signal(str)
    #: the live run's Cockpit, where its question is answered.
    cockpit_requested = Signal()
    #: run directory, path inside it ("" for the run itself): show on Artifacts.
    artifact_requested = Signal(str, str)
    #: the subject (its id, key, title and cycles): start a cycle on it.
    run_on_requested = Signal(dict)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.core = None
        self._subjects = []
        self._answered = None
        self._details = {}              # subject id -> SubjectDetail, open ones only
        self._histories = {}            # subject id -> (scope, limit, answer)
        self._limits = {}               # subject id -> history entries asked for
        self._open = {key for key, value in (settings.folds(FOLDS) or {}).items() if value}
        self._live_run, self._busy = "", False
        self._asked = {}
        self._loaded = False
        self._view = ALL
        self._view_chosen = False
        self._stale = True
        self._reveal_after = ""     # a subject to open once the list has been read
        #: Who lends the live Stages panel (the Cycles page, through the window),
        #: which open row holds it now, which rows show Stages and of which run,
        #: and the stages already read back from runs that have ended.
        self._lender = None
        self._borrower = ""
        self._staged = {}
        self._recordings = {}
        #: Subjects whose section is in a window of its own, and what each row
        #: was last showing (tab, run) when its section moved.
        self._windows = {}

        column = QVBoxLayout(self)
        column.setContentsMargins(24, 20, 24, 20)
        column.setSpacing(6)
        column.addWidget(widgets.heading("Subjects"))
        column.addWidget(widgets.lede(
            "Everything the cycles have worked on, one row each, with what waits for "
            "you first. Open a row to see where it stopped, what happened on the way "
            "and what to do next."))
        column.addSpacing(12)

        self.views = widgets.Segmented([VIEW_LABELS[one] for one in VIEWS], VIEW_LABELS[ALL])
        self.views.changed.connect(self._view_changed)
        self.count = widgets.mono("")
        self.refresh_button = icons.button(QPushButton(""), "refresh")
        self.refresh_button.setProperty("variant", "ghost")
        self.refresh_button.setToolTip("Read the list again")
        self.refresh_button.clicked.connect(self.refresh)
        column.addWidget(widgets.row(self.views, None, self.count, self.refresh_button))
        column.addSpacing(8)

        panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.table = widgets.Table(0, len(COLUMNS), expandable=True, reclick_clears=False)
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setShowGrid(False)
        self.table.setProperty("rows", "quiet")
        header = self.table.horizontalHeader()
        # Titles over the left edge of their column, where the search box under
        # them and the text of every cell start.
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        # Only the left padding, the choice table's rule: the band, the font and
        # the rule under it still come from theme.py.
        header.setStyleSheet("QHeaderView::section { padding-left: %dpx; }"
                             % text_inset(self.table))
        header.setSectionResizeMode(C_MARK, QHeaderView.Fixed)
        self.table.setColumnWidth(C_MARK, MARK_WIDTH)
        header.setSectionResizeMode(C_SUBJECT, QHeaderView.Stretch)
        for index, width in ((C_STATE, 230), (C_CYCLE, 170), (C_NOTE, 220),
                             (C_UPDATED, 116)):
            header.setSectionResizeMode(index, QHeaderView.Interactive)
            self.table.setColumnWidth(index, width)
        self.table.cellClicked.connect(self._clicked)
        self.table.cellDoubleClicked.connect(lambda row, _column: self._toggle(row))
        self.table.toggle_requested.connect(self._set_open)
        self.search = widgets.SearchRow(self.table, skip=(C_MARK,))
        self.search.changed.connect(self._searched)
        self.empty = widgets.empty_note(self.table, "")
        panel.layout().addWidget(self.table)
        column.addWidget(panel, 1)
        self.skeleton = widgets.Skeleton(self.table, "rows")

        self.more_button = QPushButton("")
        self.more_button.setProperty("variant", "ghost")
        self.more_button.clicked.connect(self._more)
        self.more_button.hide()
        column.addWidget(self.more_button, 0, Qt.AlignLeft)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self._say_empty()

    # -- the core and the window ------------------------------------------------------
    def set_core(self, core):
        self.core = core
        self._stale = True
        if self.isVisible():
            self.refresh()

    def set_stages_lender(self, lender):
        """Who lends the live Stages panel: ``lend(reclaim)``, ``take_back(panel)``,
        ``detached()`` and ``show_window()`` - the Cycles page, as the window
        puts it."""
        self._lender = lender

    def set_live(self, run_id="", busy=False):
        """Which run this window is executing, and whether it is executing any.

        Open rows say what to do next from it - Answer in the Cockpit of the live
        run, Park it, or Resume another only when nothing else is going.
        """
        changed = (run_id or "", bool(busy)) != (self._live_run, self._busy)
        self._live_run, self._busy = run_id or "", bool(busy)
        if changed:
            for subject_id, detail in self._details.items():
                detail.update_subject(self._subject(subject_id) or detail.subject,
                                      self._live_run, self._busy)
            self._restage()

    def refresh_soon(self):
        """Something happened in a run: read the list again, once it settles."""
        self._stale = True
        if self.isVisible():
            self._timer.start()

    def showEvent(self, event):               # noqa: N802 - Qt's own spelling
        super().showEvent(event)
        if self._stale or not self._loaded:
            self.refresh()
        self._restage()

    def hideEvent(self, event):               # noqa: N802 - Qt's own spelling
        # The live panel goes home whenever this page is not on screen, so the
        # Cycles page never finds its Stages tab empty - unless a section in a
        # window of its own has it, which is still on screen.
        if self._borrower not in self._windows:
            self.release_stages()
        super().hideEvent(event)

    def changeEvent(self, event):             # noqa: N802 - Qt's own spelling
        super().changeEvent(event)
        if event.type() in (QEvent.StyleChange, QEvent.PaletteChange) and self._subjects:
            # The State column's ink was set when the rows were made.
            QTimer.singleShot(0, self._recolour)

    # -- reading the list -------------------------------------------------------------
    def refresh(self, everything=False):
        """Ask the core for the list as it is now, keeping what is open.

        ``everything`` reads the whole list rather than the pages already shown:
        for finding one subject that has not been paged in yet.
        """
        self._timer.stop()
        if self.core is None:
            return
        self._stale = False
        if not self._loaded:
            # Only the first time: a refresh keeps the rows it has until the new
            # ones arrive, rather than going grey every time a run takes a step.
            self.skeleton.start()
        core, view = self.core, self._view
        limit = 0 if everything else max(PAGE, len(self._subjects))
        self._call("list", lambda: core.cycle_subject_list(view=view, limit=limit),
                   self._listed)

    def _listed(self, answer, error):
        self.skeleton.stop()
        if error is not None:
            self._loaded = True
            self._say_empty(error="Subjects could not be read: %s" % error)
            return
        if not answer.get("ok", True):
            self._say_empty(error="Subjects could not be read: %s" % "; ".join(
                answer.get("problems") or ["the core said no"]))
            return
        first = not self._loaded
        self._loaded = True
        self._answered = answer
        counts = answer.get("counts") or {}
        for view in VIEWS:
            amount = counts.get(view)
            label = VIEW_LABELS[view]
            self.views.set_text(label, "%s %d" % (label, amount) if amount else label)
        if first and not self._view_chosen and counts.get(NEEDS_YOU):
            # Opened while something waits for its person: that is what the page
            # is for, so it is what it opens on.
            self._view = NEEDS_YOU
            self.views.set_current(VIEW_LABELS[NEEDS_YOU], notify=False)
            self.refresh()
            return
        self._fill(answer.get("subjects") or [], answer)

    def _fill(self, subjects, answer):
        """Rows for ``subjects``, the open ones opened again."""
        bar = self.table.verticalScrollBar()
        kept = bar.value()
        selected = self._selected_id()
        same = [one.get("id") for one in subjects] == [one.get("id") for one in self._subjects]
        self._subjects = list(subjects)
        if same and self.table.data_rows():
            # The same subjects in the same order - what a run moving along
            # gives, every few seconds: the rows are told what changed, and the
            # open ones keep their place, their scroll and their Stages.
            for row, subject in zip(self.table.data_rows(), self._subjects):
                detail = self._details.get(subject.get("id"))
                self._fill_row(row, subject, opened=detail is not None)
                if detail is not None:
                    detail.update_subject(subject, self._live_run, self._busy)
                    self._history(subject["id"])
                if subject.get("id") in self._windows:
                    self._windows[subject["id"]].set_title(story.title(subject))
        else:
            # Rows are about to be made again: the live panel must not be in
            # one of them when it goes.
            if self._borrower not in self._windows:
                self.release_stages()
            # Sections in windows stay where they are; only the rows go.
            self._details = {sid: detail for sid, detail in self._details.items()
                             if sid in self._windows}
            self.table.setRowCount(0)
            for subject in self._subjects:
                row = self.table.rowCount()
                self.table.insertRow(row)
                self._fill_row(row, subject)
            for subject in self._subjects:
                if subject.get("id") in self._windows:
                    self._history(subject["id"])
                if subject.get("id") in self._open or subject.get("id") in self._windows:
                    self._open_row(self._row_of(subject["id"]), subject, fetch=True)
        self.search.apply()
        total, shown = answer.get("total") or 0, len(self._subjects)
        self.count.setText("%d of %d" % (shown, total) if total > shown else
                           ("%d subject%s" % (total, "" if total == 1 else "s")))
        left = max(0, total - shown)
        self.more_button.setText("Show %d more" % min(PAGE, left))
        self.more_button.setVisible(bool(left))
        self._say_empty()
        if selected:
            row = self._row_of(selected)
            if row is not None:
                self.table.selectRow(row)
        QTimer.singleShot(0, lambda: bar.setValue(min(kept, bar.maximum())))

    def _fill_row(self, row, subject, opened=False):
        mark = self._item("")
        mark.setIcon(icons.icon("disclosure_open" if opened else "disclosure_closed",
                                theme.NEUTRAL[600]))
        mark.setToolTip("Close" if opened else "Open")
        self.table.setItem(row, C_MARK, mark)
        title = self._item(story.title(subject))
        title.setData(ROLE_ID, subject.get("id"))
        title.setToolTip(story.title(subject))
        self.table.setItem(row, C_SUBJECT, title)
        state = self._item(story.state_text(subject) or "-")
        state.setForeground(theme.color(story.state_colour(subject)))
        self.table.setItem(row, C_STATE, state)
        self.table.setItem(row, C_CYCLE, self._item(story.cycles_text(subject) or "-"))
        note = self._item(story.note_text(subject) or "-")
        note.setToolTip((subject.get("note") or {}).get("text") or "")
        self.table.setItem(row, C_NOTE, note)
        when = self._item(cyclesubjects.when(subject.get("updated_at")) or "-")
        when.setFont(theme.mono_font(9))
        self.table.setItem(row, C_UPDATED, when)

    def _item(self, text):
        item = QTableWidgetItem(str(text or ""))
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        return item

    def _recolour(self):
        for row in self.table.data_rows():
            subject = self._subject(self._id_of(row))
            item = self.table.item(row, C_STATE)
            if subject is not None and item is not None:
                item.setForeground(theme.color(story.state_colour(subject)))
            mark = self.table.item(row, C_MARK)
            if mark is not None:
                opened = self.table.detail_of(row) is not None
                mark.setIcon(icons.icon("disclosure_open" if opened else "disclosure_closed",
                                        theme.NEUTRAL[600]))

    def _more(self):
        if self.core is None:
            return
        core, view, offset = self.core, self._view, len(self._subjects)

        def done(answer, error):
            if error is not None or not (answer or {}).get("ok", True):
                return
            self._fill(self._subjects + list(answer.get("subjects") or []), answer)

        self._call("more", lambda: core.cycle_subject_list(view=view, limit=PAGE,
                                                           offset=offset), done)

    def _view_changed(self, label):
        self._view = next(view for view in VIEWS if VIEW_LABELS[view] == label)
        self._view_chosen = True
        self._subjects = []
        self.refresh()

    def _say_empty(self, error=""):
        """What the table says when it shows no rows - each reason in its own words."""
        if error:
            text = error
        elif self.core is None or not self._loaded:
            text = ""
        elif self._view == NEEDS_YOU:
            text = ("Nothing is waiting for you. A question a run asks, a pause, or a "
                    "run you parked appears here.")
        elif self._view == ACTIVE:
            text = "Nothing is going or waiting. Runs in progress appear here."
        else:
            text = ("No subjects yet. Each run of a cycle that names what it works on - "
                    "a task, a release - appears here, with everything that happened "
                    "in it. A run that names nothing is a row of its own.")
        self.empty.say(text)
        self.empty.say_filtered("No subject matches the search." if self.search.searching()
                                else "")

    def _searched(self):
        self._say_empty()

    # -- opening a row ------------------------------------------------------------------
    def _clicked(self, row, column):
        if column == C_MARK:
            self._toggle(row)

    def _toggle(self, row):
        row = self.table.owner_of(row)
        self._set_open(row, self.table.detail_of(row) is None)

    def _set_open(self, row, opened):
        subject_id = self._id_of(row)
        if not subject_id:
            return
        if opened:
            self._open.add(subject_id)
            self._open_row(row, self._subject(subject_id) or {}, fetch=True)
        else:
            self._open.discard(subject_id)
            self._staged.pop(subject_id, None)
            if self._borrower == subject_id:
                self.release_stages()
            window = self._windows.pop(subject_id, None)
            if window is not None:
                window.shut()
                window.deleteLater()
            self._details.pop(subject_id, None)
            self.table.close_row(row)
            mark = self.table.item(row, C_MARK)
            if mark is not None:
                mark.setIcon(icons.icon("disclosure_closed", theme.NEUTRAL[600]))
                mark.setToolTip("Open")
        self.settings.save_folds(FOLDS, {key: True for key in sorted(self._open)})

    def _open_row(self, row, subject, fetch=True):
        if row is None:
            return
        subject_id = subject.get("id", "")
        mark = self.table.item(row, C_MARK)
        if mark is not None:
            mark.setIcon(icons.icon("disclosure_open", theme.NEUTRAL[600]))
            mark.setToolTip("Close")
        if subject_id in self._windows:
            # Its section is in a window: the row says so, and holds nothing else.
            self.table.open_row(row, self._elsewhere(subject_id))
            return
        detail = self._new_detail(subject)
        self.table.open_row(row, detail)
        self._details[subject_id] = detail
        if fetch:
            self._history(subject_id)
        if subject_id in self._staged:
            # Opened again (the list was made anew): on Stages, as it was.
            detail.set_tab(story.STAGES, self._staged[subject_id])

    def _new_detail(self, subject):
        """A section for one subject, for its row or for a window."""
        detail = SubjectDetail(subject)
        detail.update_subject(subject, self._live_run, self._busy)
        subject_id = subject.get("id", "")
        detail.acted.connect(lambda verb, payload, sid=subject_id: self._acted(sid, verb, payload))
        detail.resized.connect(lambda sid=subject_id: self._fit(sid))
        cached = self._histories.get(subject_id)
        if cached is not None and cached[0] == detail.scope:
            # What was read last time, at once, while the fresh copy is read.
            detail.set_answer(cached[2])
        else:
            detail.loading(True)
        return detail

    def _elsewhere(self, subject_id):
        note = _Elsewhere()
        note.show_requested.connect(lambda sid=subject_id: self._bring_forward(sid))
        note.dock_requested.connect(lambda sid=subject_id: self._dock(sid))
        return note

    # -- a section in a window of its own ----------------------------------------------
    def _detach(self, subject_id):
        """Move one subject's section into a window, the row saying where it went."""
        if subject_id in self._windows:
            self._bring_forward(subject_id)
            return
        subject = self._subject(subject_id)
        old = self._details.get(subject_id)
        if subject is None or old is None:
            return
        tab, run = old.tab, old.stage_run()
        if self._borrower == subject_id:
            self.release_stages()
        detail = self._new_detail(subject)
        detail.set_detached(True)
        window = SubjectWindow(detail, story.title(subject))
        window.closed_by_person.connect(lambda sid=subject_id: self._dock(sid))
        # Gone with the page, whatever else happens: it has no parent to take it.
        self.destroyed.connect(window.deleteLater)
        self._windows[subject_id] = window
        self._details[subject_id] = detail
        row = self._row_of(subject_id)
        if row is not None:
            self.table.open_row(row, self._elsewhere(subject_id))
        if tab != story.HISTORY:
            detail.set_tab(tab, run if tab == story.STAGES else "")
        window.bring_forward()

    def _dock(self, subject_id):
        """The section back in its row: a person closed its window, or asked."""
        window = self._windows.pop(subject_id, None)
        if window is None:
            return
        detail = self._details.pop(subject_id, None)
        tab = detail.tab if detail is not None else story.HISTORY
        run = detail.stage_run() if detail is not None else ""
        if self._borrower == subject_id:
            self.release_stages()
        window.shut()
        window.deleteLater()
        row = self._row_of(subject_id)
        subject = self._subject(subject_id)
        if row is None or subject is None or subject_id not in self._open:
            return
        self._open_row(row, subject, fetch=False)
        restored = self._details.get(subject_id)
        if restored is not None and tab != story.HISTORY:
            restored.set_tab(tab, run if tab == story.STAGES else "")

    def _bring_forward(self, subject_id):
        window = self._windows.get(subject_id)
        if window is not None:
            window.bring_forward()

    def shut_windows(self):
        """Close every subject's window because the application is going."""
        self.release_stages()
        for window in list(self._windows.values()):
            window.shut()

    def window_of(self, subject_id):
        return self._windows.get(subject_id)

    def _fit(self, subject_id):
        row = self._row_of(subject_id)
        if row is not None:
            self.table.fit_detail(row)

    def _history(self, subject_id):
        detail = self._details.get(subject_id)
        if detail is None or self.core is None:
            return
        core, scope = self.core, detail.scope
        limit = self._limits.get(subject_id, HISTORY_PAGE)

        def done(answer, error, sid=subject_id, scope=scope, limit=limit):
            shown = self._details.get(sid)
            if error is not None:
                if shown is not None:
                    shown.set_problem("The history could not be read: %s" % error)
                return
            if not answer.get("ok", True):
                if shown is not None:
                    shown.set_problem("The history could not be read: %s" % "; ".join(
                        answer.get("problems") or ["the core said no"]))
                return
            self._histories[sid] = (scope, limit, answer)
            if shown is not None and shown.scope == scope:
                shown.set_answer(answer)

        self._call("history:" + subject_id,
                   lambda: core.cycle_subject_history(subject_id, scope=scope, limit=limit),
                   done)

    # -- Stages ---------------------------------------------------------------------------
    def _show_stages(self, subject_id, run_id):
        """One open row's Stages: the live panel for the run going in this window,
        a recorded run's stages for any other."""
        detail = self._details.get(subject_id)
        if detail is None:
            return
        self._staged[subject_id] = run_id
        if run_id and run_id == self._live_run and self._lender is not None:
            if self._borrower == subject_id:
                return
            if self._lender.detached():
                detail.show_stage_note("Stages are in a separate window", window=True)
                return
            self.release_stages()
            panel = self._lender.lend(self.release_stages)
            if panel is None:
                detail.show_stage_note("Stages are in a separate window", window=True)
                return
            detail.hold_panel(panel)
            self._borrower = subject_id
            self._fit(subject_id)
            return
        if self._borrower == subject_id:
            self.release_stages()
        self._recorded(subject_id, run_id)

    def release_stages(self):
        """Give the live panel back to the Cycles page, if a row has it."""
        sid, self._borrower = self._borrower, ""
        detail = self._details.get(sid) if sid else None
        panel = detail.release_panel() if detail is not None else None
        if panel is not None and self._lender is not None:
            self._lender.take_back(panel)
        if detail is None or sid not in self._staged:
            return
        if self._staged[sid] == self._live_run:
            # Taken back while still wanted here - Stages going to a window of
            # its own, say: say so, and let the row work out what it shows now.
            detail.show_stage_note("Stages are back on the Cycles page.")
            QTimer.singleShot(0, self._restage)
        else:
            self._recorded(sid, self._staged[sid])

    def _restage(self):
        """After the live run changed, or the page came back: each row on Stages
        shows the right thing again."""
        visible = self.isVisible()
        if self._borrower and self._staged.get(self._borrower) != self._live_run:
            self.release_stages()
        for subject_id, run_id in list(self._staged.items()):
            if subject_id in self._details and (visible or subject_id in self._windows):
                self._show_stages(subject_id, run_id)

    def _recorded(self, subject_id, run_id):
        detail = self._details.get(subject_id)
        if detail is None:
            return
        if not run_id:
            detail.show_stage_note("No run to show the stages of.")
            return
        if run_id in self._recordings:
            detail.show_recorded(*self._recordings[run_id])
            return
        answer = (self._histories.get(subject_id) or (None, None, {}))[2] or {}
        run_dir = next((one.get("run_dir") for one in answer.get("runs") or []
                        if one.get("run") == run_id), "")
        if not run_dir:
            detail.show_stage_note("This run's folder is not known.")
            return
        detail.show_stage_note("Reading the run's stages...")

        def done(events, error, sid=subject_id, run_id=run_id, run_dir=run_dir):
            shown = self._details.get(sid)
            if error is not None or not events:
                if shown is not None and shown.stage_run() == run_id:
                    shown.show_stage_note("This run kept no stages.")
                return
            from ..runner import RunState
            state = RunState()
            cyclereplay.restore(run_dir, state, events=events)
            cycle = state.cycle or {}
            labels = {node.get("id"): node.get("label") or node.get("id")
                      for node in (cycle.get("graph") or {}).get("nodes") or []}
            stages = list(cycle.get("stages") or [])
            state.deleteLater()
            if run_id != self._live_run:
                self._recordings[run_id] = (stages, labels)
            if shown is not None and shown.stage_run() == run_id and not shown.holds_panel():
                shown.show_recorded(stages, labels)

        self._call("stages:" + subject_id, lambda: cyclereplay.events_of(run_dir), done)

    # -- what an open row asks for ------------------------------------------------------
    def _acted(self, subject_id, verb, payload):
        subject = self._subject(subject_id) or {}
        latest = subject.get("latest") or {}
        if verb == "stages":
            self._show_stages(subject_id, payload or "")
            return
        if verb == "stages_off":
            self._staged.pop(subject_id, None)
            if self._borrower == subject_id:
                self.release_stages()
            return
        if verb == "detach":
            self._detach(subject_id)
            return
        if verb == "dock":
            self._dock(subject_id)
            return
        if verb == "stages_window":
            if self._lender is not None:
                self._lender.show_window()
            return
        if verb == story.OPEN:
            self.open_run_requested.emit(latest.get("cycle", ""), latest.get("run_dir", ""))
        elif verb == story.COCKPIT:
            self.cockpit_requested.emit()
        elif verb == story.RESUME:
            offer = subject.get("resume") or {}
            self.resume_requested.emit(offer.get("cycle", ""), offer.get("run_dir", ""),
                                       offer.get("run_id", ""))
        elif verb == "park":
            self.park_requested.emit(latest.get("id", ""))
        elif verb == "run_on":
            self.run_on_requested.emit({key: subject.get(key) for key in
                                        ("id", "key", "title", "kind", "cycles")})
        elif verb == "note":
            self._write_note(subject_id, subject)
        elif verb == "scope":
            self._limits[subject_id] = HISTORY_PAGE
            detail = self._details.get(subject_id)
            if detail is not None:
                detail.loading(True)
            self._history(subject_id)
        elif verb == "earlier":
            self._limits[subject_id] = self._limits.get(subject_id, HISTORY_PAGE) + HISTORY_MORE
            self._history(subject_id)
        elif verb == "transcript":
            self._transcript(subject_id, payload.get("run", ""), payload.get("step", ""))
        elif verb == "file":
            self.artifact_requested.emit(payload.get("run_dir", ""), payload.get("path", ""))
        elif verb == "files":
            self.artifact_requested.emit(payload.get("run_dir", ""), "")
        elif verb == "run":
            self.open_run_requested.emit(payload.get("cycle", ""), payload.get("run_dir", ""))

    def _transcript(self, subject_id, run_id, step_id):
        if self.core is None or not hasattr(self.core, "cycle_subject_transcript"):
            return
        core = self.core

        def done(answer, error, sid=subject_id):
            detail = self._details.get(sid)
            if detail is None:
                return
            if error is not None or not (answer or {}).get("ok", True):
                problems = (answer or {}).get("problems") or [str(error or "")]
                told = {"calls": [{"call": "", "prompt": "", "stages": [
                    {"kind": "result", "title": "Its account could not be read",
                     "detail": "; ".join(problems)}]}]}
            else:
                told = answer
            detail.set_transcript(run_id, step_id, told)

        self._call("transcript:%s:%s:%s" % (subject_id, run_id, step_id),
                   lambda: core.cycle_subject_transcript(run_id, step_id), done)

    def _write_note(self, subject_id, subject):
        text = widgets.ask_text(
            self, "Note", "A note about %s" % (story.title(subject) or subject_id),
            detail="Where you stopped, what comes next, what you are waiting for. "
                   "Notes are kept with the subject and shown in its history; "
                   "Ctrl+Enter keeps it.",
            agree="Keep note", lines=4)
        if not text or self.core is None:
            return
        core = self.core
        answer, error = background.wait(lambda: core.cycle_subject_note(subject_id, text),
                                        core)
        if error is not None or not (answer or {}).get("ok", True):
            problems = ((answer or {}).get("problems") or [str(error or "")])
            widgets.warn(self, "Note", "The note was not kept.", "; ".join(problems))
            return
        self.refresh()

    # -- finding one -----------------------------------------------------------------------
    def reveal(self, subject_id, again=True):
        """Show one subject opened, setting aside a view or search that hides it.

        One the list has not paged in yet - an older subject, asked for from the
        Cycles page - is looked for once in the whole list, and opened there.
        """
        row = self._row_of(subject_id)
        if row is None and again and (self._view != ALL or self._has_more()):
            self._view, self._view_chosen = ALL, True
            self.views.set_current(VIEW_LABELS[ALL], notify=False)
            self._reveal_after = subject_id
            self.refresh(everything=True)
            return True
        if row is None:
            return False
        if self.search.searching():
            self.search.clear()
        if self.table.detail_of(row) is None:
            self._set_open(row, True)
        self.table.selectRow(row)
        # After the list has put its scroll back where it was (see _fill), not
        # before it: that is queued the same way, and would undo this.
        item = self.table.item(row, C_SUBJECT)
        QTimer.singleShot(0, lambda: self.table.scrollToItem(
            item, QAbstractItemView.PositionAtTop))
        return True

    def search_rows(self):
        """Every subject the list holds: its name, where it stands, who worked on it."""
        found = []
        for subject in self._subjects:
            state = story.state_text(subject)
            tone = ("warn" if subject.get("needs_you") else
                    "accent" if subject.get("active") else "neutral")
            found.append(search.hit(
                story.title(subject),
                " - ".join(part for part in (story.cycles_text(subject),
                                             cyclesubjects.when(subject.get("updated_at")))
                           if part),
                state, tone, keys=" ".join(part for part in (
                    subject.get("kind") or "", story.note_text(subject)) if part),
                target=("subject", subject.get("id", "")),
                live=bool(subject.get("active"))))
        return found

    def show_found(self, target):
        if not target or target[0] != "subject":
            return False
        return self.reveal(target[1])

    # -- bookkeeping -----------------------------------------------------------------------
    def _call(self, kind, work, done):
        """``work`` on a thread (or here, for a core that does not block);
        ``done(result, error)`` - for the latest call of each kind only."""
        self._asked[kind] = self._asked.get(kind, 0) + 1
        mine = self._asked[kind]

        def answer(result, error):
            if self._asked.get(kind) != mine:
                return
            done(result, error)
            if kind == "list" and error is None and getattr(self, "_reveal_after", ""):
                wanted, self._reveal_after = self._reveal_after, ""
                self.reveal(wanted, again=False)
            if error is not None and not isinstance(error, core_mod.CoreError):
                raise error

        if background.offloads(self.core):
            background.run(work, answer, owner=self)
        else:
            answer(*background.call(work))

    def _has_more(self):
        answer = self._answered or {}
        return (answer.get("total") or 0) > len(self._subjects)

    def _id_of(self, row):
        item = self.table.item(row, C_SUBJECT) if row is not None else None
        return item.data(ROLE_ID) if item is not None else ""

    def _row_of(self, subject_id):
        for row in self.table.data_rows():
            if self._id_of(row) == subject_id:
                return row
        return None

    def _subject(self, subject_id):
        for subject in self._subjects:
            if subject.get("id") == subject_id:
                return subject
        return None

    def _selected_id(self):
        rows = {index.row() for index in self.table.selectedIndexes()}
        return self._id_of(rows.pop()) if len(rows) == 1 else ""

    def subjects(self):
        return list(self._subjects)

    def opened(self):
        """The subjects open just now, in the order of the list."""
        return [self._id_of(row) for row in self.table.data_rows()
                if self.table.detail_of(row) is not None]

    def detail(self, subject_id):
        return self._details.get(subject_id)
