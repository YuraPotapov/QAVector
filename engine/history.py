"""Live history: a copy of the page after every step, kept while the run lives.

A paused scenario can be stepped back over: the person sees the page as it was
after an earlier step, while the live page stays where the run left it. That
needs the page itself, not a picture of it and not its address - an Odoo view
is generated, and opening its URL again does not bring back what was on it.

**What is kept.** Chrome's own copy of the page (``Page.captureSnapshot``, an
MHTML archive), one per position: ``step_000`` before the first step, then
``step_NNN`` after step NNN. The adapter takes it - see
``PlaywrightAdapter.page_copy`` for what it does first, because Chrome's copy
leaves out what was typed into a form.

**Only while the run lives.** Nothing here goes into the report. A scenario's
copies are deleted when it ends - nobody can step back into it after that - and
the whole folder when the run does. A run that was killed leaves its folder
behind, and the next run deletes it (:func:`sweep`). Deleting even a large
history takes a fraction of a second, so none of this is noticed.

**Every resource once.** Chrome draws an MHTML archive with every form control
greyed out, which reads as a different page, so a copy is shown as HTML
instead. Its pictures, fonts, stylesheets and frames are named by the hash of
what they hold and kept once for the whole scenario (``res/``), each step's
page only naming them. Built into every place that names them, as they first
were, one Odoo page grew from 4 MB to 43 MB - a stylesheet names one picture a
thousand times - and stepping back took the best part of a minute. The HUD in
the window is sent each resource once too, and keeps it (engine/hud.js).

A copy is turned into HTML the first time somebody goes to it, or a moment
before: while a scenario is paused the steps just before the cursor are made
ready in the background (:meth:`ScenarioHistory.prepare`).
"""

import atexit
import collections
import concurrent.futures
import email
import email.policy
import hashlib
import logging
import mimetypes
import os
import re
import shutil
import tempfile
import threading
import time
from urllib.parse import urljoin

log = logging.getLogger("flowengine.history")

#: The folder under the system's temporary directory every run's history is in.
ROOT_NAME = "qavector-history"

#: How a copy's page names one of its resources until it is shown somewhere:
#: ``qavres:`` and the hash of what the resource holds.
REF = "qavres:"
REF_PATTERN = re.compile(r"qavres:([0-9a-f]{40})")

#: Resources whose own text names other resources.
TEXT_TYPES = ("text/css", "text/html")

# How deep a stylesheet's own imports, and a frame's own frames, are followed.
_MAX_DEPTH = 4

