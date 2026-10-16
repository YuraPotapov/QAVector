"""Exporting a run's step-by-step guides: one file, or an archive of them.

A scenario run with ``--report-doc`` leaves a guide in each scenario's folder -
``documentation.html``, its pictures beside it in ``doc/``. Somebody handing the
result on wants files, not a report tree, so Export Documentation turns what is
selected into:

* **one file** for a session or a scenario - a "book": its guides one after
  another in the order they ran, each starting on a new page;
* **an archive** for a whole run - exactly what exporting each of its sessions
  would give, one file per session, in a .zip.

As HTML the file stands on its own: its pictures are written into it, so it can
be mailed or attached without the folder it came from. As PDF it is printed by
WebEngine, the way the Artifacts page always printed a guide - from a book that
points at the pictures where they are, rather than one carrying them as text.

The guides are this application's own output (``engine.livedoc``), so their
shape is known: one ``<style>``, one ``<body>``, pictures as relative
``doc/step_NNN.png``, anchors ``page-N`` and ``latest``. The GUI never imports
the engine, so the book is put together from the files, not re-rendered.
"""

import base64
import collections
import html
import os
import pathlib
import re
import shutil
import tempfile
import zipfile
from urllib.parse import unquote

from PySide6.QtCore import QObject, QUrl, Signal

from . import background

#: The guide a run writes into each scenario's folder (engine.livedoc).
GUIDE_NAME = "documentation.html"
#: Where the guide keeps its pictures and the data it is rendered from.
DOC_DIR = "doc"
JSON_NAME = "documentation.json"

HTML = "html"
PDF = "pdf"

#: One file of the export: what it is called, and the guides it holds in order.
Part = collections.namedtuple("Part", "name guides")
#: What a selection exports: its parts, whether they go into an archive, and
#: the name the file (or the archive) is offered under.
Plan = collections.namedtuple("Plan", "parts archive name")

#: Pictures a guide may carry, by suffix.
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml"}

#: Between two guides of one book: a new page in print, a clear break on screen.
BOOK_CSS = """
.guide + .guide { break-before: page; margin-top: 56px; padding-top: 28px;
                  border-top: 2px solid var(--line, #d4d4d7); }
@media print { .guide + .guide { margin-top: 0; padding-top: 0; border-top: none; } }
"""

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# -- what there is ------------------------------------------------------------
def guides_under(path):
    """The guides ``path`` holds, in the order they ran. [] when there are none.

    A folder is searched all the way down, its ``doc/`` folders aside. A file
    stands for its scenario when it is the guide itself or the result.json
    beside it; any other file has no guide to offer.
    """
    if not path:
        return []
    if os.path.isfile(path):
        name = os.path.basename(path)
        if name == GUIDE_NAME:
            return [path]
        if name == "result.json":
            guide = os.path.join(os.path.dirname(path), GUIDE_NAME)
            return [guide] if os.path.isfile(guide) else []
        return []
    found = []
    for folder, folders, files in os.walk(path):
        folders[:] = sorted(one for one in folders
                            if one != DOC_DIR and not one.startswith("."))
        if GUIDE_NAME in files:
            found.append(os.path.join(folder, GUIDE_NAME))
    return sorted(found, key=_run_order)


#: How much of documentation.json is read for when its scenario started. The
#: run's head is written first; the chapters after it can run to megabytes,
#: and this is asked on every rescan while a run is writing.
HEAD_BYTES = 4096


def _run_order(guide):
    """When a guide's scenario started, as the guide says, then the file's time.

    ``started`` is to the minute, so two scenarios in one minute fall back to
    when each guide was last written - which is in the order they ran.
    """
    started = ""
    try:
        with open(os.path.join(os.path.dirname(guide), DOC_DIR, JSON_NAME),
                  encoding="utf-8", errors="replace") as handle:
            match = re.search(r'"started":\s*"([^"]*)"', handle.read(HEAD_BYTES))
            started = match.group(1) if match else ""
    except OSError:
        pass
    try:
        mtime = os.path.getmtime(guide)
    except OSError:
        mtime = 0.0
    return started, mtime, guide


def plan(selection, run_root=""):
    """What exporting ``selection`` gives, or None when there is nothing to export.

    The run's own folder is an archive of its sessions - one part per folder
    directly under it. Anything below it is one file.
    """
    if not selection:
        return None
    selection = os.path.normpath(selection)
    if (run_root and os.path.isdir(selection)
            and selection == os.path.normpath(run_root)):
        groups = collections.OrderedDict()
        for guide in guides_under(selection):
            head = os.path.relpath(guide, selection).split(os.sep)[0]
            if head == GUIDE_NAME:
                head = os.path.basename(selection)     # a guide in the run itself
            groups.setdefault(head, []).append(guide)
        parts = [Part(name, guides) for name, guides in groups.items()]
        if not parts:
            return None
        return Plan(parts, True, "%s-documentation" % _folder_name(selection))
    guides = guides_under(selection)
    if not guides:
        return None
    folder = selection if os.path.isdir(selection) else os.path.dirname(selection)
    name = _folder_name(folder)
    return Plan([Part(name, guides)], False, name)


