"""Settings dialog: where the core is, and which interpreter runs it.

This is the whole configuration surface of the GUI. Everything else it knows
comes from the core itself, which is why the dialog's own feedback is simply
"can I run --describe against this, and what did it say?".

**The form scrolls.** Ten paths, each with a note under it saying what happens
when it is left blank, is taller than a laptop screen - and a dialog cannot
grow past the screen, so Qt squeezed the notes instead, below the height their
wrapped text needs, until each ran into the field under it and the last of them
was cut off with no way to reach it. The body scrolls and the buttons stay
outside it, the way ``logsources.RowDialog`` does, so Save is always reachable.
"""

import os
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QLabel,
                               QLineEdit, QPushButton, QScrollArea, QVBoxLayout,
                               QWidget)

from .. import core as core_mod
from .. import logsourcesfile as lsf
from .. import cycleprojectsfile as cpf
from .. import servicesfile as sf
from .. import theme, widgets


#: The core's own name for the store, repeated rather than imported: the GUI
#: depends on PySide6 and nothing else, and this is only ever a placeholder and
#: a file dialog's suggestion. The core resolves the real path itself when the
#: box is left empty.
SECRETS_NAME = "cyclesecrets.json"
MEMORY_NAME = "cyclememory.json"

#: What the footer and the window's own chrome take, so the first size asked
#: for is the form's height plus the things around it rather than the form's
#: alone. Measured rather than derived: the footer is not laid out yet when the
#: dialog first sizes itself.
FOOTER_HEIGHT = 130


def _default_secrets_path():
    """Where the core will put the store when nobody has said otherwise."""
    return os.path.join(os.path.dirname(sf.default_path()), SECRETS_NAME)


