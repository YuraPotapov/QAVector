"""Log page: the launcher's stderr, filterable, live or from an earlier run.

The core already tags each line with the session that produced it during
parallel runs (``[dev-agent] ...``), so per-session filtering costs nothing but
a regex that is already applied when the line arrives.

The page used to hold nothing but the current run: starting a run cleared it, and
closing the app lost it. Every run's log is now archived when it ends, so this
page can also open any of them, newest first. An archived log is parsed back into
the same records a live line produces, so the filters, the colouring and the
session picker work on it exactly as they do live.
"""

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QHBoxLayout,
                               QLineEdit, QPlainTextEdit, QPushButton,
                               QVBoxLayout, QWidget)

from .. import history as history_mod, theme, widgets
from ..runner import parse_log_line

LEVEL_COLOR = {
    "DEBUG": theme.NEUTRAL[500],
    "INFO": theme.NEUTRAL[800],
    "WARNING": theme.WARN,
    "ERROR": theme.BAD,
    "CRITICAL": theme.BAD,
}
LEVEL_ORDER = ["ALL", "DEBUG", "INFO", "WARNING", "ERROR"]

LIVE = "Live run"
MAX_ARCHIVE_BYTES = 8_000_000      # a log this big is a disk problem, not a read


class LogPage(QWidget):
    """Everything the launcher wrote to stderr, in order, with a filter."""

    MAX_LINES = 20000      # a long parallel run is chatty; keep memory bounded

    def __init__(self, settings=None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.history = None
        self._lines = []
        self._sessions = []
        self._live = []             # the running log, kept while an archive is shown
        self._showing = ""          # "" = live, otherwise an archived log's path
        self._loading = False

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        head = QWidget()
        widgets.scoped_style(head, "border-bottom: 1px solid %s;" % theme.DIVIDER)
        head_layout = QVBoxLayout(head)
        head_layout.setContentsMargins(24, 14, 24, 12)
        self.level = widgets.Segmented(LEVEL_ORDER, "ALL")
        self.level.changed.connect(lambda _v: self._rerender())
        self.session = QComboBox()
        self.session.addItem("all sessions")
        self.session.currentIndexChanged.connect(lambda _i: self._rerender())
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search…")
        self.search.setFixedWidth(190)
        self.search.textChanged.connect(lambda _t: self._rerender())
        self.follow = QCheckBox("Follow")
        self.follow.setChecked(True)
        save = QPushButton("Save…")
        save.clicked.connect(self.save_to_file)
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.clear)
        self.source = QComboBox()
        self.source.setMinimumWidth(240)
        self.source.setMaximumWidth(420)
        self.source.currentIndexChanged.connect(self._source_chosen)
        head_layout.addWidget(widgets.row(
            widgets.heading("Log"), None,
            self.level, self.session, self.search, self.follow, save,
            self.clear_button))
        # The picker sits on its own line with the note. Crowding it into the row
        # above pushed the window's minimum width past 1500px, and this page sets
        # that minimum for every page in the stack.
        self.note = widgets.elided_mono("stderr of the launcher process")
        second = QHBoxLayout()
        second.setContentsMargins(0, 6, 0, 0)
        second.setSpacing(10)
        second.addWidget(widgets.kicker("showing"))
        second.addWidget(self.source)
        second.addWidget(self.note, 1)
        head_layout.addLayout(second)
        column.addWidget(head)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(self.MAX_LINES)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.view.setStyleSheet(
            "background: %s; border: none; font-family: %s; font-size: 12px;"
            % (theme.NEUTRAL[100], theme.MONO_CSS))
        column.addWidget(self.view, 1)

        self._reload_sources()
        self._restore()

    # -- input ----------------------------------------------------------------
    def append(self, record):
        """One live line. Kept even while an archived log is on screen."""
        self._live.append(record)
        if len(self._live) > self.MAX_LINES:
            del self._live[:len(self._live) - self.MAX_LINES]
        if self._showing:
            return                  # an archive is on screen; do not mix them
        self._lines = self._live
        session = record.get("session", "")
        if session and session not in self._sessions:
            self._sessions.append(session)
            self.session.addItem(session)
        if self._passes(record):
            self._write(record)

    def clear(self):
        """A new run is starting: drop the live log and come back to it."""
        self._live = []
        self._showing = ""
        self._lines = self._live
        self._sessions = []
        self._reset_sessions()
        self.view.clear()
        self._sync_source()
        self._update_note()

    # -- which log is on screen -----------------------------------------------
    def set_history(self, history):
        """Take the recorded runs as the list of logs worth offering."""
        self.history = history
        history.changed.connect(self._reload_sources)
        self._reload_sources()
        if not self._showing:
            self._restore()

    def show_live(self):
        if self._showing:
            self._showing = ""
            self._lines = self._live
            self._reindex()
        self._sync_source()
        self._update_note()

    def show_file(self, path):
        """Load an archived log and render it as if it had just arrived."""
        records = _read_archive(path)
        if records is None:
            widgets.warn(self, "Log", "Cannot read %s." % path)
            self.show_live()
            return False
        self._showing = path
        self._lines = records
        self._reindex()
        self._sync_source()
        self._update_note()
        if self.settings is not None:
            self.settings.log_source = path
        return True

    def _reload_sources(self):
        chosen = self._showing
        self._loading = True
        try:
            self.source.clear()
            self.source.addItem(LIVE, "")
            entries = self.history.with_logs() if self.history else []
            for entry in entries:
                self.source.addItem(history_mod.entry_label(entry),
                                    entry["log_file"])
        finally:
            self._loading = False
        self._sync_source(chosen)

    def _sync_source(self, path=None):
        path = self._showing if path is None else path
        index = self.source.findData(path or "")
        if index < 0 and path:
            self._loading = True
            self.source.addItem(os.path.basename(path), path)
            self._loading = False
            index = self.source.findData(path)
        self._loading = True
        self.source.setCurrentIndex(index if index >= 0 else 0)
        self._loading = False

    def _source_chosen(self, _index):
        if self._loading:
            return
        path = self.source.currentData() or ""
        if path == self._showing:
            return
        if path:
            self.show_file(path)
        else:
            self.show_live()
            if self.settings is not None:
                self.settings.log_source = ""

    def _restore(self):
        remembered = self.settings.log_source if self.settings is not None else ""
        if remembered and os.path.isfile(remembered):
            self.show_file(remembered)

    def _reindex(self):
        """Rebuild the session picker from whatever is now on screen, and repaint."""
        self._sessions = []
        for record in self._lines:
            session = record.get("session", "")
            if session and session not in self._sessions:
                self._sessions.append(session)
        self._reset_sessions()
        self._rerender()

    def _reset_sessions(self):
        current = self.session.currentText()
        self.session.blockSignals(True)
        self.session.clear()
        self.session.addItem("all sessions")
        for session in self._sessions:
            self.session.addItem(session)
        index = self.session.findText(current)
        self.session.setCurrentIndex(index if index > 0 else 0)
        self.session.blockSignals(False)

    def _update_note(self):
        """Say plainly whether this is the running log or a finished one."""
        archived = bool(self._showing)
        # Following and clearing only mean something for a log that is still
        # growing; on an archive they would just be confusing.
        self.follow.setEnabled(not archived)
        self.clear_button.setEnabled(not archived)
        if archived:
            self.note.setText("archived log · %d lines · %s"
                              % (len(self._lines), self._showing))
        else:
            self.note.setText("stderr of the launcher process")

    # -- filtering ------------------------------------------------------------
    def _passes(self, record):
        level = self.level.current()
        if level != "ALL" and record.get("level") != level:
            return False
        chosen = self.session.currentText()
        if self.session.currentIndex() > 0 and record.get("session") != chosen:
            return False
        needle = self.search.text().strip().lower()
        if needle:
            haystack = (record.get("text", "") + record.get("session", "")).lower()
            if needle not in haystack:
                return False
        return True

    def _rerender(self):
        self.view.clear()
        for record in self._lines:
            if self._passes(record):
                self._write(record)

    def _write(self, record):
        level = record.get("level", "")
        colour = LEVEL_COLOR.get(level, theme.NEUTRAL[700])
        session = record.get("session", "")
        # A rich-text append per line is affordable at this volume and keeps the
        # level/session colouring the design asks for.
        html = ('<span style="color:%s">%s</span> '
                '<span style="color:%s">%-7s</span> '
                '<span style="color:%s">%s</span> '
                '<span style="color:%s">%s</span>'
                % (theme.NEUTRAL[500], _escape(record.get("ts", "")),
                   colour, _escape(level),
                   theme.ACCENT_RAMP[700], _escape("[%s]" % session if session else ""),
                   theme.NEUTRAL[800], _escape(record.get("text", ""))))
        self.view.appendHtml(html)
        if self.follow.isChecked():
            self.view.moveCursor(QTextCursor.End)

    # -- export ---------------------------------------------------------------
    def write_to(self, path):
        """Write every line held here as plain text; returns True on success.

        Separate from :meth:`save_to_file` because the same text is archived
        silently at the end of every run, so that a history entry can still show
        its log after the page has been cleared for the next one.
        """
        try:
            with open(path, "w", encoding="utf-8") as fh:
                for record in self._lines:
                    session = ("[%s] " % record["session"]) if record.get("session") else ""
                    fh.write("%s %-7s %s%s\n" % (record.get("ts", ""),
                                                 record.get("level", ""), session,
                                                 record.get("text", "")))
        except OSError:
            return False
        return True

    def save_to_file(self):
        path, _filter = QFileDialog.getSaveFileName(
            self, "Save log", os.path.join(os.path.expanduser("~"), "launcher.log"),
            "Log files (*.log *.txt);;All files (*)")
        if not path:
            return
        if not self.write_to(path):
            widgets.warn(self, "Save log", "Could not write %s." % path)


def _read_archive(path):
    """An archived log as the same records a live line produces, or None.

    ``write_to`` writes the launcher's own console format back out, so parsing it
    with the runner's parser is exact for every line the launcher wrote; the few
    the GUI itself injected (the ``$ argv`` line) come back as plain text, which is
    how they were shown live too.
    """
    try:
        if os.path.getsize(path) > MAX_ARCHIVE_BYTES:
            return None
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    records = [parse_log_line(line) for line in lines if line.strip()]
    return records[-LogPage.MAX_LINES:]


def _escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace(" ", "&nbsp;"))