def scope(plan_):
    """What the export will be, said for the button: "" when there is none."""
    if plan_ is None:
        return ""
    if plan_.archive:
        count = len(plan_.parts)
        return ("%d session%s, as one .zip with a file for each"
                % (count, "" if count == 1 else "s"))
    count = len(plan_.parts[0].guides)
    return ("one file" if count == 1 else
            "one file, %d scenarios in the order they ran" % count)


def _folder_name(path):
    return os.path.basename(os.path.normpath(path)) or "documentation"


def safe_name(name):
    """``name`` as a file name any system takes - a session is "host:port-login"."""
    cleaned = _UNSAFE.sub("_", name or "").strip(" .")
    return cleaned or "documentation"


# -- the book -----------------------------------------------------------------
def book(part, inline=True):
    """``part``'s guides as one HTML page.

    ``inline`` writes the pictures into the page as ``data:`` URIs, so the file
    stands on its own; otherwise they are pointed at where they are, which is
    what a page printed here and thrown away needs.
    """
    style, lang, label = "", "", ""
    titles, articles = [], []
    for number, guide in enumerate(part.guides, 1):
        with open(guide, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        if not style:
            style = _first(r"<style>(.*?)</style>", text)
            lang = _first(r'<html lang="([^"]*)"', text)
            label = _first(r'<nav class="contents"><p class="kicker">([^<]*)</p>', text)
        body = _first(r"<body>(.*)</body>", text) or text
        titles.append(_first(r"<h1>(.*?)</h1>", body)
                      or html.escape(os.path.basename(os.path.dirname(guide))))
        body = _prefixed(body, "g%d-" % number)
        body = _pictures(body, os.path.dirname(guide), inline)
        articles.append('<article class="guide" id="g%d">\n%s\n</article>'
                        % (number, body.strip()))
    out = ['<!doctype html>', '<html lang="%s">' % (lang or "en"),
           '<head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           '<title>%s</title>' % html.escape(part.name),
           '<style>%s%s</style></head><body>' % (style, BOOK_CSS)]
    if len(articles) > 1:
        # The book's own contents, in the guides' own word for it: each guide
        # still has its own, which only reaches its own pages.
        out.append('<nav class="contents"><p class="kicker">%s</p><ol>%s</ol></nav>' % (
            label or "Contents",
            "".join('<li><a href="#g%d">%s</a></li>' % (number, title)
                    for number, title in enumerate(titles, 1))))
    out.extend(articles)
    out.append('</body></html>')
    return "\n".join(out) + "\n"


def _first(pattern, text):
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1) if match else ""


def _prefixed(body, prefix):
    """Anchors made unique across the book: page-3 of the second guide is g2-page-3.

    ``latest`` goes: it marks where a guide being written had got to, and a
    book is not being written.
    """
    body = re.sub(r'\sid="latest"', "", body)
    body = re.sub(r'(\sid=")([^"]+)"', lambda m: '%s%s%s"' % (m.group(1), prefix,
                                                               m.group(2)), body)
    return re.sub(r'(\shref="#)([^"]+)"',
                  lambda m: '%s%s%s"' % (m.group(1), prefix, m.group(2)), body)


def _pictures(body, folder, inline):
    """Every ``<img src>`` made to work away from the guide's folder.

    Only pictures inside that folder are followed - a guide never points
    anywhere else, and a book must not go and fetch what one somehow did.
    """
    root = os.path.realpath(folder)

    def replace(match):
        source = html.unescape(match.group(2))
        if re.match(r"[a-z][a-z0-9+.-]*:", source, re.IGNORECASE):
            return match.group(0)                 # data:, or anything already absolute
        path = os.path.realpath(os.path.join(folder, unquote(source)))
        if os.path.commonpath((path, root)) != root or not os.path.isfile(path):
            return match.group(0)
        if inline:
            with open(path, "rb") as handle:
                data = base64.b64encode(handle.read()).decode("ascii")
            mime = MIME.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
            url = "data:%s;base64,%s" % (mime, data)
        else:
            url = pathlib.Path(path).as_uri()
        return "%s%s%s" % (match.group(1), html.escape(url, quote=True), match.group(3))

    return re.sub(r'(<img\b[^>]*?\ssrc=")([^"]*)(")', replace, body)


