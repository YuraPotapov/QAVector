"""Offline license viewer. Reads shipped text only, independently of the core."""

import json
from pathlib import Path, PurePosixPath
import sys

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QLabel,
                               QListWidget, QPlainTextEdit, QPushButton,
                               QVBoxLayout, QWidget)

from . import version, widgets


def licenses_root():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "licenses"
    return Path(__file__).resolve().parents[2] / "licenses"


class ThirdPartyLicensesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Third-Party Licenses")
        self.frame = widgets.dress(self)
        self.resize(900, 620)
        self.root = licenses_root()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.addWidget(widgets.heading("Third-Party Licenses", "h2"))
        layout.addWidget(widgets.lede(
            "Offline notices for the Linux execution runtime. Each component retains its own license and authorship."))
        self.components = QListWidget()
        self.components.setMaximumWidth(240)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.documents = QComboBox()
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        detail = QWidget()
        column = QVBoxLayout(detail)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(self.summary)
        column.addWidget(self.documents)
        column.addWidget(self.text, 1)
        layout.addWidget(widgets.row(self.components, detail), 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.entries = []
        try:
            catalog = json.loads((self.root / "components.json").read_text(encoding="utf-8"))
            self.entries = catalog["components"]
            if catalog["schema_version"] != 1 or not self.entries:
                raise ValueError("Invalid license catalog")
            for entry in self.entries:
                self.components.addItem("%s\n%s · %s" % (entry["name"], entry["version"], entry["license"]))
        except (OSError, ValueError, KeyError, TypeError):
            self.text.setPlainText("Required third-party license materials are missing or invalid. Reinstall a complete QAVector distribution.")
            return
        self.components.currentRowChanged.connect(self._component_changed)
        self.documents.currentIndexChanged.connect(self._document_changed)
        self.components.setCurrentRow(0)

    def _component_changed(self, index):
        if not 0 <= index < len(self.entries):
            return
        entry = self.entries[index]
        self.summary.setText("%s %s · %s\n%s" % (entry["name"], entry["version"], entry["license"], entry.get("scope", "")))
        self.documents.clear()
        for name in entry["documents"]:
            label = {"NOTICE.md": "Notice", "AUTHORS": "Authors and copyright notices",
                     "COPYING": "Full license", "COPYRIGHT": "Copyright and license"}.get(PurePosixPath(name).name, name)
            self.documents.addItem(label, name)

    def _document_changed(self, _index):
        name = self.documents.currentData()
        if not name:
            return
        try:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name:
                raise ValueError("Invalid license path")
            self.text.setPlainText((self.root / name).read_text(encoding="utf-8"))
            self.text.moveCursor(QTextCursor.MoveOperation.Start)
        except (OSError, ValueError):
            self.text.setPlainText("Required license document is missing: " + name)


class AboutDialog(QDialog):
    def __init__(self, parent=None, description=""):
        super().__init__(parent)
        self.setWindowTitle("About QAVector")
        self.frame = widgets.dress(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.addWidget(widgets.heading("QAVector " + version(), "h2"))
        if description:
            layout.addWidget(widgets.lede(description))
        layout.addWidget(widgets.lede("Third-party components retain their own licenses and copyright notices."))
        self.licenses_button = QPushButton("Third-Party Licenses")
        self.licenses_button.clicked.connect(self._licenses)
        layout.addWidget(self.licenses_button)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _licenses(self):
        ThirdPartyLicensesDialog(self).exec()
