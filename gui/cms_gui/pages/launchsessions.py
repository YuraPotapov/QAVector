"""How many windows a run keeps open at once, in a small panel under its button.

The number is set once and rarely touched, so it no longer takes a block of
the page: the page shows what is in force ("3 at a time", "Auto") on one
button, and this panel - opened from it - holds the number, All at once,
Auto, and what the machine is doing right now, which is what the number is
weighed against. A machine under memory pressure is still said on the page
itself (see LaunchSessionsPage._update_metrics): that is worth seeing without
asking.

The panel owns the widgets and nothing else; the page reads and writes them as
it did when they sat in the page.
"""

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .. import widgets


def label_for(jobs, all_at_once, auto):
    """What the page's button says is in force."""
    if auto:
        return "Auto"
    if all_at_once:
        return "All at once"
    return "One at a time" if jobs <= 1 else "%d at a time" % jobs


class SessionsPanel(widgets.FloatingPanel):
    """See the module docstring."""

    WIDTH = 440

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("role", "clarifypopup")
        self.setFixedWidth(self.WIDTH)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        head = QFrame()
        head.setProperty("role", "clarifyhead")
        band = QHBoxLayout(head)
        band.setContentsMargins(12, 7, 12, 7)
        band.setSpacing(8)
        band.addWidget(widgets.kicker("Sessions at the same time"), 1)
        keys = QLabel("Esc closes")
        keys.setProperty("role", "keys")
        band.addWidget(keys)
        outer.addWidget(head)

        body = QWidget(self)
        column = QVBoxLayout(body)
        column.setContentsMargins(12, 10, 12, 12)
        column.setSpacing(8)
        self.jobs = widgets.Stepper(1, 64)
        self.jobs_all = QCheckBox("All at once")
        self.jobs_auto = QCheckBox("Auto")
        self.jobs_auto.setToolTip(
            "Start at one window per processor core and let the machine decide: "
            "fewer while memory is under real pressure, back up again once it is "
            "not. A number you type here is never changed for you.")
        column.addWidget(widgets.row(self.jobs, self.jobs_all, self.jobs_auto, None,
                                     spacing=12))
        hint = QLabel("One at a time is the calmest to watch; more is faster. "
                      "\"Auto\" lets the machine raise and lower it as the run goes.")
        hint.setProperty("role", "hint")
        hint.setWordWrap(True)
        column.addWidget(hint)
        self.metrics = QLabel("System load: CPU --% | RAM --%")
        self.metrics.setProperty("role", "hint")
        self.metrics.setWordWrap(True)
        column.addWidget(self.metrics)
        outer.addWidget(body)

    def open_below(self, anchor):
        """Show under ``anchor`` (the page's button), on the number."""
        self.adjustSize()
        self.move(anchor.mapToGlobal(QPoint(0, anchor.height() + 4)))
        self.show()
        self.raise_()
        self.activateWindow()
        target = self.jobs if self.jobs.isEnabled() else self.jobs_auto
        target.setFocus(Qt.PopupFocusReason)

    def keyPressEvent(self, event):          # noqa: N802 - Qt's own spelling
        if event.key() == Qt.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)
