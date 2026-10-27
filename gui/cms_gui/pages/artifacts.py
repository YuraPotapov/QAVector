"""Artifacts page: a run's report tree, with a preview.

During a run it is fed by two events - ``run.dir`` names the run directory,
``artifacts.written`` announces each scenario's files as they land - so the tree
fills in as the run goes rather than after it.

A scenario's folder opens on its step-by-step guide, when the run wrote one
(``--report-doc``): ``documentation.html``, which follows the run as it is
written. Export Documentation turns what is selected into files to hand on -
see :mod:`cms_gui.docexport`: one file for a session or a scenario, a .zip of
one per session for the whole run, as HTML or PDF.

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

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout, QLabel, QMenu,
                               QPlainTextEdit, QPushButton, QScrollArea, QSplitter,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import docexport, findbar, history as history_mod, icons, search, theme, widgets

TEXT_SUFFIXES = (".json", ".log", ".txt", ".html", ".yaml", ".yml", ".md", ".csv")
HTML_SUFFIXES = (".html", ".htm")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")
PREVIEW_LIMIT = 400_000     # bytes of a text file worth putting on screen
#: How long the files announced during a run gather before the tree is read
#: again. A guide is rewritten after every step it describes, and reading a run
#: of several hundred files for each of them kept the window busy.
RESCAN_GATHER_MS = 700

CURRENT = "Current run"
NO_RUNS = "no reports yet"
#: The guide a run writes into each scenario's folder (engine.livedoc).
GUIDE_NAME = "documentation.html"


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
        self.no_guide = None         # the empty state, made the first time it is needed
        self._guide_path = ""        # the guide on show, and when it last changed:
        self._guide_mtime = None     # a rescan reloads it only if it has
        self._rescanning = False
        self.exporter = None          # made the first time something is exported
        # Files announced by a run, read in one go: see note_artifacts.
        self._stale = False
        self._gather = QTimer(self)
        self._gather.setSingleShot(True)
        self._gather.setInterval(RESCAN_GATHER_MS)
        self._gather.timeout.connect(self._rescan_announced)
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
        self.preview_mode = widgets.capped(QComboBox())
        self.preview_mode.addItems(["Preview", "Source"])
        self.preview_mode.setToolTip("Show the HTML page or its source")
        self.preview_mode.setVisible(False)
        self.preview_mode.currentIndexChanged.connect(self._preview_selected)
        path_row.addWidget(self.preview_mode)
        # A scenario's folder, or its result.json: the run's guide, or the JSON
        # its result is in.
        self.guide_mode = widgets.capped(QComboBox())
        self.guide_mode.addItems(["Guide", "Source"])
        self.guide_mode.setToolTip("Show the run's step-by-step guide, or "
                                   "result.json as written")
        self.guide_mode.setVisible(False)
        self.guide_mode.currentIndexChanged.connect(self._preview_selected)
        path_row.addWidget(self.guide_mode)
        # What is selected, as files to hand on: one for a session or a
        # scenario, a .zip of one per session for the whole run. Offered
        # wherever there is a guide to export, whatever the pane shows.
        self.export_docs = icons.button(QPushButton("Export Documentation"), "export")
        self.export_docs.setProperty("hasmenu", "true")
        self.export_docs.setVisible(False)
        self.export_menu = QMenu(self.export_docs)
        for label, kind in (("HTML", docexport.HTML), ("PDF", docexport.PDF)):
            action = self.export_menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, k=kind: self.export_documentation(k))
        self.export_docs.setMenu(self.export_menu)
        path_row.addWidget(self.export_docs)
        path_row.addWidget(self.open_file)
        path_layout.addLayout(path_row)
        right_layout.addWidget(path_bar)
        # Ctrl+F over whatever the pane shows: a guide, a Jira task's reading
        # copy, any report - or the source, JSON or log as text.
        self.find_bar = findbar.FindBar()
        right_layout.addWidget(self.find_bar)
        findbar.install(self, lambda: (self.find_bar, self._find_target()))

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

    def reveal(self, run_dir, path=""):
        """Show ``run_dir`` with ``path`` selected in it. True when it was found.

        ``path`` is what a cycle records: relative to the run's own directory,
        because the directory describes itself and may have been copied from
        another machine. An absolute path is taken as it is. Nothing to select,
        or a file that has since gone, leaves the folder on show - which is
        still the right place to have arrived - and the path line says which.
        """
        if run_dir and os.path.normpath(run_dir) != os.path.normpath(
                self._shown_dir or os.sep):
            self.show_dir(run_dir)
        else:
            self.rescan()
        if not path:
            self.tree.clearSelection()
            return False
        wanted = os.path.normpath(path if os.path.isabs(path)
                                  else os.path.join(run_dir or self._shown_dir, path))
        if self._select_path(wanted):
            self.tree.scrollToItem(self.tree.currentItem())
            return True
        self.tree.clearSelection()
        self.path_label.setText("%s is no longer there" % wanted)
        return False

    # -- search (Ctrl+K) -------------------------------------------------------
    def search_rows(self):
        """Every run whose reports are still on disk, the live one first."""
        found = []
        if self._run_dir and os.path.isdir(self._run_dir):
            found.append(search.hit(CURRENT, self._run_dir, "Current run", "accent",
                                    target=self._run_dir))
        for entry in (self.history.with_artifacts() if self.history else []):
            if entry.get("run_dir") == self._run_dir:
                continue
            found.append(search.hit(history_mod.entry_label(entry), entry["run_dir"],
                                    keys=entry.get("summary") or "",
                                    target=entry["run_dir"]))
        return found

    def search_documents(self):
        """``(folder, label)`` of every run whose files Search reads: the ones
        the picker offers, and a folder opened by hand."""
        found = [(self._run_dir, CURRENT)] if self._run_dir else []
        for entry in (self.history.with_artifacts() if self.history else []):
            if entry.get("run_dir") != self._run_dir:
                found.append((entry["run_dir"], history_mod.entry_label(entry)))
        if self._shown_dir and self._shown_dir not in (path for path, _label in found):
            found.append((self._shown_dir, os.path.basename(self._shown_dir.rstrip(os.sep))))
        return found

    def show_found(self, target):
        """A run (its folder); or a document in one, with what was searched
        for ready in Find - lit in it, wherever it is."""
        if isinstance(target, tuple) and target and target[0] == "file":
            _kind, run_dir, path, words = target
            if not os.path.isdir(run_dir):
                return False
            if not self.reveal(run_dir, path):
                return False
            self.find_bar.field.setText(words)
            self.find_bar.open(self._find_target())
            return True
        run_dir = target
        if not run_dir or not os.path.isdir(run_dir):
            return False
        if os.path.normpath(run_dir) != os.path.normpath(self._shown_dir or os.sep):
            self.show_dir(run_dir)
        return True

    def note_artifacts(self, _event):
        """A run wrote files; the tree is read again - once for a burst of them.

        Announcements come after every scenario and after every step a guide
        describes, so they are gathered for a moment and read in one rescan.
        A page nobody is looking at only notes that it is behind, and catches
        up when it is shown.
        """
        self._stale = True
        if self.isVisible() and not self._gather.isActive():
            self._gather.start()

    def _rescan_announced(self):
        if self._stale and self.isVisible():
            self.rescan()

    def showEvent(self, event):              # noqa: N802 - Qt's own spelling
        QWidget.showEvent(self, event)
        if self._stale:
            self.rescan()

    def rescan(self):
        self._stale = False
        # Marked, so the preview can tell the same file chosen again by a rescan
        # from a reader choosing it: a guide being written follows the run.
        self._rescanning = True
        try:
            self._rescan()
        finally:
            self._rescanning = False

    def _rescan(self):
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
            # Normalised on both sides: the tree's paths are joined from the
            # shown directory as it was given, which may end in a separator.
            if os.path.normpath(item.data(0, Qt.UserRole) or "") == os.path.normpath(path):
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
            if not self._rescanning:
                self.html.stop()
            self.html.setVisible(False)
        if self.no_guide is not None:
            self.no_guide.setVisible(False)
        self._offer_export(path)
        if not self._rescanning:
            self._guide_path, self._guide_mtime = "", None
        folder = _scenario_folder(path)
        self.guide_mode.setVisible(bool(folder))
        if folder and self.guide_mode.currentIndex() == 0:
            self._show_guide(os.path.join(folder, GUIDE_NAME))
            return
        if folder and os.path.isdir(path):
            # Source, for the folder itself: the file its flow is read from.
            path, suffix = os.path.join(folder, "result.json"), ".json"
        if not path or os.path.isdir(path):
            self._show_text("" if not path else "Directory")
            return
        if is_html and self.preview_mode.currentIndex() == 0:
            if os.path.basename(path) == GUIDE_NAME:
                self._show_guide(path)
            else:
                self._show_html(path)
            return
        if suffix in IMAGE_SUFFIXES:
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                self.text.setVisible(False)
                self.image_area.setVisible(True)
                self.image.setPixmap(pixmap.scaled(
                    1200, 900, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                self._find_follows()
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

    def _show_guide(self, path):
        """The run's guide, or a word on how to get one.

        A rescan picks the selection again every time a scenario writes - while
        a run goes, after every step it describes. A guide that has not changed
        is left exactly as it is, scrolled where the reader left it; one that
        has is read again and scrolled to its newest step, so it follows the run
        the way a log pane follows its tail.
        """
        if not os.path.isfile(path):
            self._show_no_guide()
            return
        mtime = _mtime(path)
        again = self._rescanning and path == self._guide_path
        if again and mtime == self._guide_mtime and self.html is not None:
            self.text.setVisible(False)
            self.image_area.setVisible(False)
            self.html.setVisible(True)
            self._find_follows()
        else:
            self._show_html(path, fragment="latest" if again else "")
        self._guide_path, self._guide_mtime = path, mtime

    def _show_no_guide(self):
        if self.no_guide is None:
            # At the top of the pane, as History's empty state is: stretched over
            # all of it, the title and its hint drift a screen apart.
            self.no_guide = QWidget()
            column = QVBoxLayout(self.no_guide)
            column.setContentsMargins(16, 16, 16, 16)
            column.addWidget(widgets.empty_zone(
                "No guide for this run",
                "Tick \"Write a step-by-step guide while it runs\" under Launch, "
                "Reports and screenshots, and run the scenario again. Source shows "
                "its result.json."))
            column.addStretch(1)
            self._preview_layout.addWidget(self.no_guide, 1)
        self.text.setVisible(False)
        self.image_area.setVisible(False)
        self.no_guide.setVisible(True)
        self._find_follows()

    # -- exporting the documentation ------------------------------------------
    def _offer_export(self, path):
        """Show Export Documentation when what is selected has a guide in it."""
        plan = docexport.plan(path, self._shown_dir) if path else None
        self.export_docs.setVisible(plan is not None)
        if plan is not None:
            self.export_docs.setToolTip("Export the documentation here: %s."
                                        % docexport.scope(plan))

    def export_documentation(self, kind):
        """Export what is selected as ``kind`` (HTML or PDF), asking where to."""
        selected = self._selected_path()
        plan = docexport.plan(selected, self._shown_dir)
        if plan is None or (self.exporter is not None and self.exporter.busy()):
            return
        suffix = ".zip" if plan.archive else "." + kind
        folder = selected if os.path.isdir(selected) else os.path.dirname(selected)
        described = {docexport.HTML: "HTML", docexport.PDF: "PDF"}[kind]
        kinds = ("%s files in a zip archive (*.zip)" % described if plan.archive
                 else "%s (*%s)" % (described, suffix))
        target, _filter = QFileDialog.getSaveFileName(
            self, "Export the documentation as %s" % described,
            os.path.join(folder, docexport.safe_name(plan.name) + suffix), kinds)
        if not target:
            return
        if not target.lower().endswith(suffix):
            target += suffix
        if self.exporter is None:
            self.exporter = docexport.DocExporter(self)
            self.exporter.progress.connect(self._export_progress)
            self.exporter.finished.connect(self._exported)
        self.export_docs.setEnabled(False)
        self.path_label.setText("Writing %s ..." % target)
        self.exporter.start(plan, kind, target)

    def _export_progress(self, done, total):
        if total > 1:
            self.path_label.setText("Writing %d of %d ..." % (min(done + 1, total), total))

    def _exported(self, path, ok, problem):
        self.export_docs.setEnabled(True)
        if ok:
            self.rescan()            # it may have landed in the folder on show
            self.path_label.setText("Saved %s" % path)
        else:
            self.path_label.setText("Could not write %s%s"
                                    % (path, ": %s" % problem if problem else ""))

    def _show_text(self, body):
        if self.html is not None:
            self.html.setVisible(False)
        self.image_area.setVisible(False)
        self.text.setVisible(True)
        self.text.setPlainText(body)
        self._find_follows()

    # -- finding in what is on show -------------------------------------------
    def _find_target(self):
        """The reader on show, as something to search; None for a picture."""
        if self.html is not None and not self.html.isHidden():
            return findbar.target_for(self.html)
        if not self.text.isHidden() and (self.no_guide is None
                                         or self.no_guide.isHidden()):
            return findbar.target_for(self.text)
        return None

    def _find_follows(self):
        """An open search moves to the reader now on show."""
        self.find_bar.retarget(self._find_target())

    def _show_html(self, path, fragment=""):
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
        self._find_follows()
        self.html.load_file(path, self._shown_dir, fragment)

    # -- opening --------------------------------------------------------------
    def _open_selected(self):
        path = self._selected_path()
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _open_run_dir(self):
        if self._shown_dir:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._shown_dir))


def _scenario_folder(path):
    """The scenario folder ``path`` is, or whose result.json it is - or ""."""
    if not path:
        return ""
    if os.path.isdir(path):
        return path if os.path.isfile(os.path.join(path, "result.json")) else ""
    return os.path.dirname(path) if os.path.basename(path) == "result.json" else ""


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
