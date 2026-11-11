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

**A step that changed nothing has no copy of its own.** A check or a wait
leaves the page as the step before it did, so its position names that step's
copy (:meth:`ScenarioHistory.same_as`): nothing is written for it, and going
back over it is the same page already on show, not a second one to make, send
and draw.

**What an archive holds again is not read again.** A stylesheet is most of an
archive and the same in every copy of a page, so what each part became is
remembered by the part as archived (:func:`convert`'s ``known``): the next
copy's stylesheet is recognised by its hash, not decoded and searched again.

**Most copies are light.** Chrome writes every stylesheet out again for every
archive - a third of a second of a heavy page's step, for megabytes that are
the same as a step ago. So a full archive is taken once for a document, and
after it a step leaves only its DOM (``step_NNN.json``, a tenth of the size and
of the time), read against that archive's stylesheets, fonts and pictures
(:func:`convert_light`). The page says when that would not be true - a new
document, a frame, a stylesheet changed by script - and the adapter when
something was fetched that no DOM names; then, and whenever a light copy
shows a picture the archive does not hold, the step gets a full archive of
its own, which the steps after it are read against
(``PlaywrightAdapter.page_light_copy``, :meth:`ScenarioHistory.capture`).

**What Chrome's archive lacks is put into it.** It holds no fonts - a page
whose icons are a font was copied without one of them - and its stylesheets
are written back out of Chrome's parsed rules, which loses a rule that sets a
shorthand through a variable. So the adapter has the page read its fonts and
its stylesheets, as the server sent them, from its own cache, and adds them to
the archive as parts marked ``X-Qav-As: served``; such a part is taken over
Chrome's of the same address (:func:`_by_address`).

**Where the page was scrolled to** is written on the copy too
(``SCROLL_ATTR``): a copy runs no script and is drawn from the top, so the HUD
and the Run page put each scrolled thing back once the copy is laid out.
"""

import atexit
import binascii
import collections
import concurrent.futures
import email
import email.policy
import hashlib
import json
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
# And how far an element was scrolled, as "top,left": a copy is drawn from the
# top, so whoever shows one puts these back (engine/hud.js, the Run page).
SCROLL_ATTR = "data-qav-scroll"
# The header of an archive part the adapter read from the page itself - a
# font, a stylesheet as the server sent it - with the value "served".
SERVED_HEADER = "X-Qav-As"

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
        # A position that shares an earlier one's copy -> that earlier position.
        self._shared = {}
        # A position with a copy of its own -> which taking of it this is: a
        # step run again is copied again, and a page still being made from the
        # first copy must not be kept as the second (see _make).
        self._stamps = {}
        self._taken = 0
        # A position whose copy is light -> (the position of the full copy it
        # is read against, that copy's stamp).
        self._lights = {}
        # The full copy the next light ones are read against: its position,
        # and what the page said of itself then - None when there is none, or
        # the adapter takes no light copies.
        self._base = None
        self._base_facts = None
        self._bases = collections.OrderedDict()    # (position, stamp) -> its parts
        self._bases_lock = threading.Lock()
        #: How many copies of each kind were taken, and why the full ones were.
        self.kinds = collections.Counter()
        self.reasons = collections.Counter()
        self._resources = {}                       # hash -> (type, body)
        self._known = {}                           # see convert()
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
        a run without a browser, or a page in the middle of going away.

        A light copy when the page, the adapter and the last full copy all
        say it would be true (see the module); a full one otherwise, which
        the copies after it are then read against.
        """
        self.forget_from(position)
        facts, why = self._ask(adapter), ""
        if facts is not None:
            why = self._light(position, facts)
            if not why:
                self.kinds["light"] += 1
                return True
        try:
            taken = bool(adapter.page_copy(self.path(position)))
        except Exception as exc:              # a copy must never break a run
            log.debug("no copy of step %d: %s", position, exc)
            taken = False
        if taken:
            with self._lock:
                self._taken += 1
                self._stamps[position] = self._taken
                self._positions.add(position)
                # What the page said a moment before the archive was taken is
                # what the archive holds: nothing ran in between.
                self._base = position if facts else None
                self._base_facts = ({"doc": facts.get("doc"), "linked": facts.get("linked")}
                                    if facts else None)
            self.kinds["full"] += 1
            if why:
                self.reasons[why] += 1
                log.debug("step %d: a full copy (%s)", position, why)
        return taken

    @staticmethod
    def _reason(why):
        """A reason without its numbers, to count the same ones together."""
        return re.sub(r"^\d+ ", "", why or "")

    def _ask(self, adapter):
        """What the page says a light copy of it would be made of, or None -
        an adapter that takes none, a page that cannot be asked."""
        ask = getattr(adapter, "page_light_copy", None)
        if ask is None:
            return None
        try:
            facts = ask(self._base_facts)
        except Exception as exc:
            log.debug("the page was not asked for a light copy: %s", exc)
            return None
        return facts if isinstance(facts, dict) and facts.get("doc") else None

    def _light(self, position, facts):
        """Keep ``facts`` as the light copy at ``position``. Answers "" when
        kept, otherwise why a full copy has to be taken instead."""
        if not facts.get("html"):
            return self._reason(facts.get("why")) or "the page did not say"
        with self._lock:
            base = self._base
            stamp = self._stamps.get(base)
        if base is None or stamp is None:
            return "no full copy to read it against"
        try:
            held = self._addresses(base, stamp)
            # Every picture the page shows has to be in the archive: one that
            # is not would be asked of the server when the copy is shown - or
            # be a hole in it.
            if any(address.split("#", 1)[0] not in held for address in facts.get("uses") or ()):
                return "a picture the full copy does not hold"
            _atomic(self.path(position, "json"), json.dumps(
                {"html": facts["html"], "url": facts.get("url") or "",
                 "dynamic": facts.get("dynamic") or {},
                 "adopted": facts.get("adopted") or []}).encode("utf-8"))
        except Exception as exc:
            log.debug("no light copy of step %d: %s", position, exc)
            return "it could not be kept"
        with self._lock:
            self._taken += 1
            self._stamps[position] = self._taken
            self._lights[position] = (base, stamp)
            self._positions.add(position)
        return ""

    def _addresses(self, position, stamp):
        """The parts of the full copy at ``position``, by address. The last
        two are kept: the one copies are being taken against, and one somebody
        may be stepping back over from before it."""
        key = (position, stamp)
        with self._bases_lock:
            held = self._bases.get(key)
            if held is not None:
                self._bases.move_to_end(key)
                return held
        with open(self.path(position), "rb") as handle:
            held = _by_address(_parts(handle.read()))
        with self._bases_lock:
            self._bases[key] = held
            while len(self._bases) > 2:
                self._bases.popitem(last=False)
        return held

    def has(self, position):
        return position in self._positions

    def same_as(self, position, earlier):
        """``position`` left the page as ``earlier`` did - a check, a wait - so
        it names that copy instead of taking one: about a second saved on a
        heavy page, and nothing written, for a picture that would be the same.
        Going from one to the other is then the page already on show."""
        self.forget_from(position)
        with self._lock:
            if earlier not in self._positions:
                return False
            self._shared[position] = self._shared.get(earlier, earlier)
            self._positions.add(position)
        return True

    def _own(self, position):
        """The position whose copy ``position`` shows: itself, unless it shares."""
        return self._shared.get(position, position)

    # -- showing one ---------------------------------------------------------------
    def view(self, position):
        """The copy at ``position`` as an HTML file naming its resources in
        ``res/``, made the first time somebody goes there - or ready already,
        when :meth:`prepare` got to it. None when there is no copy of that step.
        Positions that share a copy answer the same file."""
        if position not in self._positions:
            return None
        own = self._own(position)
        if self._make(own) is None:
            return None
        target = self.path(own, "html")
        with _views_lock:
            _views[target] = (self, own)
        return target

    def payload(self, position):
        """``(html, [(hash, type, body)])`` - the page, and everything it names
        in the order the HUD has to make them (what a resource names first)."""
        page = self._make(self._own(position))
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
        asked = set()
        for position in positions:
            if position not in self._positions:
                continue
            own = self._own(position)
            if own in asked or own in self._pages:
                continue
            asked.add(own)
            try:
                self._worker.submit(self._make, own)
            except RuntimeError:              # shut down meanwhile
                return

    def _make(self, position):
        """The page of the copy ``position`` has of its own, as ``(html,
        [hash])``. Made by the session's thread or by the worker, whichever
        gets there first; the other waits for it."""
        with self._lock:
            page = self._pages.get(position)
            if page is not None:
                self._pages.move_to_end(position)
                return page
            if position not in self._stamps:
                return None
            making = self._making.setdefault(position, threading.Lock())
        with making:
            with self._lock:
                page = self._pages.get(position)
                stamp = self._stamps.get(position)
                light = self._lights.get(position)
            if page is not None:
                return page
            if stamp is None:
                return None
            try:
                if light is not None:
                    with open(self.path(position, "json"), encoding="utf-8") as handle:
                        html, names = convert_light(json.load(handle), self._addresses(*light),
                                                    self._resources, self._known)
                else:
                    with open(self.path(position), "rb") as handle:
                        html, names = convert(handle.read(), self._resources, self._known)
            except Exception as exc:
                if not self._closed:
                    log.warning("Could not show the page at step %d: %s", position, exc)
                return None
            # Kept only if the copy is still the one that was read: while the
            # worker made this page the scenario may have gone on from an
            # earlier step and taken this one again, and what the first taking
            # left is no longer the page after this step. Written under the
            # lock forget_from takes, so the two cannot cross.
            with self._lock:
                if self._stamps.get(position) != stamp:
                    return None
                try:
                    self._write(position, html, names)
                except Exception as exc:
                    if not self._closed:
                        log.warning("Could not show the page at step %d: %s", position, exc)
                    return None
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
        with self._lock:
            for old in [p for p in self._positions if p >= position]:
                self._positions.discard(old)
                # A position that shared a copy had no files: the copy is an
                # earlier position's, which is either kept or dropped itself.
                if self._shared.pop(old, None) is not None:
                    continue
                self._pages.pop(old, None)
                self._stamps.pop(old, None)
                # A light copy goes with its step; the full copy it was read
                # against is an earlier position, kept or dropped itself - and
                # dropped, it is nothing to read the next copies against: the
                # step taken next gets a full one.
                self._lights.pop(old, None)
                if self._base == old:
                    self._base = self._base_facts = None
                for ext in ("mhtml", "html", "json"):
                    try:
                        os.remove(self.path(old, ext))
                    except OSError:
                        pass

    def close(self):
        said, self._closed = self._closed, True      # closed twice: by its gate, by its run
        if self._worker is not None:
            self._worker.shutdown(wait=False, cancel_futures=True)
        with _views_lock:
            for view in [one for one, (owner, _p) in _views.items() if owner is self]:
                del _views[view]
        with self._lock:
            self._positions.clear()
            self._shared.clear()
            self._stamps.clear()
            self._lights.clear()
            self._base = self._base_facts = None
            self._pages.clear()
        with self._bases_lock:
            self._bases.clear()
        if self.kinds and not said:
            log.debug("history of %s: %d light and %d full copies%s",
                      os.path.basename(self.dir), self.kinds["light"], self.kinds["full"],
                      "".join("; %d for %s" % (count, why)
                              for why, count in self.reasons.most_common()))
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
def convert(raw, store=None, known=None):
    """Read an MHTML archive (bytes) into ``(html, names)``.

    ``html`` is the page with every picture, stylesheet, font and frame it uses
    named ``qavres:<hash>``; ``names`` lists those hashes, each after whatever it
    names itself, and ``store`` (a dict, kept across copies) gets ``hash ->
    (type, body)`` for each. Addresses inside a stylesheet are read against the
    stylesheet's own address, as a browser would. Scripts are dropped - the copy
    is to look at - and so is the HUD.

    ``known`` (a dict, kept across copies with ``store``) is what earlier
    archives' parts became: a part archived byte for byte as before, naming
    what it named before, is not decoded and searched a second time.
    """
    store = {} if store is None else store
    parts = _parts(raw)
    pages = [part for part in parts if part["type"] == "text/html"]
    if not pages:
        raise ValueError("no page in this copy")
    collector = _Collector(_by_address(parts), store, known)
    html = _HUD.sub("", collector.page(pages[0], 0))
    return html, list(collector.names)


def _by_address(parts):
    """An archive's parts by what a page names them: address, or ``cid:``.

    A part the adapter read from the page itself (``SERVED_HEADER``) is taken
    over Chrome's of the same address: a stylesheet as the server sent it,
    where Chrome's is written back out of its parsed rules and loses some.
    """
    held = {}
    for part in parts:
        if part["location"] and (part.get("served") or part["location"] not in held):
            held[part["location"]] = part
        if part["cid"]:
            held.setdefault("cid:" + part["cid"], part)
    return held


# A <style> whose rules were put in by script: its text is not its rules, so
# PlaywrightAdapter.page_light_copy sends them beside the DOM, by this number.
_DYNAMIC_STYLE = re.compile(
    r'(<style\b[^>]*?)\sdata-qav-style="(\d+)"([^>]*>)(.*?)(</style>)', re.S | re.I)
# What a DOM holds and a copy must not: a picture chosen from a srcset is one
# the archive may not hold; <noscript> is text while scripts run and elements
# in a copy, where they do not; a policy written into the page would forbid
# the copy its own resources.
_SRCSET = re.compile(r"""\s(?:srcset|imagesrcset)=(["']).*?\1""", re.S | re.I)
_NOSCRIPT = re.compile(r"<noscript\b[^>]*>.*?</noscript>", re.S | re.I)
_POLICY = re.compile(r"""<meta\b[^>]*http-equiv=["']?content-security-policy[^>]*>""",
                     re.S | re.I)


def convert_light(light, parts, store=None, known=None):
    """Read a light copy into ``(html, names)``, as :func:`convert` reads an
    archive.

    ``light`` is what ``PlaywrightAdapter.page_light_copy`` answered - the
    DOM (``html``), the address it was at (``url``), the rules of the styles
    script filled in (``dynamic``) and of the stylesheets the document adopted
    (``adopted``) - and ``parts`` the parts, by address, of the full copy
    taken of the same document before it: what the DOM names is looked for
    there, exactly as an archive's own page names its own parts.
    """
    store = {} if store is None else store
    dynamic = light.get("dynamic") or {}
    html = _DYNAMIC_STYLE.sub(
        lambda m: m.group(1) + m.group(3) + dynamic.get(m.group(2), m.group(4)) + m.group(5),
        light["html"])
    html = _POLICY.sub("", _NOSCRIPT.sub("", _SRCSET.sub("", html)))
    adopted = "".join("<style>%s</style>" % text for text in light.get("adopted") or ())
    if adopted:
        # After everything else in the page, as adopted stylesheets come
        # after the document's own.
        cut = html.rfind("</body>")
        html = html[:cut] + adopted + html[cut:] if cut >= 0 else html + adopted
    page = {"location": light.get("url") or "", "cid": "", "type": "text/html",
            "charset": "utf-8", "body": html.encode("utf-8")}
    collector = _Collector(parts, store, known)
    return _HUD.sub("", collector.page(page, 0)), list(collector.names)


_BOUNDARY = re.compile(rb'boundary="?([^";\r\n]+)"?', re.I)
_CHARSET = re.compile(r'charset="?([^";\s]+)', re.I)


def _parts(raw):
    """The archive's parts: ``{"location", "cid", "type", "charset"}`` and
    either ``"raw"`` with its ``"encoding"`` - the part as archived, decoded by
    :func:`_body` only when somebody needs it - or ``"body"``, decoded already.

    Chrome's archive is read directly: the standard library's parser took most
    of the time a copy took to show, building a message out of megabytes of
    stylesheet that is the same in every copy. Anything not shaped like
    Chrome's is read by that parser, as before.
    """
    try:
        parts = _split(raw)
    except Exception as exc:
        log.debug("archive not read directly: %s", exc)
        parts = None
    return parts or _parts_by_email(raw)


def _split(raw):
    head = raw.partition(b"\r\n\r\n")[0]
    found = _BOUNDARY.search(head)
    if not found:
        return None
    # A boundary line stands alone, and the line end before it is its own.
    # Split on the bytes and each cut checked after: a pattern that says the
    # same took most of the time the whole archive took to read.
    mark = b"\r\n--" + found.group(1)
    pieces, last = [], False
    for cut in (b"\r\n" + raw).split(mark):
        if last:
            break
        if not pieces:
            pieces.append(cut)                    # what comes before the first part
        elif cut.startswith(b"\r\n"):
            pieces.append(cut[2:])
        elif cut.startswith(b"--") and cut[2:].lstrip(b" \t")[:2] in (b"\r\n", b""):
            last = True                           # the closing line
        else:
            pieces[-1] += mark + cut              # the same letters inside a part
    out = []
    for piece in pieces[1:]:
        head, separator, body = piece.partition(b"\r\n\r\n")
        if not separator:
            head, body = piece, b""
        headers, name = {}, None
        for text in head.decode("latin-1").split("\r\n"):
            if text[:1] in (" ", "\t") and name:          # a header going on
                headers[name] += " " + text.strip()
                continue
            name, colon, value = text.partition(":")
            if not colon:
                name = None
                continue
            name = name.strip().lower()
            headers[name] = value.strip()
        if not headers:
            continue
        kind, _semicolon, params = headers.get("content-type", "text/plain").partition(";")
        charset = _CHARSET.search(params)
        out.append({"location": headers.get("content-location", ""),
                    "cid": headers.get("content-id", "").strip("<>"),
                    "type": kind.strip().lower() or "text/plain",
                    "charset": charset.group(1) if charset else "utf-8",
                    "raw": body,
                    "encoding": headers.get("content-transfer-encoding", "").lower(),
                    "served": headers.get(SERVED_HEADER.lower(), "").lower() == "served"})
    return out


def _parts_by_email(raw):
    message = email.message_from_bytes(raw, policy=email.policy.compat32)
    out = []
    for part in message.walk():
        if part.is_multipart():
            continue
        out.append({"location": (part.get("Content-Location") or "").strip(),
                    "cid": (part.get("Content-ID") or "").strip().strip("<>"),
                    "type": part.get_content_type(),
                    "body": part.get_payload(decode=True) or b"",
                    "charset": part.get_content_charset() or "utf-8",
                    "served": (part.get(SERVED_HEADER) or "").strip().lower() == "served"})
    return out


def _body(part):
    """What a part holds, decoded the first time it is asked for."""
    body = part.get("body")
    if body is None:
        body = part["raw"]
        try:
            if part["encoding"] == "quoted-printable":
                body = binascii.a2b_qp(body)
            elif part["encoding"] == "base64":
                body = binascii.a2b_base64(body)
        except (binascii.Error, ValueError):
            pass                                  # as archived beats nothing
        part["body"] = body
    return body


def _text(part):
    try:
        return _body(part).decode(part["charset"], "replace")
    except LookupError:                           # a charset Python has no name for
        return _body(part).decode("utf-8", "replace")


class _Collector:
    def __init__(self, by_address, store, known=None):
        self._by = by_address
        self._store = store
        self._known = known
        self._done = {}
        # What the resource being read names, innermost last: address -> what
        # it was named, or None - kept with it in ``known``, to tell whether
        # the next archive's part still names the same things.
        self._using = []
        self.names = collections.OrderedDict()   # hash -> None, in the order made

    def page(self, part, depth):
        html = _text(part)
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
            found = self._done[address]
        else:
            found = self._resource(address, base, depth)
        if self._using:
            # With the address it was named from: a part filed under cid: is
            # read against that (see _resource), so asked again it has to be
            # asked from the same place.
            self._using[-1][address] = (found, base)
        return found

    def _resource(self, address, base, depth):
        part = self._by.get(address)
        if part is None or depth >= _MAX_DEPTH:
            return None
        self._done[address] = None            # a stylesheet that imports itself
        # What the part names is read against its own address - unless that
        # is only a name inside the archive: Chrome files a page's <style>
        # under cid:, its url() still written against the page, and read
        # against cid: every picture such a style named was lost.
        own = part["location"]
        if not own or own.startswith("cid:"):
            own = base
        key = None
        if self._known is not None and "raw" in part:
            key = (part["type"], part["encoding"], own, hashlib.sha1(part["raw"]).digest())
        name = self._recognised(key, depth) or self._read(part, own, depth, key)
        self.names[name] = None
        self._done[address] = REF + name
        return REF + name

    def _recognised(self, key, depth):
        """The name an earlier archive's part got, when this one is that part
        again: archived the same, and everything it names still named the same
        - a stylesheet is the same stylesheet only while its pictures are."""
        known = self._known.get(key) if key else None
        if known is None:
            return None
        name, named = known
        if name not in self._store:
            return None
        self._using.append({})
        try:
            for address, (found, base) in named.items():
                if self._ref(address, base, depth + 1) != found:
                    return None
        finally:
            self._using.pop()
        return name

    def _read(self, part, own, depth, key):
        body = _body(part)
        kind = part["type"]
        self._using.append({})
        try:
            if kind == "text/css":
                body = self._css(_text(part), own, depth + 1).encode("utf-8")
            elif kind == "text/html":
                body = _HUD.sub("", self.page(part, depth + 1)).encode("utf-8")
        finally:
            named = self._using.pop()
        name = hashlib.sha1(kind.encode("ascii", "replace") + b"\0" + body).hexdigest()
        self._store.setdefault(name, (kind, body))
        if key:
            self._known[key] = (name, named)
        return name


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