def _default_memory_path():
    return os.path.join(os.path.dirname(sf.default_path()), MEMORY_NAME)


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Settings")
        self.frame = widgets.dress(self)
        self.setMinimumWidth(620)
        self._settled = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._body = QWidget()
        column = QVBoxLayout(self._body)
        column.setContentsMargins(24, 20, 24, 18)
        column.setSpacing(6)
        self.column = column
        self._scroll = QScrollArea()
        self._scroll.setWidget(self._body)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.NoFrame)
        # Never sideways: a hint is word-wrapped, so a horizontal bar would
        # only ever mean the form had been made too narrow to read.
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(self._scroll, 1)
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

        # Wanted for a sharper reason than the scenarios one. In a source
        # checkout the core's default is the checkout, so the cycles somebody
        # edits are the template files that ship with the application - and
        # saving one writes their own Jira instance and work email into a file
        # that is committed.
        self.cycles = QLineEdit(settings.cycles_path)
        self.cycles.setProperty("mono", True)
        self.cycles.setPlaceholderText("the core's own cycles directory")
        column.addWidget(widgets.field(
            "Cycles", widgets.row(self.cycles,
                                  self._browse_button(self._pick_cycles)),
            "The folder your cycles are read from and written to. Blank uses "
            "the core's default, which in a source checkout is the checkout "
            "itself - so editing a cycle there changes a file that ships with "
            "the application. Passed to every command as --cycles-dir."))

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

        # Its own file rather than a corner of the one above: a project on the
        # Cycles page can have half a dozen cycles and no services at all.
        self.cycle_projects = QLineEdit(settings.cycle_projects_path)
        self.cycle_projects.setProperty("mono", True)
        self.cycle_projects.setPlaceholderText(cpf.default_path())
        column.addWidget(widgets.field(
            "Cycle projects",
            widgets.row(self.cycle_projects,
                        self._browse_button(self._pick_cycle_projects)),
            "The projects the Cycles page groups by. Separate from the services "
            "above on purpose - a project can have cycles and no services. "
            "Which cycles belong to one is written in each cycle, not here."))

        # The values of secret variables, kept out of the cycle files because
        # those are committed and shipped inside the build.
        self.cycle_secrets = QLineEdit(settings.cycle_secrets_path)
        self.cycle_secrets.setProperty("mono", True)
        self.cycle_secrets.setPlaceholderText(_default_secrets_path())
        column.addWidget(widgets.field(
            "Cycle secrets",
            widgets.row(self.cycle_secrets,
                        self._browse_button(self._pick_cycle_secrets)),
            "The values of variables a cycle marks secret, encrypted, with the "
            "key beside them. Never inside a cycle file: those are committed "
            "and shipped, so anything written there travels to everyone."))

        # What the project remembers between runs: which tasks were done, how
        # much budget each has used, who is working on one right now.
        self.cycle_memory = QLineEdit(settings.cycle_memory_path)
        self.cycle_memory.setProperty("mono", True)
        self.cycle_memory.setPlaceholderText(_default_memory_path())
        column.addWidget(widgets.field(
            "Cycle memory",
            widgets.row(self.cycle_memory,
                        self._browse_button(self._pick_cycle_memory)),
            "What cycles remember between runs, so a task already done is not "
            "done again. Plain JSON on purpose - it is meant to be readable "
            "when you want to know why a run skipped something."))

        column.addStretch(1)

        # Outside the scroll: testing the connection and saving are what the
        # dialog is for, and a form long enough to scroll is exactly the one
        # whose Save would otherwise be below the bottom edge.
        footer = QWidget()
        bottom = QVBoxLayout(footer)
        bottom.setContentsMargins(24, 8, 24, 14)
        bottom.setSpacing(8)
        test = QPushButton("Test connection")
        test.clicked.connect(self._test)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        bottom.addWidget(widgets.row(test, None))
        bottom.addWidget(self.result)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.Save).setProperty("variant", "primary")
        bottom.addWidget(buttons)
        outer.addWidget(footer)
        self._fit()

    def _fit(self):
        """Open at the height the form actually needs.

        A dialog is given whatever height its first layout pass settles on, and
        anything past that is silently clipped - which with this many fields is
        the last one's hint, then the last field, then Save. Capped at the
        screen: a form taller than the display now scrolls rather than being
        squeezed, which is what the cap used to cost.
        """
        screen = self.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry().height() if screen else 900
        wanted = self._body.sizeHint().height() + FOOTER_HEIGHT
        self.resize(max(620, self.width()), min(wanted, int(available * 0.9)))

    def showEvent(self, event):
        """Take back whatever the form did not need, once it has real geometry.

        Every hint under a field is a word-wrapped QLabel, and one of those
        reports a sizeHint for a width it has not been given - always more lines
        than it will actually take. ``_fit`` can only add those up, so without
        this the dialog opens with a band of nothing under its last field.
        """
        super().showEvent(event)
        if not self._settled:
            self._settled = True
            QTimer.singleShot(0, self._settle)

    def _settle(self):
        """Shrink to what is actually laid out. Never grows, never scrolls away."""
        content = 0
        for index in range(self.column.count()):
            widget = self.column.itemAt(index).widget()
            if widget is not None and widget.isVisible():
                content = max(content, widget.geometry().bottom() + 1)
        if content <= 0:
            return
        slack = self._body.height() - (content
                                       + self.column.contentsMargins().bottom())
        # At the screen cap the body is already scrolling and there is no slack
        # to take; a couple of pixels is rounding, not a band.
        if slack > 2:
            self.resize(self.width(), self.height() - slack)

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

    def _pick_cycle_projects(self):
        path = widgets.pick_path(self, cpf.FILE_NAME,
                                 self.cycle_projects.text() or cpf.default_path(),
                                 save=True)
        if path:
            self.cycle_projects.setText(path)

    def _pick_cycle_secrets(self):
        path = widgets.pick_path(self, SECRETS_NAME,
                                 self.cycle_secrets.text() or _default_secrets_path(),
                                 save=True)
        if path:
            self.cycle_secrets.setText(path)

    def _pick_cycle_memory(self):
        path = widgets.pick_path(self, MEMORY_NAME,
                                 self.cycle_memory.text() or _default_memory_path(),
                                 save=True)
        if path:
            self.cycle_memory.setText(path)

    def _pick_cycles(self):
        path = widgets.pick_path(self, "Cycles folder",
                                 self.cycles.text() or "~", directory=True)
        if path:
            self.cycles.setText(path)

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
                             self.flows.text().strip(),
                             cycles_dir=self.cycles.text().strip())

    def apply(self):
        self.settings.core_script = self.script.text().strip()
        self.settings.interpreter = self.interpreter.text().strip()
        self.settings.config = self.config.text().strip()
        self.settings.services_path = self.services.text().strip()
        self.settings.cycle_projects_path = self.cycle_projects.text().strip()
        self.settings.cycle_secrets_path = self.cycle_secrets.text().strip()
        self.settings.cycle_memory_path = self.cycle_memory.text().strip()
        self.settings.log_sources_path = self.log_sources.text().strip()
        self.settings.flows_path = self.flows.text().strip()
        self.settings.cycles_path = self.cycles.text().strip()
