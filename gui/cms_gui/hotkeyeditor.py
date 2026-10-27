"""Draft hotkey settings, with keyboard capture instead of text entry."""

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QHeaderView,
                               QLabel, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)
from shiboken6 import isValid

from . import hotkeys, widgets


UNASSIGNED = "Not assigned"
RECORDING = "Press a key combination"


class _Capture(QObject):
    """Own all keyboard events until a chord is released or capture cancels.

    Accepting ShortcutOverride prevents Qt's shortcut map from dispatching a
    key before KeyPress arrives. Keeping the filter through KeyRelease also
    prevents a held key's repeat from escaping after the first assignment.
    """

    captured = Signal(str)
    cancelled = Signal()

    def __init__(self, target, registry):
        super().__init__(target)
        self.target = target
        self._focus_targets = (target, target.viewport())
        self.registry = registry
        self.active = False
        self.pending = None
        self._watched = ()
        target.destroyed.connect(self._destroyed)

    def begin(self):
        self.cancel()
        self.target.setFocus(Qt.OtherFocusReason)
        self.pending = None
        self.active = True
        self.registry.suspend()
        # The focused table accepts ShortcutOverride before Qt dispatches any
        # QAction or QShortcut. Filter only these known widgets: WebEngine's
        # private receivers must never enter an application-wide Python filter.
        self._watched = tuple(dict.fromkeys((*self._focus_targets, self.target.window())))
        for widget in self._watched:
            widget.installEventFilter(self)

    def _stop(self):
        if not self.active:
            return False
        self.active = False
        for widget in self._watched:
            if isValid(widget):
                widget.removeEventFilter(self)
        self._watched = ()
        self.registry.resume()
        self.pending = None
        return True

    def cancel(self):
        if self._stop():
            self.cancelled.emit()

    def _destroyed(self):
        self._stop()

    def eventFilter(self, watched, event):                  # noqa: N802
        if not self.active:
            return False
        kind = event.type()
        if kind in (QEvent.ShortcutOverride, QEvent.Shortcut):
            event.accept()
            return True
        if kind == QEvent.KeyPress:
            event.accept()
            if event.isAutoRepeat():
                return True
            if event.key() == Qt.Key_Escape:
                self.cancel()
                return True
            if self.pending is None:
                sequence = hotkeys.sequence_from_event(event)
                if sequence:
                    self.pending = sequence
            return True
        if kind == QEvent.KeyRelease:
            event.accept()
            # The chord ends when its key comes up, whatever it reads as by
            # then: with Shift let go first, the "!" pressed comes up as "1".
            if (not event.isAutoRepeat() and self.pending is not None
                    and event.key() not in hotkeys.MODIFIER_KEYS):
                sequence = self.pending
                self._stop()
                self.captured.emit(sequence)
            return True
        if ((kind == QEvent.FocusOut
             and any(watched is widget for widget in self._focus_targets))
                or (kind in (QEvent.Hide, QEvent.WindowDeactivate)
                    and watched is self.target.window())):
            self.cancel()
        return False


