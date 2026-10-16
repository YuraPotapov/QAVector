"""Run page: the live view of a run, assembled purely from the event stream.

One panel per driven window, each showing the same step tree the in-page HUD
draws - it comes from the identical ``PlanNode.to_dict()`` payload, carried in
the ``flow.start`` event. Nothing here polls or guesses: every state change is
an event the launcher sent.
"""

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout, QLabel,
                               QPlainTextEdit, QProgressBar,
                               QPushButton, QScrollArea, QVBoxLayout, QWidget)

from .. import icons, search, theme, widgets
from .serverlogwindow import ServerLogWindow, level_html

#: Lines painted in one session's Server log box. The model keeps more (see
#: cms_gui.runner.SERVER_LOG_LINES); this is only what is worth re-rendering.
SERVER_VISIBLE_LINES = 400

ALL_LOGS = "All logs"

STATE_TAGS = {
    "launching": ("LAUNCHING", "neutral"),
    "launched": ("LAUNCHED", "neutral"),
    "attached": ("ATTACHED", "outline"),
    "running": ("RUNNING", "outline"),
    "passed": ("PASS", "accent"),
    "failed": ("FAIL", "bad"),
    "closed": ("CLOSED", "neutral"),
}

#: How wide one level of the step tree is indented. Was four spaces of a
#: monospaced string, and is now a margin, because the mark beside it is a
#: painted icon rather than a character in the same run of text.
INDENT_PX = 14

#: Status -> (icon name, colour token). The mark and the colour say the same
#: thing twice on purpose: colour alone is not something everyone can read.
STATUS_MARKS = {
    "pass": ("pass", theme.OK),
    "fail": ("fail", theme.BAD),
    "error": ("fail", theme.BAD),
    "running": ("running", theme.ACCENT),
}


def format_duration(seconds):
    """How long, in the unit that reads: 3.2 s, 42 s, 1 min 18 s, 1 h 02 min.

    Seconds alone stopped being readable the moment a run of backend tests went
    past a minute: 78.3 s has to be worked out, 1 min 18 s does not.
    """
    if seconds is None:
        return ""
    seconds = max(0.0, float(seconds))
    if seconds < 10:
        return "%.1f s" % seconds
    if seconds < 60:
        return "%d s" % seconds
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return "%d min %02d s" % (minutes, secs)
    hours, minutes = divmod(minutes, 60)
    return "%d h %02d min" % (hours, minutes)


def _took(run, now=None):
    """How long one scenario took, or has taken so far. "" before it starts."""
    started = run.get("started")
    if not started:
        return ""
    ended = run.get("ended") or (now if now is not None else time.time())
    return format_duration(ended - started)


def _mark(status):
    """(icon name, colour) for a step in ``status``."""
    return STATUS_MARKS.get(status, ("pending", theme.NEUTRAL[500]))


def _mark_label(name, colour, size=13):
    """The mark itself: a painted icon in a label, so no font has to have it."""
    label = QLabel()
    label.setPixmap(icons.pixmap(name, size, colour))
    label.setFixedWidth(size + 6)
    label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
    return label