_CSS_URL = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""")
_CSS_IMPORT = re.compile(r"""@import\s+(['"])([^'"]+)\1""")
_ATTR_URL = re.compile(r"""\b(src|href)=(["'])([^"']+)\2""")
_STYLE_ELEMENT = re.compile(r"(<style\b[^>]*>)(.*?)(</style>)", re.S | re.I)
_STYLE_ATTR = re.compile(r"""\bstyle=(["'])(.*?)\1""", re.S)
_SCRIPT = re.compile(r"<script\b[^>]*>.*?</script>", re.S | re.I)
_CANVAS = re.compile(r"<canvas\b[^>]*>.*?</canvas>", re.S | re.I)
# The HUD is in the page while it is copied - taking it out for the copy made
# it blink away for the length of every one - so it is cut out of the copy here.
# Its host holds a shadow root and nothing else.
_HUD = re.compile(r'<div id="__exec_hud_host__"[^>]*>\s*(?:<template\b[^>]*>.*?</template>)?'
                  r'\s*</div>', re.S)
# What PlaywrightAdapter.page_copy writes a canvas's pixels into.
PIXELS_ATTR = "data-qav-pixels"

# How many converted pages a scenario keeps in memory; the rest are re-read.
_KEPT = 8

_EXT = {"text/css": ".css", "text/html": ".html", "image/svg+xml": ".svg",
        "font/woff2": ".woff2", "font/woff": ".woff", "application/font-woff": ".woff",
        "font/ttf": ".ttf", "image/x-icon": ".ico"}

# The views made so far, by path: what the HUD is sent for one (see lookup).
_views = {}
_views_lock = threading.Lock()


def root(base=None):
    return os.path.join(base or tempfile.gettempdir(), ROOT_NAME)


def lookup(view):
    """``(html, [(hash, type, body)])`` for a view :meth:`ScenarioHistory.view`
    made - the page and, in order, everything it names - or None."""
    with _views_lock:
        found = _views.get(view)
    if found is None:
        return None
    history, position = found
    return history.payload(position)


# -- the folders ---------------------------------------------------------------
class RunHistory:
    """One run's history: a folder of its own, gone when the run is.

    Named after this process, so :func:`sweep` can tell a folder whose run is
    still going from one whose run was killed.
    """

    def __init__(self, base=None):
        self.dir = os.path.join(root(base), "%d-%d" % (os.getpid(), int(time.time() * 1000)))
        self._lock = threading.Lock()
        self._names = set()
        self._scenarios = []
        self._closed = False
        os.makedirs(self.dir, exist_ok=True)
        # A run that ends in sys.exit or an exception still tidies up; one that is
        # killed outright is the next run's sweep.
        atexit.register(self.close)

    def scenario(self, session, scenario):
        """A folder for one scenario in one window. A scenario a window runs
        twice gets two, so the second run never finds the first one's copies."""
        base = "%s/%s" % (_safe(session), _safe(scenario))
        with self._lock:
            name, n = base, 1
            while name in self._names:
                n += 1
                name = "%s-%d" % (base, n)
            self._names.add(name)
            scenario = ScenarioHistory(os.path.join(self.dir, *name.split("/")))
            self._scenarios.append(scenario)
        return scenario

    def close(self):
        if self._closed:
            return
        self._closed = True
        for scenario in self._scenarios:
            scenario.close()
        _remove(self.dir)
        try:
            atexit.unregister(self.close)
        except Exception:
            pass


class ScenarioHistory:
    """The copies one scenario has left so far, by position.

    Its resources are kept once, for every step: in memory by hash, and on disk
    in ``res/`` for the GUI, which reads a step's page from a file.
    """

    def __init__(self, folder):
        self.dir = folder
        self._positions = set()
        self._resources = {}                       # hash -> (type, body)
        self._written = set()                      # hashes in res/ already
        self._pages = collections.OrderedDict()    # position -> (html, [hash])
        self._lock = threading.Lock()
        self._making = {}                          # position -> its Lock
        self._worker = None
        self._closed = False
        os.makedirs(os.path.join(folder, "res"), exist_ok=True)

    def path(self, position, ext="mhtml"):
        return os.path.join(self.dir, "step_%03d.%s" % (position, ext))

    def capture(self, adapter, position):
        """Copy the page as it is now. False when there is no page to copy -
        a run without a browser, or a page in the middle of going away."""
        self.forget_from(position)
        try:
            taken = bool(adapter.page_copy(self.path(position)))
        except Exception as exc:              # a copy must never break a run
            log.debug("no copy of step %d: %s", position, exc)
            taken = False
        if taken:
            self._positions.add(position)
        return taken

    def has(self, position):
        return position in self._positions

    def same_as(self, position, earlier):
        """``position`` left the page as ``earlier`` did - a check, a wait - so
        it shares that copy instead of taking one: about a second saved on a
        heavy page, for a picture that would be the same."""
        self.forget_from(position)
        if earlier not in self._positions:
            return False
        try:
            shutil.copyfile(self.path(earlier), self.path(position))
        except OSError:
            return False
        self._positions.add(position)
        return True

    # -- showing one ---------------------------------------------------------------
    def view(self, position):
        """The copy at ``position`` as an HTML file naming its resources in
        ``res/``, made the first time somebody goes there - or ready already,
        when :meth:`prepare` got to it. None when there is no copy of that step."""
        if self._make(position) is None:
            return None
        target = self.path(position, "html")
        with _views_lock:
            _views[target] = (self, position)
        return target

    def payload(self, position):
        """``(html, [(hash, type, body)])`` - the page, and everything it names
        in the order the HUD has to make them (what a resource names first)."""
        page = self._make(position)
        if page is None:
            return None
        html, names = page
        return html, [(name,) + self._resources[name] for name in names]

    def prepare(self, positions):
        """Make these ready in the background, in this order: the steps somebody
        is likely to go to next, while the scenario waits for them."""
        if self._closed:
            return
        if self._worker is None:
            self._worker = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="history")
        for position in positions:
            if position in self._positions and position not in self._pages:
                try:
                    self._worker.submit(self._make, position)
                except RuntimeError:              # shut down meanwhile
                    return

    def _make(self, position):
        if position not in self._positions:
            return None
        with self._lock:
            page = self._pages.get(position)
            if page is not None:
                self._pages.move_to_end(position)
                return page
            making = self._making.setdefault(position, threading.Lock())
        with making:
            with self._lock:
                page = self._pages.get(position)
            if page is not None:
                return page
            try:
                with open(self.path(position), "rb") as handle:
                    html, names = convert(handle.read(), self._resources)
                self._write(position, html, names)
            except Exception as exc:
                if not self._closed:
                    log.warning("Could not show the page at step %d: %s", position, exc)
                return None
            with self._lock:
                self._pages[position] = (html, names)
                while len(self._pages) > _KEPT:
                    self._pages.popitem(last=False)
            return html, names

    def _write(self, position, html, names):
        """The step's page for the GUI, and whichever of its resources are not
        on disk yet."""
        files = {name: name + _extension(self._resources[name][0]) for name in names}
        for name in names:
            if name in self._written:
                continue
            kind, body = self._resources[name]
            if kind in TEXT_TYPES:
                body = REF_PATTERN.sub(lambda m: files.get(m.group(1), m.group(0)),
                                       body.decode("utf-8")).encode("utf-8")
            _atomic(os.path.join(self.dir, "res", files[name]), body)
            self._written.add(name)
        page = REF_PATTERN.sub(lambda m: "res/" + files.get(m.group(1), m.group(1)), html)
        _atomic(self.path(position, "html"), page.encode("utf-8"))

    # -- forgetting ----------------------------------------------------------------
    def forget_from(self, position):
        """Drop the copies at ``position`` and after it: those steps are about to
        be taken again, so what they left the first time is no longer true. The
        resources stay - the next copies will name most of them again."""
        for old in [p for p in self._positions if p >= position]:
            self._positions.discard(old)
            with self._lock:
                self._pages.pop(old, None)
            for ext in ("mhtml", "html"):
                try:
                    os.remove(self.path(old, ext))
                except OSError:
                    pass

    def close(self):
        self._closed = True
        if self._worker is not None:
            self._worker.shutdown(wait=False, cancel_futures=True)
        with _views_lock:
            for view in [one for one, (owner, _p) in _views.items() if owner is self]:
                del _views[view]
        self._positions.clear()
        self._pages.clear()
        _remove(self.dir)


