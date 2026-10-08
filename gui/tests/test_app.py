"""The startup splash - the first thing anybody sees.

Its own file because ``app.main`` is the one thing the suite cannot call: it
builds a QApplication and enters an event loop. The pieces it assembles can
still be asked questions.
"""

from PySide6.QtGui import QPainter, QPixmap

from cms_gui import app as app_mod
from cms_gui import theme


def _band_colours(qapp, dark):
    """(the window's background, what the splash paints its strip)."""
    theme.set_dark_mode(dark)
    qapp.setStyleSheet(theme.stylesheet())
    try:
        artwork = QPixmap(app_mod.SPLASH_PX, app_mod.SPLASH_PX)
        artwork.fill()
        splash = app_mod.Splash(artwork)
        splash.showMessage("starting…")

        canvas = QPixmap(app_mod.SPLASH_PX, app_mod.SPLASH_PX)
        canvas.fill()
        painter = QPainter(canvas)
        splash.drawContents(painter)
        painter.end()

        middle = app_mod.SPLASH_PX - app_mod.SPLASH_BAND_PX // 2
        return theme.BG, canvas.toImage().pixelColor(400, middle).name()
    finally:
        theme.set_dark_mode(False)
        qapp.setStyleSheet(theme.stylesheet())


def test_the_splash_opens_in_the_colours_the_window_will(qapp):
    """It used to fill with the ink and write in the background, so the strip
    was the negative of whatever was about to open: a light bar under a dark
    window and a dark one under a light window."""
    for dark in (False, True):
        background, band = _band_colours(qapp, dark)
        assert band.lower() == background.lower(), (
            "%s mode: strip %s under a %s window"
            % ("dark" if dark else "light", band, background))


def test_the_strip_is_still_a_strip(qapp):
    # The fill no longer separates it from the artwork by sheer contrast, so it
    # is divided from it the way everything else in the design is.
    theme.set_dark_mode(False)
    qapp.setStyleSheet(theme.stylesheet())
    artwork = QPixmap(app_mod.SPLASH_PX, app_mod.SPLASH_PX)
    artwork.fill()
    splash = app_mod.Splash(artwork)
    splash.showMessage("starting…")
    canvas = QPixmap(app_mod.SPLASH_PX, app_mod.SPLASH_PX)
    canvas.fill()
    painter = QPainter(canvas)
    splash.drawContents(painter)
    painter.end()
    top = app_mod.SPLASH_PX - app_mod.SPLASH_BAND_PX
    assert canvas.toImage().pixelColor(400, top).name().lower() \
        == theme.DIVIDER.lower()


# ------------------------------------------------- on screen, with its picture
# It used to be show() and one pass of the event loop. A compositing window
# manager shows what is in a window only once the application has painted it
# and answered for that frame in its event loop - so for the second or two the
# main window took to build, the splash was an empty grey square.
def test_the_splash_is_exposed_and_painted_before_anything_else_starts(qapp):
    artwork = QPixmap(app_mod.SPLASH_PX, app_mod.SPLASH_PX)
    artwork.fill()
    splash = app_mod.Splash(artwork)
    painted = []
    original = splash.repaint
    splash.repaint = lambda: (painted.append(splash.windowHandle().isExposed()),
                              original())
    try:
        app_mod.show_painted(qapp, splash)
        assert painted == [True], "painted once, and only once it was on screen"
    finally:
        splash.close()
        splash.deleteLater()


def test_a_splash_the_window_system_never_exposes_does_not_hold_up_the_start(
        qapp, monkeypatch):
    import time

    from PySide6.QtGui import QWindow

    monkeypatch.setattr(QWindow, "isExposed", lambda self: False)
    artwork = QPixmap(app_mod.SPLASH_PX, app_mod.SPLASH_PX)
    artwork.fill()
    splash = app_mod.Splash(artwork)
    # QSplashScreen.show() waits for the window on its own, up to a second on
    # some platforms; that wait is Qt's, so it is measured and taken off.
    started = time.monotonic()
    splash.show()
    qt_wait = time.monotonic() - started
    splash.hide()
    try:
        started = time.monotonic()
        app_mod.show_painted(qapp, splash, limit=0.2)
        assert time.monotonic() - started < qt_wait + 0.6
    finally:
        splash.close()
        splash.deleteLater()
