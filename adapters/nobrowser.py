"""The adapter a run with no browser gets: every page operation refuses.

``--no-browser`` runs scenarios made only of steps that never touch a page - the
service steps, which the GUI carries out, and ``assert_host_up``, which is a plain
HTTP probe. The launcher refuses any other scenario before it starts and the
runner checks again after compiling, so nothing should ever call in here. If
something does, it gets an error saying what happened rather than an
AttributeError on None.

The diagnostics refuse too, rather than answer empty: the reporter wraps each one
and skips whatever fails, so a report is ``result.json`` (and any server log), not
a folder of blank screenshots and empty DOM dumps.
"""

from adapters.base import BrowserAdapter


class NoBrowserError(RuntimeError):
    """A page operation was asked of a run that has no page."""


class NoBrowserAdapter(BrowserAdapter):
    @staticmethod
    def _refuse(what):
        raise NoBrowserError("this run has no browser: %s" % what)

    # ---------------------------------------------------------------- actions
    def goto(self, url, timeout=None):
        self._refuse("goto")

    def fill(self, selector, value, timeout=None):
        self._refuse("fill")

    def click(self, selector, timeout=None):
        self._refuse("click")

    def select(self, selector, value, timeout=None):
        self._refuse("select")

    def wait_for(self, selector, state="visible", timeout=None):
        self._refuse("wait_for")

    def press_key(self, key):
        self._refuse("press")

    # ---------------------------------------------------------------- queries
    def exists(self, selector, timeout=None):
        self._refuse("assert_exists")

    def visible(self, selector, timeout=None):
        self._refuse("assert_visible")

    def text(self, selector, timeout=None):
        self._refuse("assert_text_contains")

    def url(self):
        self._refuse("url")

    def title(self):
        self._refuse("title")

    # ------------------------------------------------------------ diagnostics
    def screenshot(self, path):
        self._refuse("screenshot")

    def content(self):
        self._refuse("dom")

    def console_logs(self):
        self._refuse("console")
