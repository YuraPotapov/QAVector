"""One integration view, hosted in the page or in an application-owned window.

Every provider uses this host. Moving the existing widget preserves drafts,
cached data and in-flight requests; opening a second copy is never necessary.
Windows follow the active project and never keep the application alive.
See docs/integration-windows.md for the provider and lifecycle contract.
"""

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import QPushButton, QToolButton, QVBoxLayout, QWidget

from . import icons, widgets


class IntegrationWindow(QWidget):
    return_requested = Signal()

    def __init__(self, title, geometry=None):
        super().__init__(None)
        self._shutting = False
        self._closed = False
        self.setWindowTitle(title)
        self.setAttribute(Qt.WA_QuitOnClose, False)
        self.frame = widgets.dress(self, minimizable=True)
        self.setWindowFlag(Qt.Window, True)
        self.column = QVBoxLayout(self)
        self.column.setContentsMargins(0, 0, 0, 0)
        self.resize(1200, 800)
        if geometry:
            self.restoreGeometry(geometry)

    def set_title(self, title):
        self.setWindowTitle(title)
        if self.frame is not None:
            self.frame.bar.name.setText(title)

    def bring_forward(self):
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()

    def shut(self):
        self._shutting = True
        if not self._closed:
            self.close()

    def closeEvent(self, event):
        super().closeEvent(event)
        self._closed = True
        if not self._shutting:
            self.return_requested.emit()


class IntegrationHost(QObject):
    """One host per provider, owned by the Integrations page.

    The page calls set_project_name on a context change and shutdown on exit.
    Detaching only changes the view's parent; it never changes its core.
    """

    docked = Signal()

    def __init__(self, provider, name, view, stack, settings, parent):
        super().__init__(parent)
        self.provider, self.name = provider, name
        self.view, self.stack, self.settings = view, stack, settings
        self.window = None
        self.project_name = "Workspace"
        self._closing = False
        self.toggle_button = QToolButton()
        self.toggle_button.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.toggle_button.setProperty("variant", "icon")
        self.toggle_button.setCursor(Qt.PointingHandCursor)
        self.toggle_button.clicked.connect(self.toggle)
        view.window_actions.addWidget(self.toggle_button)
        view.installEventFilter(self)
        self._update_button()
        # What the page shows while the view is in its own window: a state of
        # the page, drawn like the application's other empty states.
        self.placeholder = QWidget()
        column = QVBoxLayout(self.placeholder)
        column.setContentsMargins(24, 22, 24, 18)
        column.setSpacing(10)
        column.addWidget(widgets.heading(name))
        column.addWidget(widgets.empty_zone(
            "%s is open in its own window" % name,
            "It follows the project picked here. Bring its window forward, or put "
            "it back on this page."))
        self.show_button = QPushButton("Show window")
        self.show_button.setProperty("variant", "primary")
        self.show_button.clicked.connect(self.open)
        self.return_button = QPushButton("Return here")
        self.return_button.clicked.connect(self.dock)
        column.addWidget(widgets.row(self.show_button, self.return_button, None))
        column.addStretch(1)
        stack.addWidget(view)
        stack.addWidget(self.placeholder)

    def title(self):
        return "%s — %s — QAVector" % (self.name, self.project_name)

    def _update_button(self):
        detached = self.window is not None
        icons.button(self.toggle_button, "dock" if detached else "popout")
        text = "Return to main window" if detached else "Open in separate window"
        self.toggle_button.setToolTip(text)
        self.toggle_button.setAccessibleName(text)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.StyleChange, QEvent.PaletteChange):
            self._update_button()
        return super().eventFilter(watched, event)

    def toggle(self):
        if self.window is None:
            self.detach()
        else:
            self.dock()

    def set_project_name(self, name):
        self.project_name = name
        if self.window is not None:
            self.window.set_title(self.title())

    def open(self):
        if self._closing:
            return
        self.stack.setCurrentWidget(self.placeholder if self.window else self.view)
        if self.window is not None:
            self.window.bring_forward()

    def detach(self):
        if self._closing:
            return None
        if self.window is not None:
            self.open()
            return self.window
        window = IntegrationWindow(self.title(), self.settings.integration_geometry(self.provider))
        self.window = window
        self._update_button()
        # A deleted page must not leave an independently owned window behind.
        self.destroyed.connect(window.deleteLater)
        window.return_requested.connect(self.dock)
        self.stack.removeWidget(self.view)
        window.column.addWidget(self.view, 1)
        self.stack.setCurrentWidget(self.placeholder)
        self.view.show()
        window.bring_forward()
        return window

    def dock(self):
        window = self.window
        if window is None or self._closing:
            return
        self.settings.save_integration_geometry(self.provider, window.saveGeometry())
        self.window = None
        self._update_button()
        window.column.removeWidget(self.view)
        self.stack.addWidget(self.view)
        self.stack.setCurrentWidget(self.view)
        window.shut()
        window.deleteLater()
        self.docked.emit()

    def shutdown(self):
        self._closing = True
        if self.window is not None:
            window, self.window = self.window, None
            self.settings.save_integration_geometry(self.provider, window.saveGeometry())
            # Keep ownership under the page for normal QObject destruction.
            window.column.removeWidget(self.view)
            self.stack.addWidget(self.view)
            self.view.hide()
            window.shut()
            window.deleteLater()
