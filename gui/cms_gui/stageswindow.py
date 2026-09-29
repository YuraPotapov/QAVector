"""The Cycles page's Stages, in a window of its own.

Stages is the readable account of what a run is doing, and a run takes long
enough that somebody wants to keep watching it while they do something else -
another page of this application, or another application altogether on the
other screen. A tab under the canvas cannot follow them there; a window can.

**The same feed, moved - never a copy.** The window is handed the very panel
the tab held, the ``StageList`` and the composer under it, and gives it back
when it closes. ``StageList`` is append-only and follows the run as it goes; a
second one would draw every row twice and scroll on its own, and the two would
disagree about what had been read.

**It lives only while the application does.** It has no parent - a parented
window stays above its parent and minimizes with it, and this one is meant to
go on another screen and be minimized by itself - so nothing about Qt's
ownership ties the two together. Two things do instead: it does not count as a
window whose closing keeps the process up (``WA_QuitOnClose``), and the main
window shuts it on its own way out. The page also deletes it with itself.

**Closing it is putting Stages back.** Only a person closing it does that, and
only that is remembered: the application shutting it on exit leaves it
detached, so it opens detached again next time.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from . import widgets

#: What goes to Stages from any window of the application, wherever Stages
#: is - a tab on the Cycles page or this window.
SHORTCUT = "Ctrl+Shift+S"

#: The window's name, and with a cycle's name after it once a run has one.
TITLE = "Stages"

#: How big it opens before it has a remembered place: a column, the shape
#: of a feed read top to bottom.
FIRST_SIZE = (560, 760)


def title_for(name):
    """``Stages - Development`` - or just ``Stages`` with nothing running."""
    return "%s - %s" % (TITLE, name) if name else TITLE


class StagesWindow(QWidget):
    """A frame for the stage panel, while it is not in its tab."""

    #: A person closed it - which means "put Stages back in the tab".
    closed_by_person = Signal()

    def __init__(self, settings=None):
        super().__init__(None)
        self.settings = settings
        self._panel = None
        self._shutting = False
        self._closed = False
        self.setWindowTitle(TITLE)
        # Not counted among the windows that keep the application running:
        # closing the main window ends the process with this one open.
        self.setAttribute(Qt.WA_QuitOnClose, False)
        # All three controls - minimize, maximize and close - because this is
        # a window somebody works beside, not a dialog they answer.
        self.frame = widgets.dress(self, minimizable=True)
        self.setWindowFlag(Qt.Window, True)
        self._column = QVBoxLayout(self)
        self._column.setContentsMargins(0, 0, 0, 0)
        self._column.setSpacing(0)
        self.resize(*FIRST_SIZE)
        geometry = settings.stages_geometry() if settings is not None else None
        if geometry:
            self.restoreGeometry(geometry)

    # -- the panel ------------------------------------------------------------
    def hold(self, panel):
        """Take ``panel`` in. It is reparented, not copied."""
        self._panel = panel
        self._column.addWidget(panel, 1)
        panel.show()

    def release(self):
        """Give the panel back, for the tab to take. None if it holds none."""
        panel, self._panel = self._panel, None
        if panel is not None:
            self._column.removeWidget(panel)
        return panel

    def panel(self):
        return self._panel

    # -- what it says ---------------------------------------------------------
    def set_run_name(self, name):
        title = title_for(name)
        if title == self.windowTitle():
            return
        self.setWindowTitle(title)
        # The drawn title bar takes its text once, when it is fitted, and does
        # not follow the window's title after that.
        if self.frame is not None:
            self.frame.bar.name.setText(title)

    # -- showing and going ----------------------------------------------------
    def bring_forward(self):
        """In front, and back from the taskbar if it had been minimized.

        Only the minimized bit is cleared, so a window that was maximized
        before it was minimized comes back maximized.
        """
        state = self.windowState()
        if state & Qt.WindowMinimized:
            self.setWindowState(state & ~Qt.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()

    def shut(self):
        """Close it because the application is going, not because of a person."""
        self._shutting = True
        if not self._closed:
            self.close()

    def closed(self):
        """Whether it has been closed - including from inside its own close."""
        return self._closed

    def closeEvent(self, event):             # noqa: N802 - Qt's own spelling
        if self.settings is not None:
            self.settings.save_stages_geometry(self.saveGeometry())
        super().closeEvent(event)
        # Set before saying so: whoever is told may want it gone, and closing
        # it again from inside this would be a close within a close.
        self._closed = True
        if not self._shutting:
            self.closed_by_person.emit()
