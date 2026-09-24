"""Exercise app.main, including the wheel guard absent from page-only tests."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize("platform", ["offscreen", "xcb"])
def test_real_startup_keeps_the_window_when_opening_and_reopening_html(tmp_path, platform):
    if platform == "xcb" and (sys.platform != "linux" or not os.environ.get("DISPLAY")):
        pytest.skip("Native window recreation needs an X11 display")
    gui = Path(__file__).resolve().parents[1]
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "jira.html").write_text(
        '<!doctype html><h1 style="color:navy">Task description</h1>', encoding="utf-8")
    (reports / "jira.json").write_text('{"key":"QA-1"}', encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(gui), QT_QPA_PLATFORM=platform,
               QTWEBENGINE_CHROMIUM_FLAGS="--disable-gpu",
               CMS_HOME=str(tmp_path / "data"), XDG_CONFIG_HOME=str(tmp_path / "config"),
               XDG_DATA_HOME=str(tmp_path / "xdg-data"))
    if platform == "offscreen":
        env["QT_QUICK_BACKEND"] = "software"
    else:
        # Software Quick rendering hides the raster-to-GPU window transition.
        # Use the desktop's real rendering path for this regression check.
        env.pop("QT_QUICK_BACKEND", None)
    # Native binding failures kill the process. A child both catches that failure
    # and guarantees the real startup order, without an existing test QApplication.
    script = textwrap.dedent('''
        import sys
        from pathlib import Path
        from PySide6.QtCore import QTimer, QPoint, QPointF, Qt, QEvent
        from PySide6.QtGui import QWheelEvent
        from PySide6.QtWidgets import QApplication, QDialog, QSpinBox, QVBoxLayout
        from cms_gui import app as app_mod

        reports = Path(sys.argv[1])
        loaded = []
        errors = []
        def failed(kind, error, traceback):
            errors.append(str(error))
            QApplication.instance().quit()
        sys.excepthook = failed

        class Window(app_mod.MainWindow):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.window_events = []
                self.check_window = False
                QTimer.singleShot(500, self.open_report)
                QTimer.singleShot(15000, QApplication.instance().quit)

            def event(self, event):
                if getattr(self, 'check_window', False) and event.type() in (
                        QEvent.Hide, QEvent.Show, QEvent.WinIdChange):
                    self.window_events.append(str(event.type()))
                return super().event(event)

            def refresh_inventory(self):
                pass  # The HTML preview must not need a core, Jira or services.

            def open_report(self):
                assert QApplication.instance()._no_wheel_steal is not None
                assert self.artifacts.html is None  # Chromium is still lazy.
                self.original_id = int(self.internalWinId())
                self.original_geometry = self.geometry()
                self.check_window = True
                self.show_page('artifacts')
                self.artifacts.show_dir(str(reports))
                self.artifacts._select_path(str(reports / 'jira.html'))
                self.artifacts.html.loadFinished.connect(self.report_loaded)

            def report_loaded(self, ok):
                if not ok:
                    return
                loaded.append(ok)
                if len(loaded) == 1:
                    QTimer.singleShot(50, self.reopen)
                else:
                    QTimer.singleShot(50, self.finish)

            def finish(self):
                assert int(self.internalWinId()) == self.original_id
                assert self.geometry() == self.original_geometry
                assert not self.window_events, self.window_events
                self.check_window = False
                self.close()

            def reopen(self):
                dialog = QDialog(self)
                layout = QVBoxLayout(dialog)
                box = QSpinBox()
                box.setRange(0, 10)
                box.setValue(5)
                layout.addWidget(box)
                dialog.show()
                wheel = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, -120),
                                    QPoint(0, -120), Qt.NoButton, Qt.NoModifier,
                                    Qt.NoScrollPhase, False)
                QApplication.sendEvent(box.lineEdit(), wheel)
                assert box.value() == 5
                dialog.close()
                dialog.deleteLater()
                self.show_page('launch')
                self.show_page('artifacts')
                self.artifacts.preview_mode.setCurrentIndex(1)
                assert '<h1' in self.artifacts.text.toPlainText()
                self.artifacts._select_path(str(reports / 'jira.json'))
                assert 'QA-1' in self.artifacts.text.toPlainText()
                self.artifacts._select_path(str(reports / 'jira.html'))
                self.artifacts.preview_mode.setCurrentIndex(0)

        app_mod.MainWindow = Window
        app_mod._splash_file = lambda: None
        assert app_mod.main(['html-startup-test']) == 0
        assert not errors, errors
        assert loaded == [True, True], loaded
        print('HTML startup and reopen passed with the same native window')
    ''')
    result = subprocess.run([sys.executable, "-X", "faulthandler", "-c", script, str(reports)],
                            cwd=gui, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "HTML startup and reopen passed" in result.stdout
