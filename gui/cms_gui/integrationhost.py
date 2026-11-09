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
        # What the page shows while the view is in its own window. Not an empty
        # state - nothing is missing: the page keeps its own header, crumb
        # included, and says where the rest went with the line every detached
        # panel leaves behind (widgets.Elsewhere, as Stages does).
        self.placeholder = QWidget()
        column = QVBoxLayout(self.placeholder)
        column.setContentsMargins(24, 18, 24, 14)   # the views' own margins
        column.setSpacing(4)
        self.crumb = QPushButton("Integrations  /")
        self.crumb.setObjectName("integrationCrumb")
        self.crumb.setProperty("variant", "ghost")
        self.crumb.setCursor(Qt.PointingHandCursor)
        if hasattr(view, "back_requested"):
            self.crumb.clicked.connect(view.back_requested)
        column.addWidget(self.crumb, 0, Qt.AlignLeft)
        column.addWidget(widgets.heading(name))
        column.addSpacing(10)
        panel = widgets.BlueprintPanel(padding=(14, 6, 8, 6))
        self.elsewhere = widgets.Elsewhere("%s is in a separate window. It follows "
                                           "the project picked here." % name)
        self.elsewhere.show_requested.connect(self.open)
        self.show_button = self.elsewhere.show_button
        self.return_button = QToolButton()
        self.return_button.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.return_button.setProperty("variant", "icon")
        self.return_button.setCursor(Qt.PointingHandCursor)
        self.return_button.setToolTip("Return to main window")
        self.return_button.setAccessibleName("Return to main window")
        self.return_button.clicked.connect(self.dock)
        self.elsewhere.add_widget(self.return_button)
        panel.layout().addWidget(self.elsewhere)
        column.addWidget(panel)
        column.addStretch(1)
        # Icons carry the theme's ink: drawn again when the theme changes.
        view.installEventFilter(self)
        self.placeholder.installEventFilter(self)
        self._update_button()
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
        icons.button(self.return_button, "dock")

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