def sweep(base=None):
    """Delete what runs that were killed left behind. Answers how many."""
    folder = root(base)
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    removed = 0
    for name in names:
        pid = name.split("-", 1)[0]
        if not pid.isdigit() or int(pid) == os.getpid() or _pid_alive(int(pid)):
            continue
        _remove(os.path.join(folder, name))
        removed += 1
    return removed


def _remove(path):
    shutil.rmtree(path, ignore_errors=True)


def _atomic(path, data):
    # A temporary of its own per thread: the background worker and the session
    # can both be writing a resource two pages share.
    temporary = "%s.%d.part" % (path, threading.get_ident())
    with open(temporary, "wb") as handle:
        handle.write(data)
    os.replace(temporary, path)


def _extension(kind):
    return _EXT.get(kind) or mimetypes.guess_extension(kind or "") or ".bin"


def _safe(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "")).strip("._") or "_"


def _pid_alive(pid):
    """Whether ``pid`` is a running process. Errs towards yes: a folder kept one
    run too long costs some disk, one deleted under a live run costs its history."""
    if os.name == "nt":
        # os.kill(pid, 0) would END the process on Windows: ask the kernel instead.
        try:
            import ctypes

            synchronize, wait_timeout = 0x00100000, 0x00000102
            handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
            if not handle:
                return False
            try:
                return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == wait_timeout
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


# -- MHTML to a page and its resources -------------------------------------------
def convert(raw, store=None):
    """Read an MHTML archive (bytes) into ``(html, names)``.

    ``html`` is the page with every picture, stylesheet, font and frame it uses
    named ``qavres:<hash>``; ``names`` lists those hashes, each after whatever it
    names itself, and ``store`` (a dict, kept across copies) gets ``hash ->
    (type, body)`` for each. Addresses inside a stylesheet are read against the
    stylesheet's own address, as a browser would. Scripts are dropped - the copy
    is to look at - and so is the HUD.
    """
    store = {} if store is None else store
    parts = _parts(raw)
    pages = [part for part in parts if part["type"] == "text/html"]
    if not pages:
        raise ValueError("no page in this copy")
    by_address = {}
    for part in parts:
        if part["location"]:
            by_address.setdefault(part["location"], part)
        if part["cid"]:
            by_address.setdefault("cid:" + part["cid"], part)
    collector = _Collector(by_address, store)
    html = _HUD.sub("", collector.page(pages[0], 0))
    return html, list(collector.names)