class SessionPanel(widgets.BlueprintPanel):
    """One window's state and step tree."""

    def __init__(self, parent=None):
        super().__init__(parent, padding=(0, 0, 0, 0))
        column = self.layout()
        column.setSpacing(0)

        head = QWidget()
        widgets.scoped_style(head, "border-bottom: 1px solid %s;" % theme.DIVIDER)
        self.name = QLabel("")
        self.name.setStyleSheet("font-family: %s; font-size: 13px; font-weight: 500;"
                                % theme.MONO_CSS)
        self.tag = widgets.Tag("", "neutral")
        self.meta = widgets.mono("")
        self.progress = QProgressBar()
        self.progress.setFixedWidth(180)
        self.progress.setTextVisible(False)
        self.counter = widgets.mono("")
        self.counter.setFixedWidth(56)
        self.counter.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        head_row = widgets.row(self.name, self.tag, self.meta, None,
                               self.progress, self.counter)
        head_row.setParent(head)
        head_layout = QVBoxLayout(head)
        head_layout.setContentsMargins(14, 10, 14, 10)
        head_layout.addWidget(head_row)
        column.addWidget(head)

        self.steps = QWidget()
        self.steps_layout = QVBoxLayout(self.steps)
        self.steps_layout.setContentsMargins(14, 8, 14, 12)
        self.steps_layout.setSpacing(1)
        column.addWidget(self.steps)

        # The running scenario's time label and its record, so the page's clock
        # can move it on without rebuilding the tree twice a second.
        self._live = None
        # What the step tree was built for, and its rows, so an event repaints
        # the rows that changed instead of building every one again (see
        # _render_tree).
        self._shape = None
        self._headers = {}        # run key -> _ScenarioHeader
        self._rows = {}           # run key -> {step index: _StepCells}

        # --server-log only. Folded away and hidden entirely until the first line
        # arrives, so a run without it looks exactly as it did before.
        self._server_window = None
        self.server = self._build_server_section()
        column.addWidget(self.server)
        self.server.setVisible(False)

    # -- server log ---------------------------------------------------------
    def _build_server_section(self):
        section = QWidget()
        wrap = QVBoxLayout(section)
        wrap.setContentsMargins(14, 0, 14, 12)
        self._server_disclosure = widgets.Disclosure("Server log")
        # Beside the fold, not inside it: reading a log is what this is for, and
        # the reason to reach for it is usually that the folded strip is too small
        # to read in - which is a poor place to hide the way out of it.
        self._server_popout = QPushButton("Separate Window")
        self._server_popout.setProperty("variant", "ghost")
        self._server_popout.setToolTip(
            "This session's backend log, full size, with search and level filters. "
            "Stays open and keeps up as the run goes on.")
        self._server_popout.clicked.connect(self.open_server_window)

        self._server_filter = QComboBox()
        self._server_filter.addItem(ALL_LOGS)
        self._server_filter.currentIndexChanged.connect(self._repaint_server)
        save = QPushButton("Save…")
        save.setProperty("variant", "ghost")
        save.clicked.connect(self._save_server)
        # Without --run-tests there is no report directory, so this button is the
        # only way the lines leave the window.
        save.setToolTip("Write the lines shown here to a file")
        self._server_count = widgets.mono("")

        self._server_view = QPlainTextEdit()
        self._server_view.setReadOnly(True)
        self._server_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._server_view.setMaximumBlockCount(SERVER_VISIBLE_LINES)
        self._server_view.setFixedHeight(180)
        self._server_view.setStyleSheet("font-family: %s; font-size: 12px;"
                                        % theme.MONO_CSS)

        self._server_controls = widgets.row(self._server_filter,
                                            self._server_count, None, save)
        self._server_elsewhere = widgets.Elsewhere("Streaming to a separate window")
        self._server_elsewhere.show_requested.connect(self.open_server_window)
        self._server_elsewhere.setVisible(False)

        body = self._server_disclosure.body()
        body.addWidget(self._server_controls)
        body.addWidget(self._server_view)
        body.addWidget(self._server_elsewhere)
        # On the disclosure's own header line, not beside the whole section. The
        # section used to sit in a hbox next to this button, which put the button
        # where it belongs and took its width off the log box underneath as well.
        self._server_disclosure.add_to_header(self._server_popout)
        wrap.addWidget(self._server_disclosure)
        # What is currently on screen, so a repaint only happens when it changed:
        # this runs on every event, and re-rendering hundreds of lines each time
        # would fight the user's scrollbar as well as the CPU.
        self._server_painted = None
        self._server_lines = []
        self._server_log_names = []
        return section

    def _update_server(self, session):
        lines = list(session.get("server") or ())
        if not lines:
            return
        self._server_lines = lines
        self.server.setVisible(True)
        if self._server_window is not None:
            # The window has it. Nothing is repainted here at all - a second copy
            # of a few hundred lines, rendered where nobody is looking, is the
            # whole cost this avoids.
            self._server_window.update_from(session)
            self._server_log_names = list(session.get("server_logs") or ())
            return
        names = list(session.get("server_logs") or ())
        self._server_log_names = names
        current = self._server_filter.currentText()
        if [self._server_filter.itemText(i)
                for i in range(1, self._server_filter.count())] != names:
            self._server_filter.blockSignals(True)
            self._server_filter.clear()
            self._server_filter.addItems([ALL_LOGS] + names)
            index = self._server_filter.findText(current)
            self._server_filter.setCurrentIndex(index if index >= 0 else 0)
            self._server_filter.blockSignals(False)
        # One log is the common case, and then its name on every line is noise.
        self._server_filter.setVisible(len(names) > 1)
        self._repaint_server()

    def _visible_server_lines(self):
        chosen = self._server_filter.currentText()
        if chosen and chosen != ALL_LOGS:
            return [line for line in self._server_lines if line["log"] == chosen]
        return list(self._server_lines)

    def _repaint_server(self):
        lines = self._visible_server_lines()
        many = len({line["log"] for line in lines}) > 1
        key = (len(lines), self._server_filter.currentText(),
               lines[-1]["text"] if lines else "")
        if key == self._server_painted:
            return
        self._server_painted = key
        at_bottom = (self._server_view.verticalScrollBar().value()
                     >= self._server_view.verticalScrollBar().maximum() - 4)
        self._server_view.clear()
        for line in lines[-SERVER_VISIBLE_LINES:]:
            prefix = "[%s] " % line["log"] if many else ""
            self._server_view.appendHtml(
                '<pre style="margin:0">%s</pre>'
                % level_html(line["level"], prefix + line["text"]))
        self._server_count.setText("%d line%s" % (len(lines),
                                                  "" if len(lines) == 1 else "s"))
        if at_bottom:
            bar = self._server_view.verticalScrollBar()
            bar.setValue(bar.maximum())

    def open_server_window(self):
        """Raise this session's log window, opening one if there is none.

        Kept and re-raised rather than opened again: clicking twice should bring
        back what you were reading, not start a second copy of it beside the first.
        """
        if self._server_window is None:
            self._server_window = ServerLogWindow(self.name.text() or "session")
            self._server_window.closed.connect(self._server_window_closed)
        self._server_window.update_from({"server": self._server_lines,
                                         "server_logs": self._server_log_names})
        self._set_server_detached(True)
        self._server_window.show()
        self._server_window.raise_()
        self._server_window.activateWindow()
        return self._server_window

    def _server_window_closed(self):
        self._server_window = None
        self._set_server_detached(False)
        # It kept arriving while the window had it, so the strip is behind.
        self._server_painted = None
        self._repaint_server()

    def _set_server_detached(self, detached):
        """Show the lines here, or say where they went. Never both."""
        self._server_controls.setVisible(not detached)
        self._server_view.setVisible(not detached)
        self._server_popout.setVisible(not detached)
        self._server_elsewhere.setVisible(detached)
        if detached:
            self._server_elsewhere.start()
        else:
            self._server_elsewhere.stop()

    def _save_server(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save server log",
                                              "%s-server.log" % self.name.text(),
                                              "Log files (*.log);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join("[%s] %s" % (line["log"], line["text"])
                                   for line in self._visible_server_lines()))
        except OSError as exc:
            widgets.warn(self, "Save server log", str(exc))

    def update_from(self, session):
        self.name.setText(session["name"])
        label, variant = STATE_TAGS.get(session["state"], (session["state"].upper(),
                                                           "neutral"))
        self.tag.set(label, variant)
        bits = []
        if session.get("pid"):
            bits.append("pid %s" % session["pid"])
        if session.get("scenario"):
            bits.append(session["scenario"])
        self.meta.setText(" · ".join(bits))
        total = session.get("total") or 0
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(session.get("done", 0))
        self.counter.setText("%d/%d" % (session.get("done", 0), total) if total else "")
        self._render_tree(session)
        self._update_server(session)

    def _render_tree(self, session):
        """Every scenario this window has run, not only the one running now.

        The in-page overlay has always shown the whole planned list with each
        outcome; this page showed the current scenario alone, because the state
        it read was overwritten at every flow.start. Reading the per-scenario
        records instead puts the same picture here - and the ones still to come
        are listed too, so the shape of the run is visible from the start.

        The rows are built once for each shape of the run - a scenario starting,
        or the list still to come changing - and after that only repainted: a
        step ending changes its own row's mark and colour. Building every row
        again on every event cost a second and a half per event on a scenario of
        750 steps, two events a step, and the whole window stopped answering.
        """
        runs = session.get("runs") or {}
        pending = [one for one in session.get("scenarios") or [] if one not in runs]
        waiting = not runs and not session.get("scenarios")
        shape = (tuple((key, id(run.get("tree"))) for key, run in runs.items()),
                 tuple(pending), waiting)
        if shape != self._shape:
            self._shape = shape
            self._build_tree(runs, pending, waiting)
        self._paint_tree(runs)

    def _build_tree(self, runs, pending, waiting):
        while self.steps_layout.count():
            item = self.steps_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._headers, self._rows, self._live = {}, {}, None

        if waiting:
            hint = QLabel("waiting for the first scenario\u2026")
            hint.setStyleSheet("color: %s; font-size: 12px;" % theme.NEUTRAL[600])
            self.steps_layout.addWidget(hint)
            return

        # Not drawn while it is filled: hundreds of rows each asking for a
        # layout pass and a repaint is most of what building them costs.
        self.steps.setUpdatesEnabled(False)
        try:
            # Filed under the scenario's id; shown by its name, which is what the
            # overlay shows too.
            for key, run in runs.items():
                header = _ScenarioHeader(run.get("scenario") or key)
                self._headers[key] = header
                self.steps_layout.addWidget(header.row)
                cells = self._rows[key] = {}
                for node, depth in _walk(run.get("tree") or {}):
                    if depth == 0:
                        continue        # the header above already names the flow
                    row, step = _step_row(node, depth)
                    if step is not None:
                        cells[node.get("step_index")] = step
                    self.steps_layout.addWidget(row)
            for scenario in pending:
                header = _ScenarioHeader(scenario)
                header.paint({"status": "pending"})
                self.steps_layout.addWidget(header.row)
        finally:
            self.steps.setUpdatesEnabled(True)

    def _paint_tree(self, runs):
        """Bring the built rows up to date; a row that has not changed is not touched."""
        self._live = None
        for key, run in runs.items():
            header = self._headers.get(key)
            if header is not None:
                header.paint(run)
                if run.get("status") == "running" and run.get("started"):
                    self._live = (header.took, run)
            steps = run.get("steps") or {}
            for index, cells in self._rows.get(key, {}).items():
                cells.paint(steps.get(index) or {})

    def tick(self, now=None):
        """Move the running scenario's time on, and nothing else."""
        if self._live is not None:
            label, run = self._live
            label.setText(_took(run, now))