# -- writing ------------------------------------------------------------------
def _atomic_text(path, text):
    partial = path + ".part"
    with open(partial, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(partial, path)


def _names(parts, suffix):
    """A file name per part, made safe and never the same twice."""
    seen, names = set(), []
    for part in parts:
        base = safe_name(part.name)
        name, count = base + suffix, 1
        while name.lower() in seen:
            count += 1
            name = "%s (%d)%s" % (base, count, suffix)
        seen.add(name.lower())
        names.append(name)
    return names


def write_html(plan_, target):
    """The plan as HTML at ``target``: one book, or a .zip with one per part."""
    if not plan_.archive:
        _atomic_text(target, book(plan_.parts[0], inline=True))
        return target
    partial = target + ".part"
    with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as archive:
        for part, name in zip(plan_.parts, _names(plan_.parts, ".html")):
            archive.writestr(name, book(part, inline=True))
    os.replace(partial, target)
    return target


def write_books(plan_, folder):
    """Each part as a book in ``folder``, pointing at its pictures: for printing.

    Returns ``[(part, the book's path, the name its PDF goes by)]``.
    """
    written = []
    for part, name in zip(plan_.parts, _names(plan_.parts, ".pdf")):
        path = os.path.join(folder, "%d.html" % (len(written) + 1))
        _atomic_text(path, book(part, inline=False))
        written.append((part, path, name))
    return written


def zip_files(entries, target):
    """``[(name in the archive, file)]`` packed at ``target``."""
    partial = target + ".part"
    with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, path in entries:
            archive.write(path, name)
    os.replace(partial, target)
    return target


def guide_roots(plan_):
    """The folders a book of this plan reads pictures from."""
    return tuple(sorted({os.path.realpath(os.path.dirname(guide))
                         for part in plan_.parts for guide in part.guides}))


# -- doing it -----------------------------------------------------------------
class DocExporter(QObject):
    """Writes one plan to one file, off the GUI thread where it can be.

    HTML is all file work, done on a thread. PDF is printed by WebEngine on
    this one, a part at a time - a page of its own that is never shown, so the
    preview stays where the reader has it - and an archive is packed on a
    thread once every part is printed.
    """

    #: ``(done, of)`` - parts printed so far, out of how many.
    progress = Signal(int, int)
    #: ``(target, written, what went wrong)``.
    finished = Signal(str, bool, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._busy = False
        self._page = None
        self._work = ""
        self._plan = None
        self._target = ""
        self._queue = []
        self._printed = []
        #: The part on its way: ``(its PDF's name, where it is written)``.
        self._current = None
        #: Whether that part is printing already - loadFinished can come more
        #: than once for one load, and a second print must not start.
        self._printing = False

    def busy(self):
        return self._busy

    def start(self, plan_, kind, target):
        """Export ``plan_`` as ``kind`` (HTML or PDF) to ``target``."""
        if self._busy or plan_ is None or not target:
            return False
        self._busy = True
        self._plan, self._target = plan_, target
        if kind == HTML:
            self.progress.emit(0, 1)
            background.run(lambda: write_html(plan_, target), self._html_done, owner=self)
            return True
        self._work = tempfile.mkdtemp(prefix="qavector-doc-")
        work = self._work
        background.run(lambda: write_books(plan_, work), self._books_ready, owner=self)
        return True

    def _html_done(self, _result, error):
        self._finish(error is None, "" if error is None else str(error))

    def _books_ready(self, books, error):
        if error is not None:
            self._finish(False, str(error))
            return
        self._queue = list(books or [])
        self._printed = []
        self._next()

    def _next(self):
        if not self._queue:
            self._printed_all()
            return
        part, path, name = self._queue.pop(0)
        self.progress.emit(len(self._printed), len(self._printed) + len(self._queue) + 1)
        page = self._page_for_printing()
        page.requests.roots = (os.path.realpath(self._work),) + guide_roots(self._plan)
        out = (os.path.join(self._work, "pdf", name) if self._plan.archive
               else self._target)
        self._current, self._printing = (name, out), False
        url = QUrl.fromLocalFile(path)
        # The page goes only where it was sent - see _ReportPage.
        page.document_url = QUrl(url)
        page.load(url)

    def _page_for_printing(self):
        if self._page is None:
            from .htmlpreview import _ReportPage

            self._page = _ReportPage(self)
            self._page.loadFinished.connect(self._loaded)
            self._page.pdfPrintingFinished.connect(self._pdf_written)
        return self._page

    def _loaded(self, ok):
        if self._current is None or self._printing:
            return
        if not ok:
            self._finish(False, "the documentation could not be read")
            return
        from .htmlpreview import page_layout

        _name, out = self._current
        os.makedirs(os.path.dirname(out), exist_ok=True)
        self._printing = True
        self._page.printToPdf(out, page_layout())

    def _pdf_written(self, path, ok):
        if self._current is None:
            return
        name, _out = self._current
        self._current, self._printing = None, False
        if not ok:
            self._finish(False, "could not write %s" % path)
            return
        self._printed.append((name, path))
        self._next()

    def _printed_all(self):
        if not self._plan.archive:
            self._finish(True, "")
            return
        entries, target = list(self._printed), self._target
        self.progress.emit(len(entries), len(entries))
        background.run(lambda: zip_files(entries, target), self._html_done, owner=self)

    def _finish(self, ok, message):
        self._busy = False
        self._current, self._printing = None, False
        self._queue = []
        if self._work:
            shutil.rmtree(self._work, ignore_errors=True)
            self._work = ""
        self.finished.emit(self._target, bool(ok), message)
