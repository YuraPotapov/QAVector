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

**Shown as plain HTML.** Chrome draws an MHTML archive with every form control
greyed out, which reads as a different page. :func:`to_html` turns one into a
single self-contained HTML file instead - every picture, style and font built
in - which the HUD shows over the live page and the GUI shows in its own view.
It is made only when somebody actually goes to that step.
"""

import atexit
import base64
import email
import email.policy
import logging
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

# How deep a stylesheet's own imports, and a frame's own frames, are followed.
_MAX_DEPTH = 4

_CSS_URL = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""")
_CSS_IMPORT = re.compile(r"""@import\s+(['"])([^'"]+)\1""")
_ATTR_URL = re.compile(r"""\b(src|href)=(["'])([^"']+)\2""")
_STYLE_ELEMENT = re.compile(r"(<style\b[^>]*>)(.*?)(</style>)", re.S | re.I)
_STYLE_ATTR = re.compile(r"""\bstyle=(["'])(.*?)\1""", re.S)
_SCRIPT = re.compile(r"<script\b[^>]*>.*?</script>", re.S | re.I)
_CANVAS = re.compile(r"<canvas\b[^>]*>.*?</canvas>", re.S | re.I)
# What PlaywrightAdapter.page_copy writes a canvas's pixels into, for to_html.
PIXELS_ATTR = "data-qav-pixels"


def root(base=None):
    return os.path.join(base or tempfile.gettempdir(), ROOT_NAME)


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
        return ScenarioHistory(os.path.join(self.dir, *name.split("/")))

    def close(self):
        if self._closed:
            return
        self._closed = True
        _remove(self.dir)
        try:
            atexit.unregister(self.close)
        except Exception:
            pass


class ScenarioHistory:
    """The copies one scenario has left so far, by position."""

    def __init__(self, folder):
        self.dir = folder
        self._positions = set()
        os.makedirs(folder, exist_ok=True)

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

    def view(self, position):
        """The copy at ``position`` as a self-contained HTML file, made the first
        time somebody goes there. None when there is no copy of that step."""
        if position not in self._positions:
            return None
        target = self.path(position, "html")
        if os.path.exists(target):
            return target
        try:
            with open(self.path(position), "rb") as handle:
                html = to_html(handle.read())
            temporary = target + ".part"
            with open(temporary, "w", encoding="utf-8") as handle:
                handle.write(html)
            os.replace(temporary, target)
            return target
        except Exception as exc:
            log.warning("Could not show the page at step %d: %s", position, exc)
            return None

    def forget_from(self, position):
        """Drop the copies at ``position`` and after it: those steps are about to
        be taken again, so what they left the first time is no longer true."""
        for old in [p for p in self._positions if p >= position]:
            self._positions.discard(old)
            for ext in ("mhtml", "html"):
                try:
                    os.remove(self.path(old, ext))
                except OSError:
                    pass

    def close(self):
        self._positions.clear()
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


# -- MHTML to one HTML file ------------------------------------------------------
def to_html(raw):
    """Turn an MHTML archive (bytes) into one HTML document with nothing outside it.

    Every picture, stylesheet, font and frame the page used becomes a ``data:``
    address in place. Addresses inside a stylesheet are read against the
    stylesheet's own address, as a browser would - read against the page's, the
    pictures a stylesheet names were silently fetched from the live server
    instead of the copy. Scripts are dropped: the copy is to look at.
    """
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
    return _Inliner(by_address).page(pages[0], 0)


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


class _Inliner:
    def __init__(self, by_address):
        self._by = by_address
        self._done = {}

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
        # would read them; a data: address needs none.
        html = _STYLE_ATTR.sub(
            lambda m: "style=%s%s%s" % (
                m.group(1), self._css(m.group(2).replace("&quot;", "'"), base, depth),
                m.group(1)), html)
        html = _ATTR_URL.sub(lambda m: "%s=%s%s%s" % (
            m.group(1), m.group(2), self._inline(m.group(3), base, depth) or m.group(3),
            m.group(2)), html)
        return html

    def _css(self, text, base, depth):
        text = _CSS_IMPORT.sub(lambda m: '@import url("%s")' % m.group(2), text)
        return _CSS_URL.sub(lambda m: "url(%s)" % (
            self._inline(m.group(2), base, depth) or m.group(2)), text)

    def _inline(self, ref, base, depth):
        ref = ref.strip()
        if ref.startswith(("data:", "#", "javascript:", "about:")):
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
        if part["type"] == "text/css":
            body = self._css(body.decode(part["charset"], "replace"), own,
                             depth + 1).encode("utf-8")
        elif part["type"] == "text/html":
            body = self.page(part, depth + 1).encode("utf-8")
        uri = "data:%s;base64,%s" % (part["type"], base64.b64encode(body).decode("ascii"))
        self._done[address] = uri
        return uri


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
