"""Search in what a run's reports say: Ctrl+K's words inside the files.

A Jira task a cycle fetched is in its run as ``jira.html``; a scenario's guide
is ``documentation.html``; a step's log holds whatever the step printed. The
pages list runs by name, so a task that was plainly there could not be found
by its key or its words. This reads the files of every run the Artifacts page
offers and answers **one row per document**: its title, where it is, and how
many times the words occur in it.

Pages first. HTML is what was written to be read - a task's reading copy, a
guide - so it is searched first and listed first; the logs, JSON and YAML that
mention the same words come after it, in a second pass. HTML is matched on its
text, never on its markup or styles, and that text is kept between searches:
it is small, and it is what is asked for most. The rest is read again each
time - a few hundred megabytes on a busy machine is not worth keeping.

Everything here runs off the GUI thread (``search.SearchPopup`` hands it to
``background``), and gives up as soon as ``stale()`` says the query it is
answering is no longer the one in the field.
"""

import collections
import html
import os
import re

from . import search

HTML_SUFFIXES = (".html", ".htm")
#: The rest of what the Artifacts page shows as text.
TEXT_SUFFIXES = (".json", ".log", ".txt", ".yaml", ".yml", ".md", ".csv")
#: The two passes, in the order they are searched and listed.
PAGES, FILES = "pages", "files"
#: How much of one file is read. Past this it is a dump, not a document.
READ_LIMIT = 4_000_000
#: Shorter queries match nearly every file and say nothing.
MIN_QUERY = 3
#: The most documents one pass lists.
MAX_DOCUMENTS = 200

#: One folder to read: a run's directory, and what the run is called.
Root = collections.namedtuple("Root", "path label")

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_MARKUP = re.compile(r"<(script|style|head)\b.*?</\1\s*>|<!--.*?-->|<[^>]+>",
                     re.S | re.I)
_SPACE = re.compile(r"\s+")

#: path -> (mtime, size, title, lowered text), for HTML only.
_pages = {}


def wanted(query):
    """The query as it is matched: every word, lowered; [] for too short."""
    if len((query or "").strip()) < MIN_QUERY:
        return []
    return [word.lower() for word in query.split() if word]


def occurrences(text, words):
    """How often ``words`` occur in ``text`` together; 0 unless every one does."""
    counts = [text.count(word) for word in words]
    return sum(counts) if counts and all(counts) else 0


def page_text(body):
    """An HTML page's title and its text, without markup, styles or scripts."""
    found = _TITLE.search(body)
    title = _SPACE.sub(" ", html.unescape(found.group(1))).strip() if found else ""
    return title, _SPACE.sub(" ", html.unescape(_MARKUP.sub(" ", body)))


def files(roots, kind):
    """``(root, path)`` of every file of ``kind`` under ``roots``.

    Hidden folders are left out: ``.execution`` keeps earlier copies of the
    steps' own files, which would list every match twice.
    """
    suffixes = HTML_SUFFIXES if kind == PAGES else TEXT_SUFFIXES
    seen = set()
    for root in roots:
        top = os.path.normpath(root.path)
        if top in seen or not os.path.isdir(top):
            continue
        seen.add(top)
        for folder, subfolders, names in os.walk(top):
            subfolders[:] = sorted(name for name in subfolders if not name.startswith("."))
            for name in sorted(names):
                if name.lower().endswith(suffixes):
                    yield root, os.path.join(folder, name)


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read(READ_LIMIT)


def _page(path, stat):
    known = _pages.get(path)
    if known is not None and known[:2] == (stat.st_mtime, stat.st_size):
        return known[2], known[3]
    title, text = page_text(_read(path))
    _pages[path] = (stat.st_mtime, stat.st_size, title, text.lower())
    return title, _pages[path][3]


def scan(roots, query, kind, stale=lambda: False):
    """Hits for the documents of ``kind`` holding every word of ``query``.

    Most matches first, then the newest. Each hit opens its file on the
    Artifacts page, with the query - or, when the words never stand together,
    its longest word - ready in the page's Find.
    """
    words = wanted(query)
    if not words:
        return []
    phrase = " ".join(words)
    found = []
    for root, path in files(roots, kind):
        if stale():
            return []
        try:
            stat = os.stat(path)
            if kind == PAGES:
                title, text = _page(path, stat)
            else:
                title, text = "", _read(path).lower()
        except OSError:
            continue
        count = occurrences(text, words)
        if count:
            find = phrase if len(words) > 1 and phrase in text else max(words, key=len)
            found.append((count, stat.st_mtime, root, path, title, find))
    found.sort(key=lambda row: (-row[0], -row[1], row[3]))
    return [_hit(*row) for row in found[:MAX_DOCUMENTS]]


def _hit(count, _mtime, root, path, title, find):
    where = os.path.relpath(path, root.path)
    return search.hit(
        title or os.path.basename(path),
        " · ".join(part for part in (root.label, where) if part),
        "%d match%s" % (count, "" if count == 1 else "es"),
        target=("file", root.path, where, find))
