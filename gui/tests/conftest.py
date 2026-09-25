"""Shared fixtures. Qt objects need an application instance, even headless."""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# No display on CI (or in a terminal session): render to nothing at all.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.environ["QT_QPA_PLATFORM"] == "offscreen":
    os.environ.setdefault("QT_QUICK_BACKEND", "software")

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


def _dispose(widget):
    """Close ``widget`` and delete it now, rather than at the end of the process.

    A closed window is only hidden, and a MainWindow is never collected either:
    its lambdas capture ``self`` and are connected to its own children's signals,
    so every one of them is a cycle through C++ that Python's collector cannot
    see. After test_main_window alone 1,945 windows and 92,265 widgets were still
    alive - and a test that sets the application's stylesheet restyles every one
    of them, which is how the light/dark tests came to run for ever and take the
    whole suite with them.

    Before it goes, _quiet stops what it still has running and delivers what
    that posted - see there for the crashes each step is there to prevent. If
    something will not stop, the window is left alone: a leaked window costs a
    little memory, a deleted one with a thread still in it costs the whole run.
    """
    from PySide6.QtCore import QCoreApplication, QEvent

    widget.close()
    if not _quiet(widget):
        return
    widget.deleteLater()
    # This widget's deletion and nothing else's. No event loop runs in the suite,
    # so every deleteLater() the application itself made in earlier tests - a
    # finished status probe, a removed service - is still queued, some of them on
    # objects already gone with their parent. Delivering all of those at once
    # (receiver None) destroyed one a second time and took the run down.
    QCoreApplication.sendPostedEvents(widget, QEvent.DeferredDelete)


#: How long a window's thread may still need. Its loader thread runs --describe,
#: which cms_gui.core allows 90 s; stopping short of that is how a window got
#: deleted with the thread still in it and Qt aborted the whole process.
THREAD_WAIT_MS = 95000


def _quiet(widget):
    """Stop what ``widget`` still has running, deliver what it posted. True if it did.

    Every step here is the answer to a crash, not tidiness:

    * timers are stopped, so nothing new starts - a Services page polls its
      services on one and starts a status probe from it;
    * threads are waited for, as long as they may take: a QThread destroyed while
      it runs aborts the whole process, and a close refused by one of a window's
      own questions never reaches the wait in its closeEvent;
    * processes are killed and waited for: a QProcess destroyed while it runs is
      killed from its own destructor, and its finished() then lands on objects
      already half torn down;
    * and whatever all of that posted is delivered while the widget still exists:
      a window's loader thread hands its inventory back as a queued signal, and
      one deleted with that still in the queue crashed the process on the next
      test's first processEvents().

    Then it looks again, because delivering can start more. A window schedules
    its inventory refresh with QTimer.singleShot, which is no child timer of
    its own to stop; when a close has been refused the window is not closing,
    so that refresh fired during the delivery above and started a fresh loader
    thread - and the window was deleted with it running. So the whole pass
    repeats until one finds nothing running, a few times at most; anything
    still going after that is reported, not deleted.
    """
    from PySide6.QtCore import QCoreApplication, QProcess, QThread, QTimer

    for timer in widget.findChildren(QTimer):
        timer.stop()
    for _pass in range(QUIET_PASSES):
        for thread in widget.findChildren(QThread):
            if thread.isRunning() and not thread.wait(THREAD_WAIT_MS):
                return False
        for process in widget.findChildren(QProcess):
            if process.state() != QProcess.NotRunning:
                process.kill()
                if not process.waitForFinished(3000):
                    return False
        QCoreApplication.processEvents()
        if not (any(thread.isRunning() for thread in widget.findChildren(QThread))
                or any(process.state() != QProcess.NotRunning
                       for process in widget.findChildren(QProcess))):
            return True
    return False


#: How many wait-and-deliver passes _quiet makes before it gives up on a widget.
QUIET_PASSES = 5


@pytest.fixture
def dispose():
    """``dispose(widget)`` - close a window and delete it before the next test."""
    return _dispose


#: The tests that set the whole application's stylesheet. Each one restyles
#: every widget still alive, so they go first - see below.
RESTYLE_FIRST = ("test_theme.py", "test_titlebar.py")


def pytest_collection_modifyitems(items):
    """Run the tests that restyle the whole application before everything else.

    Setting the application's stylesheet restyles every widget in the process,
    and a closed page, dialog or log window is still a widget until something
    deletes it. Main windows are disposed of; the rest are not, and forcing it
    from here is not safe - deleting a Services page from outside its own
    teardown crashed the process even once it was quiet. Run alphabetically, the
    light/dark tests came near the end, after 45,524 of those, and ran for as
    long as anyone would wait. First, they meet a nearly empty process.

    A stable sort: the order inside each group is exactly as collected.
    """
    items.sort(key=lambda item: 0 if os.path.basename(str(item.fspath)) in RESTYLE_FIRST
               else 1)


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from cms_gui import theme

    if QApplication.instance() is None:
        QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication.instance() or QApplication([])
    # Same two lines as app.main(). Without them a widget renders in Fusion's own
    # colours, so anything asserting on what the design paints - a primary
    # button's fill, a status colour - would be measuring the wrong thing.
    app.setStyle(theme.app_style())
    theme.load_fonts()
    app.setStyleSheet(theme.stylesheet())
    yield app


@pytest.fixture(autouse=True)
def no_waiting_dialogs(monkeypatch):
    """No test may open a dialog that waits for a person.

    Every message, warning and confirmation in the application goes through
    ``widgets.Message``, so one patch here covers all of them - and a test that
    forgets to think about a dialog fails on its assertion rather than hanging
    the suite until the timeout, which is what used to happen and is much
    harder to read.

    Accepted by default, so a confirmation reads as "yes" and the test gets on
    with whatever it is actually about. A test that cares which answer was
    given patches the wrapper it expects to be called - see the ones that do.
    """
    from PySide6.QtWidgets import QDialog

    from cms_gui import widgets

    monkeypatch.setattr(widgets.Message, "exec",
                        lambda self: QDialog.Accepted, raising=False)


@pytest.fixture(autouse=True)
def no_queue_is_asked(monkeypatch):
    """A window built in a test starts listening for new tasks, and the core it
    asks is the real one: the bundled cycles' queues are real Jira sites. A
    test must not reach the network, so asking says it cannot - the listener's
    own tests use a fake core and never get here."""
    from cms_gui import core as core_mod

    monkeypatch.setattr(core_mod.Core, "cycle_watch",
                        lambda self, cycle_id, trigger_id="": {
                            "ok": False, "problems": ["not asked in tests"]})
