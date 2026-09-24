"""Artifacts page: a run's report tree, with a preview.

During a run it is fed by two events - ``run.dir`` names the run directory,
``artifacts.written`` announces each scenario's files as they land - so the tree
fills in as the run goes rather than after it.

Between runs it remembers. The run directory used to arrive in an event and
disappear with the process, which left this page blank on every restart even
though the reports were still sitting on disk. Now the last directory is stored,
and every recorded run that still has its reports is offered in the picker, so the
page opens on something useful - the newest run, unless the user was looking at
another one.
"""

import json
import os
import time

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout, QLabel,
                               QPlainTextEdit, QPushButton, QScrollArea, QSplitter,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import history as history_mod, icons, theme, widgets

TEXT_SUFFIXES = (".json", ".log", ".txt", ".html", ".yaml", ".yml", ".md", ".csv")
HTML_SUFFIXES = (".html", ".htm")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")
PREVIEW_LIMIT = 400_000     # bytes of a text file worth putting on screen

CURRENT = "Current run"
NO_RUNS = "no reports yet"


class ArtifactsPage(QWidget):
    """A tree of the chosen run's report directory, plus a preview of one file."""

    def __init__(self, settings=None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.history = None
        self._run_dir = ""          # the live run's directory, when there is one
        self._shown_dir = ""        # what the tree is showing
        self._loading = False
        self.html = None             # start WebEngine only when opening HTML
        # Reserve Qt Quick composition before the main window is shown. The
        # WebEngine view creates its own Quick widget only when first loaded;
        # adding that to a visible raster window destroys and recreates the
        # native window. This empty, hidden surface avoids that transition
        # without starting Chromium or reading an artifact during startup.
        self._html_surface = QQuickWidget(self)
        self._html_surface.hide()

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        head = QWidget()
        widgets.scoped_style(head, "border-bottom: 1px solid %s;" % theme.DIVIDER)
        head_layout = QVBoxLayout(head)
        head_layout.setContentsMargins(24, 14, 24, 12)
        self.run_combo = QComboBox()
        self.run_combo.setMinimumWidth(240)
        self.run_combo.setMaximumWidth(420)
        self.run_combo.currentIndexChanged.connect(self._run_chosen)
        self.run_label = widgets.elided_mono(NO_RUNS)
        browse = icons.button(QPushButton("Open another folder"), "browse")
        browse.clicked.connect(self._browse)
        self.open_dir = QPushButton("Open folder")
        self.open_dir.clicked.connect(self._open_run_dir)
        self.open_dir.setEnabled(False)
        refresh = QPushButton("Rescan")
        refresh.clicked.connect(self.rescan)
        head_layout.addWidget(widgets.row(widgets.heading("Artifacts"), None,
                                          browse, refresh, self.open_dir))
        # Picker and path on their own line, laid out the same way as the Log
        # page's, so the two observing pages read alike. Crowding them into the row
        # above pushed the window's minimum width past 1500px.
        second = QHBoxLayout()
        second.setContentsMargins(0, 6, 0, 0)
        second.setSpacing(10)
        second.addWidget(widgets.kicker("run"))
        second.addWidget(self.run_combo)
        second.addWidget(self.run_label, 1)
        head_layout.addLayout(second)
        column.addWidget(head)

        splitter = QSplitter(Qt.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["File", "Size", "Written"])
        self.tree.setColumnWidth(0, 200)
        self.tree.setColumnWidth(1, 55)
        self.tree.setColumnWidth(2, 105)
        self.tree.itemSelectionChanged.connect(self._preview_selected)
        splitter.addWidget(self.tree)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        path_bar = QWidget()
        widgets.scoped_style(path_bar, "border-bottom: 1px solid %s;" % theme.DIVIDER)
        path_layout = QVBoxLayout(path_bar)
        path_layout.setContentsMargins(16, 10, 16, 10)
        self.path_label = widgets.elided_mono("")
        self.open_file = QPushButton("Open in OS")
        self.open_file.clicked.connect(self._open_selected)
        self.open_file.setEnabled(False)
        # Built by hand: an elided label has no width of its own to claim, so the
        # leftover space has to be handed to it explicitly.
        path_row = QHBoxLayout()
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.setSpacing(8)
        path_row.addWidget(self.path_label, 1)
        self.preview_mode = QComboBox()
        self.preview_mode.addItems(["Preview", "Source"])
        self.preview_mode.setToolTip("Show the HTML page or its source")
        self.preview_mode.setVisible(False)
        self.preview_mode.currentIndexChanged.connect(self._preview_selected)
        path_row.addWidget(self.preview_mode)
        path_row.addWidget(self.open_file)
        path_layout.addLayout(path_row)
        right_layout.addWidget(path_bar)

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setStyleSheet(
            "background: %s; border: none; font-family: %s; font-size: 12px;"
            % (theme.NEUTRAL[100], theme.MONO_CSS))
        right_layout.addWidget(self.text, 1)

        self.image_area = QScrollArea()
        self.image_area.setWidgetResizable(True)
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignCenter)
        self.image_area.setWidget(self.image)
        self.image_area.setVisible(False)
        right_layout.addWidget(self.image_area, 1)
        self._preview_layout = right_layout

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([390, 700])
        column.addWidget(splitter, 1)

        self._reload_runs()
        self._restore()

    # -- what to show ---------------------------------------------------------
    def set_history(self, history):
        """Take the recorded runs as the list of directories worth offering."""
        self.history = history
        history.changed.connect(self._reload_runs)
        self._reload_runs()
        if not self._shown_dir:
            self._restore()

    def set_run_dir(self, path):
        """The live run announced its directory."""
        path = path or ""
        if not path:
            return
        self._run_dir = path
        # Rebuild before showing: the picker had no "Current run" item until this
        # moment, so syncing to the path first would file it under its bare folder
        # name instead.
        self._reload_runs()
        self.show_dir(path)

    def show_dir(self, path):
        """Point the page at a directory and remember it for next time."""
        self._shown_dir = path or ""
        if self.settings is not None:
            self.settings.artifacts_dir = self._shown_dir
        self._sync_combo()
        self.run_label.setText(self._shown_dir or NO_RUNS)
        self.open_dir.setEnabled(bool(self._shown_dir
                                      and os.path.isdir(self._shown_dir)))
        self.rescan()

    def note_artifacts(self, _event):
        """A scenario finished writing; the cheapest correct answer is a rescan."""
        self.rescan()

    def rescan(self):
        selected = self._selected_path()
        self.tree.clear()
        directory = self._shown_dir
        if not directory or not os.path.isdir(directory):
            return
        root = QTreeWidgetItem([os.path.basename(directory.rstrip(os.sep)) or
                                directory, "", ""])
        root.setData(0, Qt.UserRole, directory)
        root.setFont(0, theme.mono_font(9))
        self.tree.addTopLevelItem(root)
        self._fill(root, directory)
        root.setExpanded(True)
        if selected:
            self._select_path(selected)

    def _fill(self, parent_item, directory):
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return
        for name in names:
            path = os.path.join(directory, name)
            if os.path.isdir(path):
                item = QTreeWidgetItem([name + "/", "", ""])
                item.setData(0, Qt.UserRole, path)
                item.setFont(0, theme.mono_font(9))
                parent_item.addChild(item)
                self._fill(item, path)
                item.setExpanded(True)
            else:
                item = QTreeWidgetItem([name, _human_size(path),
                                        _clock(_mtime(path))])
                item.setData(0, Qt.UserRole, path)
                item.setFont(0, theme.mono_font(9))
                item.setFont(1, theme.mono_font(8))
                item.setFont(2, theme.mono_font(8))
                parent_item.addChild(item)

    # -- the run picker -------------------------------------------------------
    def _reload_runs(self):
        """Offer the live run plus every recorded run whose reports still exist."""
        chosen = self._shown_dir
        self._loading = True
        try:
            self.run_combo.clear()
            if self._run_dir:
                self.run_combo.addItem(CURRENT, self._run_dir)
            entries = self.history.with_artifacts() if self.history else []
            for entry in entries:
                if entry.get("run_dir") == self._run_dir:
                    continue        # already offered as the live run
                self.run_combo.addItem(history_mod.entry_label(entry),
                                       entry["run_dir"])
            if self.run_combo.count() == 0:
                self.run_combo.addItem(NO_RUNS, "")
            self.run_combo.setEnabled(bool(self.run_combo.itemData(0)))
        finally:
            self._loading = False
        self._sync_combo(chosen)

    def _sync_combo(self, path=None):
        """Point the combo at ``path`` without treating it as a user choice."""
        path = self._shown_dir if path is None else path
        index = self.run_combo.findData(path)
        if index < 0 and path:
            # A folder opened by hand, or a run that has since been forgotten.
            self._loading = True
            self.run_combo.addItem(os.path.basename(path.rstrip(os.sep)) or path, path)
            self.run_combo.setEnabled(True)
            self._loading = False
            index = self.run_combo.findData(path)
        if index >= 0:
            self._loading = True
            self.run_combo.setCurrentIndex(index)
            self._loading = False

    def _run_chosen(self, _index):
        if self._loading:
            return
        path = self.run_combo.currentData()
        if path and path != self._shown_dir:
            self.show_dir(path)

    def _browse(self):
        start = self._shown_dir or os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(self, "Open a report folder", start)
        if chosen:
            self.show_dir(chosen)

    def _restore(self):
        """Come back to whatever was on screen last time the app was open."""
        remembered = self.settings.artifacts_dir if self.settings is not None else ""
        if remembered and os.path.isdir(remembered):
            self.show_dir(remembered)
            return
        entries = self.history.with_artifacts() if self.history else []
        if entries:
            self.show_dir(entries[0]["run_dir"])

    # -- preview --------------------------------------------------------------
    def _selected_path(self):
        items = self.tree.selectedItems()
        return items[0].data(0, Qt.UserRole) if items else ""

    def _select_path(self, path):
        iterator = self.tree.findItems("", Qt.MatchContains | Qt.MatchRecursive, 0)
        for item in iterator:
            if item.data(0, Qt.UserRole) == path:
                self.tree.setCurrentItem(item)
                return True
        return False

    def _preview_selected(self):
        path = self._selected_path()
        self.path_label.setText(path)
        self.open_file.setEnabled(bool(path))
        suffix = os.path.splitext(path)[1].lower()
        is_html = bool(path and not os.path.isdir(path) and suffix in HTML_SUFFIXES)
        self.preview_mode.setVisible(is_html)
        if self.html is not None:
            self.html.stop()
            self.html.setVisible(False)
        if not path or os.path.isdir(path):
            self._show_text("" if not path else "Directory")
            return
        if is_html and self.preview_mode.currentIndex() == 0:
            self._show_html(path)
            return
        if suffix in IMAGE_SUFFIXES:
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                self.text.setVisible(False)
                self.image_area.setVisible(True)
                self.image.setPixmap(pixmap.scaled(
                    1200, 900, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                return
        if suffix in TEXT_SUFFIXES or is_html:
            try:
                size = os.path.getsize(path)
                with open(path, encoding="utf-8", errors="replace") as fh:
                    body = fh.read(PREVIEW_LIMIT)
                if size > PREVIEW_LIMIT:
                    body += "\n\n… truncated (%s total)" % _human_size(path)
                if suffix == ".json":
                    try:
                        body = json.dumps(json.loads(body), indent=2, ensure_ascii=False)
                    except ValueError:
                        pass
                self._show_text(body)
                return
            except OSError as exc:
                self._show_text("Cannot read: %s" % exc)
                return
        self._show_text("(no preview for %s — use Open in OS)" % (suffix or "this file"))

    def _show_text(self, body):
        if self.html is not None:
            self.html.setVisible(False)
        self.image_area.setVisible(False)
        self.text.setVisible(True)
        self.text.setPlainText(body)

    def _show_html(self, path):
        if not os.path.isfile(path):
            self._show_text("Cannot read: file no longer exists")
            return
        if self.html is None:
            from ..htmlpreview import HtmlPreview

            self.html = HtmlPreview(self)
            self.html.local_file_requested.connect(self._select_path, Qt.QueuedConnection)
            self._preview_layout.addWidget(self.html, 1)
        self.text.setVisible(False)
        self.image_area.setVisible(False)
        self.html.setVisible(True)
        self.html.load_file(path, self._shown_dir)

    # -- opening --------------------------------------------------------------
    def _open_selected(self):
        path = self._selected_path()
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _open_run_dir(self):
        if self._shown_dir:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._shown_dir))


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _clock(mtime):
    return time.strftime("%H:%M:%S", time.localtime(mtime)) if mtime else ""


def _human_size(path):
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%.0f %s" % (size, unit) if unit == "B" else "%.1f %s" % (size, unit)
        size /= 1024.0
    return ""
