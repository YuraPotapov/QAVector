"""Playwright implementation of :class:`~adapters.base.BrowserAdapter`.

It attaches to the Chrome the launcher already started (and the auto-login
extension already signed in) via ``connect_over_cdp`` - so it drives the real,
logged-in window and needs no Playwright-bundled browser download. Playwright is
imported lazily inside :meth:`connect`, keeping this module importable (for unit
tests) without the package installed.
"""

import logging

from adapters.base import BrowserAdapter

log = logging.getLogger("flowengine.adapter")

# Playwright expresses "not found within timeout" as its own TimeoutError. We
# translate that into a clean False/"" for query methods instead of an
# exception, so assertions report pass/fail rather than crashing the run.
DEFAULT_TIMEOUT_MS = 30000

# settle(): how long a step's requests may keep the page busy before the
# screenshot is taken anyway, how long it has to stay quiet to count as done,
# and the pause before looking - a click's handler sends its request a moment
# after the click, not during it.
SETTLE_QUIET_MS = 400
SETTLE_FIRST_MS = 150
SETTLE_POLL_MS = 50

# Connections that stay open on purpose - Odoo's bus - and so never finish:
# counted, they would keep every screenshot waiting for the full timeout.
_LONG_LIVED_TYPES = ("websocket", "eventsource")
_LONG_LIVED_PATHS = ("/websocket", "/longpolling")

# doc_capture(): how long the element a step is about to act on may take to show
# before its picture is taken without it. An element of an earlier step is not
# waited for at all: it is on the page or it is not, and waiting for one that
# had gone cost every later picture of that page the wait again.
DOC_TARGET_MS = 3000

# The guide is about the app, not about the test tool: its pictures leave the
# HUD out. Applied by Playwright for the length of the capture only.
DOC_HIDE = "#__exec_hud_host__ { visibility: hidden !important; }"

# Which page the window is on, as the guide groups steps: the path with its
# record ids (and "new", what a record is before it is saved) blanked out, so a
# form stays one page across its save - and the dialog on top of it, if any.
_DOC_PAGE_JS = r"""() => {
  const shown = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden";
  };
  const open = [...document.querySelectorAll(
      '[role="dialog"], [aria-modal="true"], dialog[open]')].filter(shown);
  const top = open[open.length - 1];
  let dialog = "";
  if (top) {
    const by = top.getAttribute("aria-labelledby");
    const head = (by && document.getElementById(by)) ||
        top.querySelector(".modal-title, h1, h2, h3, h4");
    dialog = ((head && head.innerText) || top.getAttribute("aria-label") || "")
        .split("\n")[0].replace(/\s+/g, " ").trim().slice(0, 120) || "dialog";
  }
  const path = location.pathname.split("/")
      .map(part => /^(\d+|new)$/.test(part) ? "*" : part).join("/");
  return {key: location.origin + path + (top ? "|dialog:" + dialog : ""),
          title: (document.title || "").trim(), dialog: dialog,
          width: window.innerWidth, height: window.innerHeight};
}"""

# What a person would call an element, and where it is in the window. A field is
# named by its <label>, which in a form sits apart from the input.
_DOC_ELEMENT_JS = r"""el => {
  if (!el || !el.getBoundingClientRect) return null;
  const clean = text => (text || "").replace(/\s+/g, " ").trim();
  const field = el.matches("input, textarea, select") ? el :
      (el.matches("button, a") ? null : el.querySelector("input, textarea, select"));
  let label = el.getAttribute("aria-label");
  if (!label && field && field.labels && field.labels.length) label = field.labels[0].innerText;
  if (!label && field && field.id) {
    const by = document.querySelector('label[for="' + CSS.escape(field.id) + '"]');
    if (by) label = by.innerText;
  }
  if (!label && field) label = field.getAttribute("placeholder");
  // A field with no label of its own is named by the text just before it.
  if (!label && field) {
    const before = field.previousElementSibling;
    // textContent: innerText is the text as styled, and a caption is often
    // set in capitals by CSS alone.
    if (before && !before.matches("input, textarea, select")) label = before.textContent;
  }
  // A list's own text is its options, not its name.
  if (!label && !(field && field.tagName === "SELECT"))
    label = (el.innerText || "").split("\n").map(clean).find(Boolean);
  if (!label) label = el.getAttribute("title") || el.getAttribute("name");
  // An icon with nothing to say for itself acts on the row it sits in.
  if (!label) {
    const row = el.closest("tr, li, [role='row'], [role='treeitem']");
    const first = row && (row.querySelector("td, th") || row);
    if (first) label = (first.innerText || "").split("\n").map(clean).find(Boolean);
  }
  const r = el.getBoundingClientRect();
  const choices = field && field.tagName === "SELECT" ?
      [...field.options].slice(0, 300).map(o => [o.value, clean(o.text)]) : null;
  return {label: clean(label).slice(0, 60),
          secret: !!(field && field.type === "password"),
          choices: choices,
          box: [r.left, r.top, r.width, r.height]};
}"""