def _restyle(widget, **properties):
    """Set style properties and have the stylesheet read them again."""
    for name, value in properties.items():
        widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


class _ScenarioHeader:
    """One scenario's own line: its mark, its name, how far it got, how long it took.

    Styled through theme roles (QLabel[role="runscenario"][status=...]), so a
    scenario finishing changes a property instead of a stylesheet of its own.
    ``took`` is kept apart so the page's clock can move a running scenario's
    time on without anything else being touched.
    """

    def __init__(self, scenario):
        self.scenario = scenario
        self.mark = _mark_label(*_mark("pending"))
        self.label = QLabel(scenario)
        self.label.setProperty("role", "runscenario")
        self.took = QLabel("")
        self.took.setProperty("role", "runtook")
        self.row = QWidget()
        line = QHBoxLayout(self.row)
        line.setContentsMargins(0, 6, 0, 0)
        line.setSpacing(0)
        line.addWidget(self.mark)
        line.addWidget(self.label)
        line.addSpacing(14)
        line.addWidget(self.took)
        line.addStretch(1)
        self._painted = None

    def paint(self, run):
        status = run.get("status", "pending")
        counts = ""
        if run.get("total"):
            counts = "   %s/%s" % (run.get("done", 0), run["total"])
        took = _took(run)
        painted = (status, counts, took)
        if painted == self._painted:
            return
        if self._painted is None or self._painted[0] != status:
            name, colour = _mark(status)
            self.mark.setPixmap(icons.pixmap(name, 13, colour))
            _restyle(self.label, status=status)
        self.label.setText("%s%s" % (self.scenario, counts))
        self.took.setText(took)
        self._painted = painted


