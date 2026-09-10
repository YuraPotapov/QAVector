"""Shared fixtures. Qt objects need an application instance, even headless."""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# No display on CI (or in a terminal session): render to nothing at all.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Pages persist as they are edited - the Command form to QSettings, the history
# and saved configurations to the data directory - so instantiating one in a test
# would otherwise write into the developer's own settings and history. Both
# locations are redirected before Qt resolves them, which it does lazily on first
# use, so this must happen at import time rather than in a fixture.
_SANDBOX = tempfile.mkdtemp(prefix="cms-gui-tests-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_SANDBOX, "config")
os.environ["XDG_DATA_HOME"] = os.path.join(_SANDBOX, "data")

# And the home directory itself. The two above cover everything Qt resolves, but
# not everything the application does: services.json defaults to
# ``~/QAVector`` and gets there through os.path.expanduser, which reads
# $HOME and has never heard of XDG. Without this the suite loads - and any test
# that reaches Save *writes* - the developer's own live services file. That is not
# a hypothetical: it is how a run came back with somebody's real projects in it.
os.environ["HOME"] = os.path.join(_SANDBOX, "home")
os.environ["USERPROFILE"] = os.environ["HOME"]      # the same thing on Windows
os.makedirs(os.environ["HOME"], exist_ok=True)


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    from cms_gui import theme

    app = QApplication.instance() or QApplication([])
    # Same two lines as app.main(). Without them a widget renders in Fusion's own
    # colours, so anything asserting on what the design paints - a primary
    # button's fill, a status colour - would be measuring the wrong thing.
    app.setStyle(theme.app_style())
    theme.load_fonts()
    app.setStyleSheet(theme.stylesheet())
    yield app