class HotkeyEditor(QWidget):
    """One registry-backed table and a private draft until Apply succeeds."""

    changed = Signal()

    def __init__(self, registry, parent=None):
        super().__init__(parent)
        self.registry = registry
        self.draft = dict(registry.bindings())
        self._baseline = dict(self.draft)
        self._recording_id = None
        self._specs = registry.specs()
        column = QVBoxLayout(self)
        column.setContentsMargins(20, 16, 20, 16)
        column.setSpacing(10)
        column.addWidget(widgets.heading("Hotkeys", "h2"))
        self.hint = QLabel("Select an action and click Change, or double-click its shortcut.")
        self.hint.setWordWrap(True)
        column.addWidget(self.hint)
        self.table = QTableWidget(len(self._specs), 2)
        self.table.setHorizontalHeaderLabels(["Action", "Shortcut"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.setMinimumHeight(210)
        self._populate_table()
        self.table.cellDoubleClicked.connect(self._double_clicked)
        self.table.currentCellChanged.connect(lambda *_args: self._selection_changed())
        column.addWidget(self.table, 1)

        self.edit_button = QPushButton("Change")
        self.edit_button.clicked.connect(self.start_capture)
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.clear_selected)
        self.reset_button = QPushButton("Restore selected default")
        self.reset_button.clicked.connect(self.reset_selected)
        self.reset_all_button = QPushButton("Restore all defaults")
        self.reset_all_button.clicked.connect(self.reset_all)
        # Two short rows continue to fit on the settings dialog's laptop width.
        column.addWidget(widgets.row(self.edit_button, self.clear_button,
                                     self.reset_button, None))
        column.addWidget(widgets.row(self.reset_all_button, None))
        for button in (self.edit_button, self.clear_button, self.reset_button,
                       self.reset_all_button):
            button.setAutoDefault(False)
        self.capture = _Capture(self.table, registry)
        self.capture.captured.connect(self._captured)
        self.capture.cancelled.connect(self._cancelled)
        if self._specs:
            self.table.setCurrentCell(0, 1)
        self._refresh()
        self._selection_changed()
        registry.registered.connect(self._registered)

    def _populate_table(self):
        self.table.setRowCount(len(self._specs))
        for row, spec in enumerate(self._specs):
            item = QTableWidgetItem(spec.name)
            item.setData(Qt.UserRole, spec.id)
            item.setToolTip("\n".join(part for part in (
                spec.description, hotkeys.scope_label(spec.scope)) if part))
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, 0, item)
            value = QTableWidgetItem()
            value.setFlags(value.flags() & ~Qt.ItemIsEditable)
            value.setToolTip(item.toolTip())
            self.table.setItem(row, 1, value)

    def _select_action(self, action_id):
        for row, spec in enumerate(self._specs):
            if spec.id == action_id:
                self.table.setCurrentCell(row, 1)
                return
        if self._specs:
            self.table.setCurrentCell(0, 1)

    def _registered(self):
        """Late plugin actions join the table without losing pending edits."""
        selected = self._selected_id()
        self.capture.cancel()
        values = self.registry.bindings()
        # Registration can resolve a newly loaded plugin's saved binding over
        # an existing default. Untouched rows follow that live state; edits
        # remain in the draft and go through the usual conflict validation.
        self.draft = {action_id: (self.draft[action_id]
                                 if action_id in self._baseline
                                 and self.draft.get(action_id) != self._baseline[action_id]
                                 else value)
                      for action_id, value in values.items()}
        self._baseline = dict(values)
        self._specs = self.registry.specs()
        blocked = self.table.blockSignals(True)
        try:
            self._populate_table()
            self._select_action(selected)
        finally:
            self.table.blockSignals(blocked)
        self._refresh()
        self._selection_changed()
        self.changed.emit()

    @property
    def dirty(self):
        return self.draft != self._baseline

    def _selected_id(self):
        row = self.table.currentRow()
        return self._specs[row].id if 0 <= row < len(self._specs) else None

    def _selection_changed(self):
        if self.capture.active and self._selected_id() != self._recording_id:
            self.capture.cancel()
        selected = self._selected_id() is not None
        for button in (self.edit_button, self.clear_button, self.reset_button):
            button.setEnabled(selected)

    def _double_clicked(self, row, column):
        if column == 1:
            self.start_capture()

    def start_capture(self):
        action_id = self._selected_id()
        if action_id is None:
            return
        self.capture.cancel()
        self._recording_id = action_id
        self.capture.begin()
        self.hint.setText(RECORDING + " · Esc — cancel")
        self.table.item(self.table.currentRow(), 1).setText(RECORDING)

    def cancel_capture(self):
        self.capture.cancel()

    def _cancelled(self):
        self._recording_id = None
        self._refresh()

    def _captured(self, sequence):
        action_id = self._recording_id
        self._recording_id = None
        if action_id is not None:
            self.propose(action_id, sequence)
        if not self.capture.active:
            self._refresh()

    def propose(self, action_id, sequence):
        """Check against the whole draft before accepting a captured chord."""
        conflicts = self.registry.conflicts(action_id, sequence, self.draft)
        if conflicts:
            answer = widgets.choose(
                self, "Shortcut conflict",
                "This combination is already used by: %s." %
                ", ".join(spec.name for spec in conflicts),
                "Reassigning clears the previous shortcuts in the draft.",
                choices=("Another combination",), agree="Reassign",
                refuse="Cancel", kind="warn")
            if answer == "Another combination":
                self._select_action(action_id)
                self.start_capture()
                return False
            if answer != "Reassign":
                return False
        candidate = dict(self.draft)
        for spec in conflicts:
            candidate[spec.id] = ""
        candidate[action_id] = sequence
        problems = self.registry.validate(candidate)
        if problems:
            widgets.warn(self, "Cannot assign shortcut", "\n".join(problems))
            return False
        self.draft = candidate
        self._refresh()
        self.changed.emit()
        return True

    def clear_selected(self):
        self.capture.cancel()
        action_id = self._selected_id()
        if action_id is not None:
            self.propose(action_id, "")

    def reset_selected(self):
        self.capture.cancel()
        action_id = self._selected_id()
        if action_id is not None:
            self.propose(action_id, self.registry.defaults().get(action_id, ""))

    def reset_all(self):
        self.capture.cancel()
        candidate = self.registry.defaults()
        problems = self.registry.validate(candidate)
        if problems:
            widgets.warn(self, "Default shortcut conflict", "\n".join(problems))
            return
        self.draft = dict(candidate)
        self._refresh()
        self.changed.emit()

    def apply(self):
        self.capture.cancel()
        if not self.dirty:
            return True
        try:
            self.registry.apply(self.draft)
        except (OSError, ValueError) as exc:
            widgets.warn(self, "Cannot save hotkeys", str(exc))
            return False
        self._baseline = dict(self.draft)
        self.changed.emit()
        return True

    def discard(self):
        self.capture.cancel()
        self.draft = dict(self._baseline)
        self._refresh()
        self.changed.emit()

    def _refresh(self):
        for row, spec in enumerate(self._specs):
            sequence = QKeySequence(self.draft.get(spec.id, ""), QKeySequence.PortableText)
            self.table.item(row, 1).setText(sequence.toString(QKeySequence.NativeText)
                                          or UNASSIGNED)
        self.hint.setText("Select an action and click Change, or double-click its shortcut.")

    def hideEvent(self, event):                            # noqa: N802
        self.capture.cancel()
        super().hideEvent(event)
