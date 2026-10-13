"""The browser abstraction the engine depends on.

Every method takes an already-resolved CSS selector (the compiler turns named
targets into CSS) and a timeout in milliseconds (None -> the caller's default).
Query methods (``exists``/``visible``) wait up to the timeout and return a bool
rather than raising, so assertions can report a clean pass/fail.
"""

from abc import ABC, abstractmethod


class BrowserAdapter(ABC):
    # --- actions -------------------------------------------------------------
    @abstractmethod
    def goto(self, url, timeout=None):
        """Navigate to ``url``."""

    @abstractmethod
    def fill(self, selector, value, timeout=None):
        """Type ``value`` into the element matched by ``selector``."""

    @abstractmethod
    def click(self, selector, timeout=None):
        """Click the element matched by ``selector``."""

    @abstractmethod
    def select(self, selector, value, timeout=None):
        """Choose the option with ``value`` in the <select> matched by ``selector``.

        A native <option> cannot be clicked into selection - it has no box to hit -
        so picking one needs its own action rather than a click on the option.
        """

    @abstractmethod
    def wait_for(self, selector, state="visible", timeout=None):
        """Wait until ``selector`` reaches ``state`` (visible/attached/...)."""

    @abstractmethod
    def press_key(self, key):
        """Press a keyboard ``key`` (e.g. "Enter", "ArrowDown") on the focused element."""

    # --- queries (never raise on absence; return bool/str) -------------------
    @abstractmethod
    def exists(self, selector, timeout=None):
        """True if ``selector`` is attached within the timeout."""

    @abstractmethod
    def visible(self, selector, timeout=None):
        """True if ``selector`` is visible within the timeout."""

    @abstractmethod
    def text(self, selector, timeout=None):
        """Text content of the first ``selector`` match ("" if absent)."""

    @abstractmethod
    def url(self):
        """Current page URL."""

    @abstractmethod
    def title(self):
        """Current page title."""

    # --- diagnostics ---------------------------------------------------------
    @abstractmethod
    def screenshot(self, path):
        """Write a full-page screenshot to ``path``."""

    @abstractmethod
    def content(self):
        """Return the current DOM as an HTML string."""

    @abstractmethod
    def console_logs(self):
        """Return console messages captured since connect (list of strings)."""

    def settle(self, timeout_ms=3000):
        """Wait, briefly, until what the last step set off has finished drawing.

        Asked for right before a step's screenshot, so it shows the page the step
        led to rather than the one it left. Concrete and a no-op here: an adapter
        with no page, or a test double, has nothing to wait for.
        """

    def doc_capture(self, path, targets, focus=False):
        """A picture for the run's guide, and where its elements are on it.

        Writes what the window shows (the viewport, not the whole page) to
        ``path``, and answers ``{"page": {"key", "title", "dialog"}, "elements":
        [...]}``: one entry per selector in ``targets`` - ``{"box": [x, y, w, h]
        as fractions of the picture, "label", "secret"}``, or None when that
        element is not on it. ``focus`` stands the focused element in for a
        first target of None (what a bare ``press`` acts on).

        None - the default - means there is no page to take, and the guide
        carries the step as text only (see engine.livedoc).
        """
        return None

    # --- execution overlay (optional; default no-op) -------------------------
    # The HUD is rendered *inside* the page by injecting an isolated overlay over
    # the existing browser connection. These are concrete no-ops so an adapter
    # that cannot support it (or a test double) needs no changes.
    def overlay_setup(self, js_source):
        """Inject the overlay renderer now and re-inject it on future loads."""

    def overlay_render(self, state):
        """Push an overlay state dict into the page for the renderer to paint.

        May answer the ``treeVersion`` the page now holds, so the tree - the
        largest part of the state - is sent only when the page lacks it. None
        (the default) means "not known", and the tree is sent every time.
        """

    def overlay_mark(self, selector, label=None, timeout=None):
        """Flash a marker over the element a step is acting on.

        ``selector`` None means the currently focused element (what a bare
        ``press`` acts on).
        """

    def overlay_teardown(self):
        """Remove the injected overlay from the page."""

    # --- scenario recorder (optional; default no-op) --------------------------
    # The recorder is the overlay's mirror image: the page collects what the user
    # picked and Python takes it away. Same reasoning for the no-ops - an adapter
    # that cannot inject, or a test double, needs no changes.
    def recorder_setup(self, js_source):
        """Inject the recorder now and re-inject it on future documents."""

    def recorder_call(self, expression, argument=None):
        """Evaluate ``expression`` against window.__Recorder and return the result.

        ``expression`` is a JS arrow function body taking (recorder, argument).
        Returns None when the page has no recorder (mid-navigation, say), which
        the caller must treat as "nothing happened", not as an error.
        """
        return None

    def disconnect(self):
        """Release the backend. Default: no-op."""