class _StepCells:
    """A step row's mark and label, repainted only when its state changes."""

    def __init__(self, mark, label):
        self.mark, self.label = mark, label
        # Built as a step still to come, so most rows never need a paint at all.
        self._painted = ("pending", "")

    def paint(self, state):
        status = state.get("status", "pending")
        message = state.get("message") if status in ("fail", "error") else ""
        painted = (status, message or "")
        if painted == self._painted:
            return
        name, colour = _mark(status)
        self.mark.setPixmap(icons.pixmap(name, 12, colour))
        _restyle(self.label, status=status)
        self.label.setToolTip(message or "")
        self._painted = painted


def _walk(node, depth=0):
    """Depth-first (node, depth) pairs, in the order the runner executes them."""
    yield node, depth
    for child in node.get("children") or []:
        yield from _walk(child, depth + 1)


def _step_row(node, depth):
    """A row of the tree, and for a step the cells that show its state."""
    is_step = node.get("kind") == "step"
    row = QWidget()
    line = QHBoxLayout(row)
    line.setContentsMargins(depth * INDENT_PX, 0, 0, 0)
    line.setSpacing(0)
    label = QLabel(node.get("label", ""))
    if is_step:
        mark = _mark_label(*_mark("pending"), size=12)
        label.setProperty("role", "runstep")
    else:
        mark = _mark_label("group", theme.ACCENT_RAMP[800], 13)
        label.setProperty("role", "rungroup")
    line.addWidget(mark)
    line.addWidget(label, 1)
    return row, (_StepCells(mark, label) if is_step else None)


