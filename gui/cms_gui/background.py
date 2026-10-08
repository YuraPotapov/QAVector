"""Blocking work off the GUI thread, with the answer handed back on it.

Every call into the core is a process started and waited for. On a small
cycle that is a blink; with a few hundred runs behind it, opening one kept the
window frozen for over a second - long enough that somebody cannot tell a
program that is loading from one that has hung. Done here instead, the window
keeps painting, and the page can show where something is on its way (see
``widgets.Skeleton``).

Two rules, both learned the hard way in this application's tests
(``gui/tests/conftest.py`` says more):

* **A running QThread must never be deleted.** Jobs are kept in a module-level
  set, not parented to the widget that asked, so closing a page while a call
  is out cannot destroy a thread still running. :func:`wait_all` is what the
  window calls on its way out.
* **The answer arrives as a queued signal**, on the GUI thread, and goes
  nowhere if whoever asked has gone: a slot of a deleted object is simply not
  called.

A core that answers from memory - the fakes the tests use - is not worth a
thread: :func:`offloads` says whether a core's calls block, and callers run
the work directly when they do not.
"""

import atexit

from PySide6.QtCore import QThread, Signal

#: Every job still running. Holding them here is what keeps a thread alive
#: past the widget that started it.
_running = set()
#: Every job whose answer has not been delivered yet. A thread can be done
#: while its answer still waits in the event queue - it is not done for
#: whoever is waiting on the answer.
_unanswered = set()


class Job(QThread):
    """One piece of blocking work. ``answered(result, error)`` when done."""

    answered = Signal(object, object)

    def __init__(self, work):
        QThread.__init__(self)
        self._work = work

    def run(self):
        try:
            result, error = self._work(), None
        except Exception as exc:  # noqa: BLE001 - the caller decides what it means
            result, error = None, exc
        self.answered.emit(result, error)


def offloads(core):
    """Whether calls on ``core`` block - start a process - and so want a thread."""
    return bool(getattr(core, "offload", False))


def run(work, done, owner=None):
    """Start ``work`` on a thread; ``done(result, error)`` on the GUI thread.

    ``owner`` is the widget that asked. If it has been deleted by the time the
    answer comes - a window closed while a call was out - the answer goes
    nowhere instead of into an object that is no longer there.
    """
    job = Job(work)
    _running.add(job)
    _unanswered.add(job)

    def answered(result, error):
        _unanswered.discard(job)
        if owner is None or _alive(owner):
            done(result, error)

    job.answered.connect(answered)
    job.finished.connect(lambda: _finished(job))
    job.start()
    return job


def call(work):
    """``(result, error)`` of ``work`` done here and now - the same contract."""
    try:
        return work(), None
    except Exception as exc:  # noqa: BLE001
        return None, exc


def wait(work, core=None):
    """``(result, error)`` of ``work``, with the window still painting meanwhile.

    For the calls a page has to have the answer to before it can go on - a
    save, whose answer decides what is drawn next - but which used to be made
    on the GUI thread, freezing the window for as long as the core took: long
    enough to look hung. The work goes to a thread and a local event loop runs
    until it answers, so whatever says "this is happening" keeps animating.
    The caller blocks input itself; this only keeps the window alive.

    A core that does not block (the tests' fakes) is called here and now.
    """
    if core is not None and not offloads(core):
        return call(work)
    from PySide6.QtCore import QEventLoop

    loop, answer = QEventLoop(), []

    def done(result, error):
        answer.append((result, error))
        loop.quit()

    run(work, done)
    if not answer:
        loop.exec()
    return answer[0]


def busy():
    """Whether any job is still out - running, or answered and not delivered."""
    return bool(_unanswered) or any(job.isRunning() for job in _running)


def wait_all(milliseconds=5000):
    """Wait for every job still running - for the window's way out."""
    for job in list(_running):
        job.wait(milliseconds)


def _finished(job):
    _running.discard(job)
    job.deleteLater()


def _alive(thing):
    try:
        import shiboken6
    except ImportError:                   # not a PySide build that ships it
        return True
    return shiboken6.isValid(thing)


# A QThread destroyed while it runs takes the process with it, and at exit the
# interpreter destroys whatever is left. Waiting here, before that, is what
# makes quitting with a call still out an ordinary quit.
atexit.register(wait_all)