def _parts(raw):
    message = email.message_from_bytes(raw, policy=email.policy.compat32)
    out = []
    for part in message.walk():
        if part.is_multipart():
            continue
        out.append({"location": (part.get("Content-Location") or "").strip(),
                    "cid": (part.get("Content-ID") or "").strip().strip("<>"),
                    "type": part.get_content_type(),
                    "body": part.get_payload(decode=True) or b"",
                    "charset": part.get_content_charset() or "utf-8"})
    return out


class _Collector:
    def __init__(self, by_address, store):
        self._by = by_address
        self._store = store
        self._done = {}
        self.names = collections.OrderedDict()   # hash -> None, in the order made

    def page(self, part, depth):
        html = part["body"].decode(part["charset"], "replace")
        base = part["location"]
        html = _SCRIPT.sub("", html)
        html = _CANVAS.sub(_canvas_picture, html)
        # Chrome writes an open shadow root as <template shadowmode>, which only
        # its own MHTML loader reads; the standard spelling is shadowrootmode.
        html = re.sub(r"<template(\s+)shadowmode=", r"<template\1shadowrootmode=", html)
        html = _STYLE_ELEMENT.sub(
            lambda m: m.group(1) + self._css(m.group(2), base, depth) + m.group(3), html)
        # A style attribute's own quotes are written &quot; - as quotes, CSS
        # would read them; a reference needs none.
        html = _STYLE_ATTR.sub(
            lambda m: "style=%s%s%s" % (
                m.group(1), self._css(m.group(2).replace("&quot;", "'"), base, depth),
                m.group(1)), html)
        html = _ATTR_URL.sub(lambda m: "%s=%s%s%s" % (
            m.group(1), m.group(2), self._ref(m.group(3), base, depth) or m.group(3),
            m.group(2)), html)
        return html

    def _css(self, text, base, depth):
        text = _CSS_IMPORT.sub(lambda m: '@import url("%s")' % m.group(2), text)
        return _CSS_URL.sub(lambda m: "url(%s)" % (
            self._ref(m.group(2), base, depth) or m.group(2)), text)

    def _ref(self, ref, base, depth):
        ref = ref.strip()
        if ref.startswith(("data:", "#", "javascript:", "about:", REF)):
            return None
        address = ref if ref.startswith("cid:") or not base else urljoin(base, ref)
        if address in self._done:
            return self._done[address]
        part = self._by.get(address)
        if part is None or depth >= _MAX_DEPTH:
            return None
        self._done[address] = None            # a stylesheet that imports itself
        body = part["body"]
        own = part["location"] or base
        kind = part["type"]
        if kind == "text/css":
            body = self._css(body.decode(part["charset"], "replace"), own,
                             depth + 1).encode("utf-8")
        elif kind == "text/html":
            body = _HUD.sub("", self.page(part, depth + 1)).encode("utf-8")
        name = hashlib.sha1(kind.encode("ascii", "replace") + b"\0" + body).hexdigest()
        self._store.setdefault(name, (kind, body))
        self.names[name] = None
        self._done[address] = REF + name
        return REF + name


def _canvas_picture(match):
    """A canvas's pixels are not in Chrome's copy: page_copy put them in an
    attribute, and here the canvas becomes the picture of itself."""
    tag = match.group(0)
    pixels = re.search(r'%s="(data:image/[^"]+)"' % PIXELS_ATTR, tag)
    if not pixels:
        return tag
    size = ""
    for name in ("width", "height"):
        found = re.search(r'\b%s="(\d+)"' % name, tag)
        if found:
            size += ' %s="%s"' % (name, found.group(1))
    style = re.search(r'\bstyle="([^"]*)"', tag)
    return '<img src="%s"%s%s>' % (pixels.group(1), size,
                                   ' style="%s"' % style.group(1) if style else "")