class RunPage(QWidget):
    """Header (state, elapsed) plus one panel per session, and the summary."""

    open_artifacts = Signal()

    def __init__(self, run_state, parent=None):
        super().__init__(parent)
        self.run_state = run_state
        self._panels = {}
        self._started_at = None
        # When the scenarios ended. Kept, because run_finished is called twice -
        # once for run.finished and again when the launcher exits, which without
        # --close-after is whenever the windows are closed.
        self._finished_at = None

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        head = QWidget()
        head.setProperty("role", "bar")
        widgets.scoped_style(head, "border-bottom: 1px solid %s;" % theme.DIVIDER)
        head_layout = QVBoxLayout(head)
        head_layout.setContentsMargins(24, 14, 24, 12)
        self.tag = widgets.Tag("IDLE", "neutral")
        self.meta = widgets.mono("")
        self.elapsed = widgets.mono("—")
        head_layout.addWidget(widgets.row(widgets.heading("Run"), self.tag,
                                          self.meta, None, self.elapsed))
        column.addWidget(head)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        self.body_layout = QVBoxLayout(body)
        self.body_layout.setContentsMargins(24, 18, 24, 18)
        self.body_layout.setSpacing(16)
        self.placeholder = widgets.lede(
            "No run yet. Build a command on the Command page and press RUN - "
            "windows, scenarios and every step appear here as the launcher reports "
            "them.")
        self.body_layout.addWidget(self.placeholder)
        self.body_layout.addStretch(1)
        scroll.setWidget(body)
        column.addWidget(scroll, 1)
        self._scroll = scroll           # Search scrolls a session's panel into view

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setVisible(False)
        self.summary.setStyleSheet(
            "background: %s; color: %s; font-family: %s; font-size: 13px; padding: "
            "14px 18px;" % (theme.ACCENT_RAMP[900], theme.NEUTRAL[100], theme.MONO_CSS))
        column.addWidget(self.summary)

        self._refresh_pending = False
        run_state.changed.connect(self._schedule_refresh)
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)

    def _schedule_refresh(self):
        if not self._refresh_pending:
            self._refresh_pending = True
            QTimer.singleShot(0, self.refresh)

    # -- lifecycle ------------------------------------------------------------
    # -- search (Ctrl+K) -------------------------------------------------------
    def search_rows(self):
        """The run's sessions, in the words their own panels use for their state."""
        state = self.run_state
        going = bool(state.started and state.exit_code is None)
        found = []
        for name in dict.fromkeys(list(state.order) + list(state.sessions)):
            session = state.sessions.get(name)
            if session is None:
                continue
            label, variant = STATE_TAGS.get(session["state"],
                                            (session["state"].upper(), "neutral"))
            found.append(search.hit(
                name, " · ".join(part for part in (session.get("login") or "",
                                                   session.get("scenario") or "") if part),
                label, variant, keys=" ".join(session.get("scenarios") or []),
                target=name, live=going and session["state"] != "closed"))
        return found

    def show_found(self, name):
        panel = self._panels.get(name)
        if panel is None:
            return False
        QTimer.singleShot(0, lambda: self._scroll.ensureWidgetVisible(panel))
        return True

    def run_started(self, meta=""):
        self._started_at = time.time()
        self._finished_at = None
        self._panels.clear()
        while self.body_layout.count():
            item = self.body_layout.takeAt(0)
            if item.widget() and item.widget() is not self.placeholder:
                item.widget().deleteLater()
        self.body_layout.addWidget(self.placeholder)
        self.body_layout.addStretch(1)
        self.placeholder.setText("Launching…")
        self.placeholder.setVisible(True)
        self.summary.setVisible(False)
        self.tag.set("RUNNING", "outline")
        self.meta.setText(meta)
        self._timer.start()

    def run_finished(self, code):
        """Settle the header. Called when the SCENARIOS end, not the process.

        Idempotent: run.finished lands first, and the launcher exiting later
        calls this again with the same answer.
        """
        self._timer.stop()
        if self._started_at and self._finished_at is None:
            self._finished_at = time.time()
        took = (format_duration(self._finished_at - self._started_at)
                if self._started_at else "")
        if took:
            self.elapsed.setText(took)
        self.tag.set("FINISHED (%d)" % code, "accent" if code == 0 else "bad")
        summary = self.run_state.summary
        if summary:
            self.summary.setText(
                "RUN SUMMARY    %d/%d passed · %s · exit %s · %s"
                % (summary.get("passed", 0), summary.get("total", 0), took or "—",
                   summary.get("exit_code", code), self.run_state.run_dir or "—"))
            self.summary.setVisible(True)
        elif self.run_state.run_dir:
            self.summary.setText("Reports -> %s" % self.run_state.run_dir)
            self.summary.setVisible(True)

    def _tick(self):
        if self._started_at:
            now = time.time()
            self.elapsed.setText(format_duration(now - self._started_at))
            for panel in self._panels.values():
                panel.tick(now)

    # -- rendering ------------------------------------------------------------
    def refresh(self):
        self._refresh_pending = False
        sessions = self.run_state.ordered()
        if sessions:
            self.placeholder.setVisible(False)
        for session in sessions:
            panel = self._panels.get(session["name"])
            if panel is None:
                panel = SessionPanel()
                self._panels[session["name"]] = panel
                # Keep the trailing stretch last.
                self.body_layout.insertWidget(self.body_layout.count() - 1, panel)
            panel.update_from(session)
