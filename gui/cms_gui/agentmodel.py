"""A model chosen from the connected CLI's own catalogue, never guessed."""

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QComboBox
from shiboken6 import isValid


class ModelPicker(QComboBox):
    # The owner fills the list asynchronously by calling done(payload).
    requested = Signal(object)
    CACHE_SECONDS = 60

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(False)
        self.setAccessibleName("Model")
        self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(16)
        self.setMaxVisibleItems(12)
        self._context = None
        self._inherit = "Use Claude Code settings"
        self._inherit_tip = "Leave the model choice to the program's own settings."
        self._models = []
        self._loaded_at = 0
        self._loading = False
        self._serial = 0
        self._error = ""
        self._resize_popup = QTimer(self)
        self._resize_popup.setSingleShot(True)
        self._resize_popup.timeout.connect(
            lambda: QComboBox.showPopup(self) if self.isVisible() else None)
        self._fill("")
        self.currentIndexChanged.connect(self._tip)

    def value(self):
        return self.currentData() or ""

    def set_value(self, value):
        value = str(value or "")
        if self.findData(value) < 0:
            self.addItem(value + " (saved)", value)
        self.setCurrentIndex(self.findData(value))

    def set_context(self, context, inherited="Use Claude Code settings", tip=None):
        """Drop stale results when the selected program/connection changes."""
        value = self.value()
        if context != self._context:
            self._resize_popup.stop()
            self._serial += 1
            self._context = context
            self._models = []
            self._loading = False
            self._loaded_at = 0
            self._error = ""
        self._inherit = inherited
        if tip is not None:
            self._inherit_tip = tip
        self._fill(value)

    def _fill(self, value):
        self.blockSignals(True)
        self.clear()
        self.addItem(self._inherit, "")
        self.setItemData(0, self._inherit_tip, Qt.ToolTipRole)
        for model in self._models:
            self.addItem(model["label"], model["value"])
            tip = "\n".join(part for part in (model.get("description"),
                                             model.get("resolved_model")) if part)
            self.setItemData(self.count() - 1, tip, Qt.ToolTipRole)
        self.set_value(value)
        if self._loading or self._error:
            self.addItem("Loading models…" if self._loading else "Models unavailable — reopen to retry", None)
            item = self.model().item(self.count() - 1)
            item.setEnabled(False)
            item.setToolTip(self._error)
        self.blockSignals(False)
        self._tip()

    def _tip(self, *_args):
        self.setToolTip(self._error or self.currentData(Qt.ToolTipRole) or self.currentText())

    def showPopup(self):
        if not self._loading and (not self._loaded_at
                                  or time.monotonic() - self._loaded_at >= self.CACHE_SECONDS):
            self._load()
        super().showPopup()

    def hidePopup(self):
        self._resize_popup.stop()
        super().hidePopup()

    def _load(self):
        self._serial += 1
        serial = self._serial
        self._loading = True
        self._error = ""
        self._fill(self.value())

        def done(payload):
            if not isValid(self) or serial != self._serial:
                return
            value = self.value()         # a choice made while loading still wins
            was_open = self.view().isVisible()
            self._loading = False
            if payload.get("ok"):
                self._models = list(payload.get("models") or [])
                self._loaded_at = time.monotonic()
                self._error = ""
            else:
                self._error = payload.get("error") or "The model list could not be read."
            self._fill(value)
            if was_open:
                # The popup's geometry was computed before the response arrived.
                self.hidePopup()
                self._resize_popup.start(0)

        self.requested.emit(done)
