"""One session's backend log, in a window of its own.

The panel on the Run page keeps a folded section with the last of it, which is
right for glancing while a run goes past. Reading a log is a different activity:
it wants width for lines that are two hundred characters long, height for more
than a dozen of them at once, and somewhere to put a search that is not competing
with a step tree for the same screen. So this is the same lines, full size.

It is not modal and not a child of the panel's lifetime: several sessions can be
open side by side, which is the whole reason a per-session log is worth having
when ten windows are running as ten roles.

Live, not a snapshot - the panel pushes every refresh through :meth:`update_from`.
"""

import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QLabel,
                               QLineEdit, QPlainTextEdit,
                               QPushButton, QVBoxLayout, QWidget)

from .. import theme, widgets

#: Severity, least to most - what engine.serverlog puts on the wire.
SEVERITY = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3, "CRITICAL": 4}

#: What the filter offers, as a threshold: picking one means "this and worse".
#: DEBUG is not among them on purpose - it is the floor, so choosing it would be
#: the same as ALL, while choosing INFO is the useful thing (hide the chatter).
LEVELS = ["ALL", "INFO", "WARN", "ERROR", "CRITICAL"]


def level_html(level, text):
    """One log line as HTML, coloured by severity.

    Read from the theme at call time, never captured into a module-level map:
    the palette is swapped in place when dark mode is toggled, and a dict built
    at import would keep painting a log in the colours of the other theme.
    """
    escaped = html.escape(text)
    colour = theme.LOG_LEVEL.get(level)
    if not colour:
        return escaped
    style = "color:%s" % colour
    if level == "CRITICAL":
        # Weight and a wash rather than a new hue: a critical is not a different
        # kind of thing from an error, it is the same thing gone further.
        style += ";font-weight:600;background-color:%s" % theme.LOG_CRITICAL_BG
    return '<span style="%s">%s</span>' % (style, escaped)

ALL_LOGS = "All logs"

#: Lines painted at once. The model keeps more (cms_gui.runner.SERVER_LOG_LINES);
#: this is what a scroll-back is worth rendering.
MAX_LINES = 5000


class ServerLogWindow(QWidget):
    """Everything one session's backend logs have said, with a way through it."""

    #: The panel puts its inline view back when this closes, so it has to be told.
    closed = Signal()

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)

    def __init__(self, session_name, parent=None):
        # No parent: a window, not a panel inside one. Passing the panel would tie
        # this to its lifetime, and the panel is rebuilt as a run goes on.
        super().__init__(None)
        self.setWindowTitle("Server log - %s" % session_name)
        self.frame = widgets.dress(self, minimizable=True)
        self.setWindowFlag(Qt.Window, True)
        self.resize(1180, 760)
        self._session_name = session_name
        self._lines = []
        self._painted = None

        column = QVBoxLayout(self)
        column.setContentsMargins(18, 14, 18, 14)
        column.setSpacing(8)

        self.heading = QLabel(session_name)
        self.heading.setStyleSheet("font-family: %s; font-size: 14px; "
                                   "font-weight: 500;" % theme.MONO_CSS)
        column.addWidget(self.heading)

        self.level = widgets.Segmented(LEVELS, "ALL")
        self.level.changed.connect(lambda _v: self._repaint(force=True))
        self.log = QComboBox()
        self.log.addItem(ALL_LOGS)
        self.log.currentIndexChanged.connect(lambda _i: self._repaint(force=True))
        column.addWidget(self._controls())

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.view.setMaximumBlockCount(MAX_LINES)
        self.view.setStyleSheet("font-family: %s; font-size: 12px;" % theme.MONO_CSS)
        column.addWidget(self.view, 1)

        self.count = widgets.mono("")
        column.addWidget(self.count)

    def _controls(self):
        self.search = QLineEdit()
        self.search.setPlaceholderText("search these lines…")
        self.search.textChanged.connect(lambda _t: self._repaint(force=True))
        # Following is what you want while a run is going and exactly what you do
        # not want while reading something that scrolled past, so it is a switch
        # rather than a rule.
        self.follow = QCheckBox("Follow")
        self.follow.setChecked(True)
        save = QPushButton("Save…")
        save.setProperty("variant", "ghost")
        save.clicked.connect(self._save)
        return widgets.row(self.search, self.level, self.log, self.follow, None, save)

    # -- data -----------------------------------------------------------------
    def update_from(self, session):
        """Take this session's current lines. Called on every Run page refresh."""
        self._lines = list(session.get("server") or ())
        names = list(session.get("server_logs") or ())
        current = self.log.currentText()
        if [self.log.itemText(i) for i in range(1, self.log.count())] != names:
            self.log.blockSignals(True)
            self.log.clear()
            self.log.addItems([ALL_LOGS] + names)
            index = self.log.findText(current)
            self.log.setCurrentIndex(index if index >= 0 else 0)
            self.log.blockSignals(False)
        self.log.setVisible(len(names) > 1)
        self._repaint()

    def visible_lines(self):
        chosen = self.log.currentText()
        wanted = SEVERITY.get(self.level.current(), -1)
        needle = self.search.text().strip().lower()
        out = []
        for line in self._lines:
            if chosen and chosen != ALL_LOGS and line["log"] != chosen:
                continue
            if SEVERITY.get(line["level"], SEVERITY["INFO"]) < wanted:
                continue
            if needle and needle not in line["text"].lower():
                continue
            out.append(line)
        return out

    def _repaint(self, force=False):
        lines = self.visible_lines()
        key = (len(lines), self.level.current(), self.log.currentText(),
               self.search.text(), lines[-1]["text"] if lines else "")
        if key == self._painted and not force:
            return
        self._painted = key
        many = len({line["log"] for line in lines}) > 1
        self.view.clear()
        for line in lines[-MAX_LINES:]:
            prefix = "[%s] " % line["log"] if many else ""
            self.view.appendHtml('<pre style="margin:0">%s</pre>'
                                 % level_html(line["level"], prefix + line["text"]))
        self.count.setText("%d of %d line%s" % (len(lines), len(self._lines),
                                                "" if len(self._lines) == 1 else "s"))
        if self.follow.isChecked():
            self.view.moveCursor(QTextCursor.End)
            bar = self.view.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _save(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save server log", "%s-server.log" % self._session_name,
            "Log files (*.log);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join("[%s] %s" % (line["log"], line["text"])
                                   for line in self.visible_lines()))
        except OSError as exc:
            widgets.warn(self, "Save server log", str(exc))
