"""The shared integration host must not keep a GUI process alive."""

import os
import subprocess
import sys
import textwrap


def test_main_window_close_exits_process_with_another_provider_detached():
    script = textwrap.dedent('''
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QMainWindow,
                                      QStackedWidget, QVBoxLayout, QWidget)
        from cms_gui.integrationhost import IntegrationHost
        from cms_gui.settings import Settings
        from shiboken6 import isValid
        app = QApplication([])
        main = QMainWindow()
        stack = QStackedWidget()
        main.setCentralWidget(stack)
        view = QWidget()
        column = QVBoxLayout(view)
        view.window_actions = QHBoxLayout()
        column.addLayout(view.window_actions)
        column.addWidget(QLabel('Shared view'))
        host = IntegrationHost('example', 'Example provider', view,
                               stack, Settings(), main)
        app.aboutToQuit.connect(host.shutdown)
        main.show()
        child = host.detach()
        assert child.isVisible()
        QTimer.singleShot(50, main.close)
        app.exec()
        assert not isValid(child) or not child.isVisible()
        assert host.window is None
        print('Exited with detached integration closed')
    ''')
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
    env['PYTHONPATH'] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run([sys.executable, '-c', script], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert 'Exited with detached integration closed' in result.stdout
