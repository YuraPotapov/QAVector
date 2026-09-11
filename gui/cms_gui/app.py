"""Application entry point."""

import os
import sys

from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication, QSplashScreen

from . import icon, migrate, theme
from .settings import Settings
from .main_window import MainWindow

#: Edge of the splash, in px. The artwork is square and readable at this size;
#: bigger starts to look like a window rather than a splash.
SPLASH_PX = 600

#: Tried in order - whatever an artist last dropped into assets/ wins. Keeping a
#: list rather than one name means replacing the image is a file copy, not a
#: code change.
SPLASH_NAMES = ("splash.jpg", "splash.jpeg", "splash.png")

#: Height of the status strip painted across the foot of the splash. The artwork
#: is a drawing with its own annotations, so a message laid straight onto it
#: reads as part of the drawing - which is how a loading app looks stuck.
SPLASH_BAND_PX = 46


def _splash_file():
    """The splash artwork to use, or None when assets/ has none."""
    assets = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
    for name in SPLASH_NAMES:
        path = os.path.join(assets, name)
        if os.path.isfile(path):
            return path
    return None


class Splash(QSplashScreen):
    """The startup splash, with a status strip across its foot.

    ``drawContents`` rather than a message laid onto the artwork: the picture is
    a technical drawing with its own annotations and grid, and a line of status
    dropped on top of it reads as part of the drawing - which is exactly how a
    loading app looks like a stuck one. The strip is repainted with every
    message rather than baked into the pixmap once, so it stays put as the text
    changes length.
    """

    def drawContents(self, painter):
        band = QRect(0, self.height() - SPLASH_BAND_PX,
                     self.width(), SPLASH_BAND_PX)
        # The page's own colours, not their negative. This used to fill with the
        # ink and write in the background, which made the strip the opposite of
        # whatever the application was about to open as: a light bar under a dark
        # window, and a dark one under a light window. The first thing anybody
        # sees should not be a contradiction of the second.
        painter.fillRect(band, QColor(theme.BG))
        # A hairline where it meets the artwork, since the fill no longer
        # separates them by sheer contrast - the same divider the rest uses.
        painter.setPen(QColor(theme.DIVIDER))
        painter.drawLine(band.topLeft(), band.topRight())
        painter.setPen(QColor(theme.TEXT))
        painter.drawText(band.adjusted(18, 0, -18, 0),
                         Qt.AlignVCenter | Qt.AlignLeft, self.message())


def _claim_windows_identity():
    """Tell Windows this process is its own application, not its host's.

    Without an explicit AppUserModelID the taskbar attributes the window to
    whatever launched it - python.exe when run from a checkout - and shows that
    program's icon and grouping instead of ours. Set before the first window
    exists, which is the only time it takes effect. Silent everywhere else, and
    on any Windows that will not answer: an icon is not worth an exception.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "qavector.gui")
    except Exception:
        pass


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    _claim_windows_identity()
    app = QApplication(argv)
    app.setApplicationName("QAVector")
    app.setOrganizationName("qavector")
    # Before anything opens a setting or the data directory: the first start
    # after the rename moves chrome-multi-session's folders and settings to
    # QAVector's.
    migrate.run()
    # Fusion is the one style that looks the same on all three platforms, which
    # is what makes a single stylesheet enough to carry the design - plus the
    # indicators, which are painted because a stylesheet cannot draw them.
    app.setStyle(theme.app_style())
    theme.load_fonts()
    
    settings = Settings()
    theme.set_dark_mode(settings.dark_mode)
    
    app.setStyleSheet(theme.stylesheet())
    # Set on the application as well as the window: on Wayland and on Windows the
    # task switcher reads it from here, not from the window.
    app.setWindowIcon(icon.app_icon())

    auto_launch = None
    if "--launch" in argv:
        idx = argv.index("--launch")
        if idx + 1 < len(argv):
            auto_launch = argv[idx + 1]

    splash = None
    splash_path = _splash_file()
    if splash_path and not auto_launch:
        # The artwork carries its own title, so no version is painted onto it -
        # that used to be drawn here AND shown as a message, printing it twice.
        pixmap = QPixmap(splash_path).scaled(SPLASH_PX, SPLASH_PX,
                                             Qt.KeepAspectRatio,
                                             Qt.SmoothTransformation)
        splash = Splash(pixmap, Qt.WindowStaysOnTopHint)
        splash.showMessage("starting…")
        splash.show()
        app.processEvents()

    window = MainWindow(splash=splash, auto_launch=auto_launch, headless=bool(auto_launch))
    geometry = window.settings.geometry()
    if geometry:
        window.restoreGeometry(geometry)

    if splash is None and not window._headless:
        window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