class PlaywrightAdapter(BrowserAdapter):
    def __init__(self, playwright, browser, page):
        self._pw = playwright
        self._browser = browser
        self._page = page
        self._console = []
        # Requests the page has in flight, for settle(). Odoo is a single-page
        # app: a click starts an RPC and a redraw, with no navigation for
        # Playwright's own load states to notice.
        self._in_flight = set()
        # Counts the page's own requests starting and ending: settle() tells a
        # quiet page from one that has just been busy by whether it moved.
        self._net_events = 0
        # doc_capture(): the elements of the steps already on the guide's page,
        # by selector, so they are measured again in one call, not one each.
        self._doc_handles = {}
        # Import here so the symbol is available for except-clauses without a
        # module-level Playwright import.
        from playwright.sync_api import TimeoutError as _Timeout
        self._Timeout = _Timeout
        page.on("console", self._on_console)
        page.on("request", self._on_request)
        page.on("requestfinished", self._on_request_done)
        page.on("requestfailed", self._on_request_done)

    # ------------------------------------------------------------------ setup
    @classmethod
    def connect(cls, endpoint, default_timeout_ms=DEFAULT_TIMEOUT_MS):
        """Attach to a running Chrome at ``endpoint`` (http://127.0.0.1:PORT).

        Grabs the first existing page across the browser's contexts - that is
        the launcher's already-open, already-logged-in app window.
        """
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        try:
            browser = pw.chromium.connect_over_cdp(endpoint)
        except Exception:
            pw.stop()
            raise
        page = cls._pick_page(browser)
        page.set_default_timeout(default_timeout_ms)
        log.debug("Attached over CDP at %s; driving page %s", endpoint, page.url)
        return cls(pw, browser, page)

    @staticmethod
    def _pick_page(browser):
        for context in browser.contexts:
            if context.pages:
                return context.pages[0]
        # No page open yet (unusual) - create one in the default context.
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        return context.new_page()

    def _on_console(self, msg):
        try:
            self._console.append("%s: %s" % (msg.type, msg.text))
        except Exception:  # never let logging break a run
            pass

    def _on_request(self, request):
        try:
            if request.resource_type in _LONG_LIVED_TYPES:
                return
            if any(part in request.url for part in _LONG_LIVED_PATHS):
                return
            self._in_flight.add(request)
            self._net_events += 1
        except Exception:  # never let bookkeeping break a run
            pass

    def _on_request_done(self, request):
        if request in self._in_flight:
            self._in_flight.discard(request)
            self._net_events += 1

    def settle(self, timeout_ms=3000):
        """Wait until the page has had no request in flight for a moment, then
        for two frames to be drawn - or until ``timeout_ms`` is up. Never raises:
        a screenshot taken a little early beats none at all.

        ``wait_for_load_state("networkidle")`` would not do: it answers for the
        document, and an Odoo click loads data into the same document, so that
        state was reached long before and returns at once.
        """
        try:
            page = self._page
            before = self._net_events
            page.wait_for_timeout(SETTLE_FIRST_MS)
            waited, quiet = SETTLE_FIRST_MS, 0
            if not self._in_flight and self._net_events == before:
                # Nothing started or ended through the first pause - which is
                # also when the events of the step itself arrive - so the page
                # is not busy, and the quiet spell would only be waited out.
                quiet = SETTLE_QUIET_MS
            while waited < timeout_ms and quiet < SETTLE_QUIET_MS:
                # Waiting is also what lets the sync API deliver request events.
                page.wait_for_timeout(SETTLE_POLL_MS)
                waited += SETTLE_POLL_MS
                quiet = quiet + SETTLE_POLL_MS if not self._in_flight else 0
            page.evaluate("() => new Promise(done => requestAnimationFrame("
                          "() => requestAnimationFrame(done)))")
        except Exception as exc:
            log.debug("settle gave up: %s", exc)

    # ---------------------------------------------------------------- actions
    def goto(self, url, timeout=None):
        self._page.goto(url, timeout=timeout)

    def fill(self, selector, value, timeout=None):
        self._page.fill(selector, value, timeout=timeout)

    def click(self, selector, timeout=None):
        self._page.click(selector, timeout=timeout)

    def select(self, selector, value, timeout=None):
        # By option VALUE, not label: values are field names (company_id), which are
        # identical in every UI language, while labels are translated.
        self._page.select_option(selector, value=value, timeout=timeout)

    def wait_for(self, selector, state="visible", timeout=None):
        self._page.wait_for_selector(selector, state=state, timeout=timeout)

    def press_key(self, key):
        # Press on whatever is currently focused (e.g. right after a click:).
        self._page.keyboard.press(key)

    # ---------------------------------------------------------------- queries
    def exists(self, selector, timeout=None):
        return self._present(selector, "attached", timeout)

    def visible(self, selector, timeout=None):
        return self._present(selector, "visible", timeout)

    def _present(self, selector, state, timeout):
        try:
            self._page.wait_for_selector(selector, state=state, timeout=timeout)
            return True
        except self._Timeout:
            return False

    def text(self, selector, timeout=None):
        try:
            value = self._page.text_content(selector, timeout=timeout)
        except self._Timeout:
            return ""
        return value or ""

    def url(self):
        return self._page.url

    def title(self):
        return self._page.title()

    # -------------------------------------------------------------- overlay
    # The HUD lives inside the page as an isolated, pointer-events:none Shadow
    # DOM (see engine/hud.js). Rendering must never break a run, so every call
    # swallows exceptions - a page that navigated mid-render, or a transient
    # evaluate error, just drops that frame.
    def overlay_setup(self, js_source):
        try:
            # add_init_script re-installs the renderer on every future document
            # (reloads / goto); evaluate installs it into the current one.
            self._page.add_init_script(js_source)
            self._page.evaluate(js_source)
        except Exception as exc:
            log.debug("overlay_setup failed: %s", exc)

    def overlay_render(self, state):
        """Paint ``state``; answers the tree version the page now holds, or None."""
        try:
            return self._page.evaluate(
                "s => window.__ExecHud ? window.__ExecHud.render(s) : null", state)
        except Exception as exc:
            log.debug("overlay_render failed: %s", exc)
            return None

    def overlay_mark(self, selector, label=None, timeout=None):
        """Flash the HUD's marker over the element a step is about to act on.

        ``selector`` None means "whatever currently has focus" - that is what a
        bare `press` acts on, and the focused element is otherwise invisible in a
        screenshot.

        Resolution goes through Playwright rather than querySelector because these
        selectors use Playwright's own syntax (``:nth-match``, ``:text-is``), which
        the browser cannot parse. Best-effort throughout: a marker that cannot be
        drawn must never fail the step it is decorating.
        """
        try:
            if selector is None:
                self._page.evaluate(
                    "label => window.__ExecHud && window.__ExecHud.mark(document.activeElement,"
                    " label)", label)
            else:
                self._page.locator(selector).first.evaluate(
                    "(el, label) => window.__ExecHud && window.__ExecHud.mark(el, label)",
                    label, timeout=timeout)
        except Exception as exc:
            log.debug("overlay_mark failed for %r: %s", selector, exc)

    def overlay_teardown(self):
        try:
            self._page.evaluate("() => window.__ExecHud && window.__ExecHud.remove()")
        except Exception as exc:
            log.debug("overlay_teardown failed: %s", exc)

    # -------------------------------------------------------------- recorder
    def recorder_setup(self, js_source):
        """Same two-step install as the overlay: this document, and every next one."""
        try:
            self._page.add_init_script(js_source)
            self._page.evaluate(js_source)
        except Exception as exc:
            log.debug("recorder_setup failed: %s", exc)

    def recorder_call(self, expression, argument=None):
        """Call into window.__Recorder, tolerating a page that has none yet.

        This is polled several times a second while a recording is idle, and the
        page can be navigating at any moment - so a failure here is normal and
        means "nothing to report", never an error worth stopping for.
        """
        try:
            return self._page.evaluate(
                # The name has to be `recorder`: that is what the callers' little
                # expressions are written against.
                "arg => { const recorder = window.__Recorder;"
                " return recorder ? (%s) : null; }" % expression, argument)
        except Exception as exc:
            log.debug("recorder_call failed: %s", exc)
            return None

    # ------------------------------------------------------------ diagnostics
    def screenshot(self, path):
        try:
            self._page.screenshot(path=path, full_page=True)
        except Exception:
            # full_page can fail on some layouts; fall back to the viewport.
            self._page.screenshot(path=path)

    def doc_capture(self, path, targets, focus=False):
        """The window as it is now, for the guide - see BrowserAdapter.doc_capture.

        The first target is the step's own: it is scrolled into view and given
        a moment to appear. The rest belong to earlier steps on the same page
        and are only looked for. Everything is measured before the picture is
        taken, against the same viewport, so the boxes land where the picture
        shows them. Best-effort: an element that cannot be found is None.
        """
        page = self._page
        targets = list(targets or [])
        found = []
        if targets:
            found.append(self._doc_element(page, targets[0], first=True, focus=focus))
            found.extend(self._doc_again(page, targets[1:]))
        state = page.evaluate(_DOC_PAGE_JS)
        try:
            page.screenshot(path=path, style=DOC_HIDE)
        except TypeError:
            # Playwright before 1.41 has no style=: a picture with the HUD in it
            # beats none.
            page.screenshot(path=path)
        width = float(state.get("width") or 0) or 1.0
        height = float(state.get("height") or 0) or 1.0
        elements = []
        for info in found:
            box = _fraction(info.get("box"), width, height) if info else None
            elements.append(None if box is None else
                            {"box": box, "label": info.get("label") or "",
                             "secret": bool(info.get("secret")),
                             "choices": info.get("choices") or []})
        return {"page": {"key": state.get("key") or "",
                         "title": state.get("title") or "",
                         "dialog": state.get("dialog") or "",
                         "size": [width, height]},
                "elements": elements}

    def _doc_element(self, page, selector, first, focus):
        """The step's own element: waited for, brought into view, measured."""
        try:
            if selector is None:
                if not (first and focus):
                    return None
                return page.evaluate(
                    "() => { const el = document.activeElement;"
                    " return el && el !== document.body ? (%s)(el) : null; }"
                    % _DOC_ELEMENT_JS)
            target = page.locator(selector).first
            target.wait_for(state="visible", timeout=DOC_TARGET_MS)
            target.scroll_into_view_if_needed(timeout=DOC_TARGET_MS)
            handle = target.element_handle(timeout=DOC_TARGET_MS)
            self._keep_handle(selector, handle)
            return handle.evaluate(_DOC_ELEMENT_JS)
        except Exception as exc:
            log.debug("doc_capture: %r not found: %s", selector, exc)
            return None

    def _doc_again(self, page, selectors):
        """The elements of earlier steps, measured again in one call.

        Each is looked up once - query_selector answers at once, with nothing
        when nothing matches - and kept; after that the page is asked about all
        of them together. One the page has drawn anew since is looked up again.
        Measured one call each, they cost every picture of a busy page a call
        per step already on it.
        """
        wanted = set(selectors)
        for selector in list(self._doc_handles):
            if selector not in wanted:
                self._drop_handle(selector)
        handles = []
        for selector in selectors:
            if selector is not None and selector not in self._doc_handles:
                self._keep_handle(selector, self._query(page, selector))
            handles.append(self._doc_handles.get(selector))
        measured = self._measure(page, handles)
        for position, selector in enumerate(selectors):
            if measured[position] is None and handles[position] is not None:
                # Drawn anew: the old element is gone, a new one may match.
                handle = self._query(page, selector)
                self._keep_handle(selector, handle)
                measured[position] = self._measure(page, [handle])[0]
        return measured

    @staticmethod
    def _query(page, selector):
        try:
            return page.query_selector(selector)
        except Exception as exc:
            log.debug("doc_capture: %r not found: %s", selector, exc)
            return None

    @staticmethod
    def _measure(page, handles):
        if not any(handle is not None for handle in handles):
            return [None] * len(handles)
        try:
            return page.evaluate(
                "handles => handles.map(el => el && el.isConnected ? (%s)(el) : null)"
                % _DOC_ELEMENT_JS, handles)
        except Exception as exc:
            log.debug("doc_capture: elements not measured: %s", exc)
            return [None] * len(handles)

    def _keep_handle(self, selector, handle):
        if self._doc_handles.get(selector) is not handle:
            self._drop_handle(selector)
        if handle is not None:
            self._doc_handles[selector] = handle

    def _drop_handle(self, selector):
        handle = self._doc_handles.pop(selector, None)
        if handle is not None:
            try:
                handle.dispose()
            except Exception:
                pass

    def content(self):
        return self._page.content()

    def console_logs(self):
        return list(self._console)

    def disconnect(self):
        # Only detach Playwright; do NOT close the browser. The launcher owns
        # the Chrome process and closes it gracefully (close_all) so the login
        # session is flushed to disk. Closing it here would look like a crash.
        try:
            self._pw.stop()
        except Exception:
            pass


def _fraction(box, width, height):
    """``[x, y, w, h]`` in CSS pixels as fractions of the window, or None when
    the element is not in it - hidden, or scrolled out of sight."""
    try:
        x, y, w, h = (float(part) for part in box)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0 or x + w <= 0 or y + h <= 0 or x >= width or y >= height:
        return None
    left, top = max(x, 0.0), max(y, 0.0)
    right, bottom = min(x + w, width), min(y + h, height)
    return [round(left / width, 4), round(top / height, 4),
            round((right - left) / width, 4), round((bottom - top) / height, 4)]
