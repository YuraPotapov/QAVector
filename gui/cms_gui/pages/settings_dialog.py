"""Settings dialog: where the core is, and which interpreter runs it.

This is the whole configuration surface of the GUI. Everything else it knows
comes from the core itself, which is why the dialog's own feedback is simply
"can I run --describe against this, and what did it say?".
"""

import os
import sys

from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QLabel,
                               QLineEdit, QPushButton, QVBoxLayout)

from .. import core as core_mod
from .. import logsourcesfile as lsf
from .. import servicesfile as sf
from .. import theme, widgets


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Settings")
        self.setMinimumWidth(620)

        column = QVBoxLayout(self)
        column.setContentsMargins(24, 20, 24, 18)
        column.setSpacing(6)
        developer = bool(settings.developer_mode)
        column.addWidget(widgets.heading("Settings", "h2"))
        column.addWidget(widgets.lede(
            "Stored by the GUI. It never imports the core - it spawns the launcher "
            "through this interpreter, so the two environments stay independent."
            if developer else
            "Where QAVector finds its accounts, scenarios, services and log sources."))
        column.addSpacing(12)

        detected_script, detected_python = core_mod.autodetect()
        current_script = settings.core_script or detected_script or ""
        self.script = QLineEdit(current_script)
        self.script.setProperty("mono", True)
        self.script.setPlaceholderText("path to session_launcher.py")
        script_field = widgets.field("Core script",
                                     widgets.row(self.script,
                                                 self._browse_button(self._pick_script)))
        column.addWidget(script_field)

        # An installed build's core is an executable, so there is no interpreter to
        # choose - leave the field empty and say why rather than offering a Python
        # that could not run it anyway.
        packaged = not core_mod.needs_interpreter(current_script)
        self.interpreter = QLineEdit(
            "" if packaged else (settings.interpreter or detected_python or sys.executable))
        self.interpreter.setProperty("mono", True)
        self.interpreter.setPlaceholderText(
            "not needed: the core is a packaged executable" if packaged
            else "python that has playwright + cryptography")
        self.interpreter_browse = self._browse_button(self._pick_interpreter)
        self.interpreter.setEnabled(not packaged)
        self.interpreter_browse.setEnabled(not packaged)
        interpreter_field = widgets.field(
            "Interpreter", widgets.row(self.interpreter, self.interpreter_browse),
            "Only used for a session_launcher.py; a packaged core runs itself. "
            "The core's own .venv is detected automatically when gui/ sits inside "
            "the core checkout.")
        column.addWidget(interpreter_field)

        # Which core runs, and through what, is a developer's question - an
        # installed build finds its own. Hidden rather than dropped, though: a
        # saved path wins over detection (core.Core), so a build pointed at a
        # checkout once would otherwise have no way back from the UI.
        self.core_fields = (script_field, interpreter_field)
        for box in self.core_fields:
            box.setVisible(developer)

        self.config = QLineEdit(settings.config)
        self.config.setProperty("mono", True)
        self.config.setPlaceholderText("users.json (the core's default)")
        column.addWidget(widgets.field(
            "Config (--config)", widgets.row(self.config,
                                             self._browse_button(self._pick_config)),
            "Passed to every command, so the GUI and the CLI always read the same file."))

        # "Which file am I actually editing" is a fair question, and this is
        # where the other answers about where things live already are.
        column.addSpacing(10)
        self.log_sources = QLineEdit(settings.log_sources_path)
        self.log_sources.setProperty("mono", True)
        self.log_sources.setPlaceholderText(lsf.default_path())
        column.addWidget(widgets.field(
            "Log sources", widgets.row(self.log_sources,
                                       self._browse_button(self._pick_log_sources)),
            "The connections and backend logs, edited on the Services & Logs page. "
            "Blank uses the path shown, under your own directory. Passed to every "
            "command as --log-sources, so the file edited here and the file a run "
            "reads are the same one."))
        self.data_paths = widgets.elided_mono("")

        # A directory, not a file - and the one setting here whose default is
        # actively wrong in a checkout, where the core's own answer is the
        # checkout itself and every scenario saved lands among the source.
        self.flows = QLineEdit(settings.flows_path)
        self.flows.setProperty("mono", True)
        self.flows.setPlaceholderText("the core's own flows directory")
        column.addWidget(widgets.field(
            "Scenarios", widgets.row(self.flows,
                                     self._browse_button(self._pick_flows)),
            "The folder your scenarios are read from and written to. Blank uses "
            "the core's default, which in a source checkout is the checkout "
            "itself. Set, it is the ONLY folder used - the blocks and "
            "selectors.yaml your scenarios reference have to be in it too, so "
            "copy them across before pointing this at an empty one. Passed to "
            "every command as --flows-dir."))

        # The GUI's own, and the launcher has never heard of it - so where this
        # one goes really is nobody else's business.
        self.services = QLineEdit(settings.services_path)
        self.services.setProperty("mono", True)
        self.services.setPlaceholderText(sf.default_path())
        column.addWidget(widgets.field(
            "Services", widgets.row(self.services,
                                    self._browse_button(self._pick_services)),
            "The projects and their services. Blank uses the path shown, under "
            "your own directory - never inside a checkout. The launcher neither "
            "reads this file nor needs to."))

        column.addSpacing(10)
        test = QPushButton("Test connection")
        test.clicked.connect(self._test)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        column.addWidget(widgets.row(test, None))
        column.addWidget(self.result)

        column.addSpacing(10)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.Save).setProperty("variant", "primary")
        column.addWidget(buttons)
        self._fit()

    def _fit(self):
        """Open at the height the form actually needs.

        A dialog is given whatever height its first layout pass settles on, and
        anything past that is silently clipped - which with this many fields is
        the last one's hint, then the last field, then Save. Capped at the
        screen, because a form taller than the display cannot be completed.
        """
        self.adjustSize()
        screen = self.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry().height() if screen else 900
        self.resize(max(620, self.width()),
                    min(self.sizeHint().height() + 8, int(available * 0.9)))

    def _browse_button(self, slot):
        button = QPushButton("Browse…")
        button.clicked.connect(slot)
        return button

    def _pick_script(self):
        path = widgets.pick_path(self, "session_launcher.py",
                                 os.path.dirname(self.script.text() or "") or "~")
        if path:
            self.script.setText(path)
            guess = core_mod._venv_python(os.path.dirname(path))
            if guess and not self.interpreter.text():
                self.interpreter.setText(guess)

    def _pick_interpreter(self):
        # A chooser does not list dotted directories, but it does show what is
        # inside one it opens in - and the answer here is usually .venv/bin.
        path = widgets.pick_path(self, "Python interpreter",
                                 self.interpreter.text() or "~")
        if path:
            self.interpreter.setText(path)

    def _pick_log_sources(self):
        path = widgets.pick_path(self, "logsources.json",
                                 self.log_sources.text() or lsf.default_path())
        if path:
            self.log_sources.setText(path)

    def _pick_services(self):
        path = widgets.pick_path(self, "services.json",
                                 self.services.text() or sf.default_path(),
                                 save=True)
        if path:
            self.services.setText(path)

    def _pick_flows(self):
        path = widgets.pick_path(self, "Scenarios folder",
                                 self.flows.text() or "~", directory=True)
        if path:
            self.flows.setText(path)

    def _pick_config(self):
        path = widgets.pick_path(self, "users.json",
                                 os.path.dirname(self.config.text() or "") or "~")
        if path:
            self.config.setText(path)

    def _test(self):
        core = self.core()
        try:
            payload = core.describe()
        except core_mod.CoreError as exc:
            self.result.setText(str(exc))
            self.result.setStyleSheet("color: %s; font-size: 12px;" % theme.BAD)
            return
        inventory = core_mod.Inventory(payload)
        self.result.setText("core %s · %d environments · %d users · %d scenarios%s"
                            % (inventory.version, len(inventory.envs),
                               len(inventory.users), len(inventory.scenarios),
                               "\n" + "\n".join(inventory.warnings)
                               if inventory.warnings else ""))
        self.result.setStyleSheet("color: %s; font-size: 12px;" % theme.ACCENT_RAMP[700])

    def set_paths(self, log_sources, _services=None):
        """What is actually in force, for the field that is left blank.

        The placeholder says what blank will really use. Until the setting is
        given a value that is the file the core reported reading, which on an
        upgrade is still the old location - so the field says where the rows on
        screen came from rather than where they would go.
        """
        self.data_paths.setText(log_sources or "")
        if log_sources and not self.log_sources.text().strip():
            self.log_sources.setPlaceholderText(log_sources)

    def set_flows_dir(self, path):
        """The tree the core says it is using, for the field that is left blank.

        The same courtesy as the log sources placeholder above, and the more
        useful one: "where do my scenarios go right now" is the question this
        field exists to answer, and blank is not an answer.
        """
        if path and not self.flows.text().strip():
            self.flows.setPlaceholderText(path)

    def core(self):
        return core_mod.Core(self.script.text().strip(),
                             self.interpreter.text().strip(),
                             self.config.text().strip(),
                             self.log_sources.text().strip(),
                             self.flows.text().strip())

    def apply(self):
        self.settings.core_script = self.script.text().strip()
        self.settings.interpreter = self.interpreter.text().strip()
        self.settings.config = self.config.text().strip()
        self.settings.services_path = self.services.text().strip()
        self.settings.log_sources_path = self.log_sources.text().strip()
        self.settings.flows_path = self.flows.text().strip()
