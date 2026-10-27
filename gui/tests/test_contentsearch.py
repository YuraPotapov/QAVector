"""Search in what the reports say: one row per document, pages first."""

import os

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from cms_gui import contentsearch, search

# "Claims analytics", as a Jira task arrives in Ukrainian. Escaped so the source
# stays English; the files hold the real characters.
ANALYTICS = "\u0410\u043d\u0430\u043b\u0456\u0442\u0438\u043a\u0430"

TASK = """<!doctype html><html><head><title>QA-123 - Order export</title>
<style>.qa-123 { color: red; }</style></head>
<body><h1>QA-123 - Order export</h1><p>Export the order as CSV. %s</p>
<script>var key = "QA-123";</script>
<a href="https://example.invalid/browse/QA-123">QA-123</a></body></html>""" % ANALYTICS


@pytest.fixture
def runs(tmp_path):
    """Two runs: a task's reading copy and a log in one, a list in the other."""
    first = tmp_path / "run-1"
    (first / "steps" / "read_task").mkdir(parents=True)
    (first / "steps" / "read_task" / "jira.html").write_text(TASK, encoding="utf-8")
    (first / "steps" / "read_task" / "stdout.log").write_text(
        "fetched QA-123\nQA-123 has 2 comments\nqa-123 done\n", encoding="utf-8")
    # A hidden copy of the same file is not a second document.
    (first / ".execution").mkdir()
    (first / ".execution" / "jira.html").write_text(TASK, encoding="utf-8")
    second = tmp_path / "run-2"
    second.mkdir()
    (second / "queue.html").write_text(
        "<html><body><p>QA-124 Web export</p><p>QA-123 Order export</p></body></html>",
        encoding="utf-8")
    (second / "screen.png").write_bytes(b"QA-123")
    return [contentsearch.Root(str(first), "Run 1"), contentsearch.Root(str(second), "Run 2")]


# -- matching ---------------------------------------------------------------------
def test_a_page_is_matched_on_its_text_not_its_markup(runs):
    hits = contentsearch.scan(runs, "QA-123", contentsearch.PAGES)
    # The heading and the link's text: not the head (its title names the row),
    # the style, the script or the link's address.
    assert [(h.title, h.status) for h in hits] == [
        ("QA-123 - Order export", "2 matches"), ("queue.html", "1 match")]
    assert hits[0].detail == "Run 1 · " + os.path.join("steps", "read_task", "jira.html")
    assert hits[0].target == ("file", runs[0].path,
                              os.path.join("steps", "read_task", "jira.html"), "qa-123")


def test_the_rest_is_a_pass_of_its_own_and_counts_whatever_the_case(runs):
    hits = contentsearch.scan(runs, "qa-123", contentsearch.FILES)
    assert [(h.title, h.status) for h in hits] == [("stdout.log", "3 matches")]


def test_every_word_has_to_be_in_the_document_and_all_of_them_are_counted(runs):
    hits = contentsearch.scan(runs, "export " + ANALYTICS.lower(), contentsearch.PAGES)
    assert [(h.title, h.status) for h in hits] == [("QA-123 - Order export", "3 matches")]
    # The words never stand together: Find gets the longest of them.
    assert hits[0].target[3] == ANALYTICS.lower()
    # Together, they go to Find as they were typed.
    phrase = contentsearch.scan(runs, "order export", contentsearch.PAGES)
    assert {h.target[3] for h in phrase} == {"order export"}


def test_a_short_query_reads_nothing(runs):
    assert contentsearch.wanted("qa") == []
    assert contentsearch.scan(runs, "qa", contentsearch.PAGES) == []


def test_a_pass_for_a_query_no_longer_typed_gives_up(runs):
    assert contentsearch.scan(runs, "QA-123", contentsearch.PAGES, stale=lambda: True) == []


def test_a_page_changed_on_disk_is_read_again(runs):
    path = os.path.join(runs[1].path, "queue.html")
    assert contentsearch.scan(runs[1:], "QA-124", contentsearch.PAGES)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("<p>QA-125 replaced it, and is longer than it was</p>")
    assert contentsearch.scan(runs[1:], "QA-124", contentsearch.PAGES) == []


# -- in the popup -------------------------------------------------------------------
SECTIONS = [("scenarios", "Scenarios"), ("artifacts", "Artifacts")]


@pytest.fixture
def popup(qapp, dispose, runs):
    hits = [search.hit("QA-123 Order export", "qa_123_order", target="qa_123_order",
                       section="scenarios"),
            search.hit("Run 1", runs[0].path, target=runs[0].path, section="artifacts")]
    window = QWidget()
    window.resize(1000, 700)
    window.show()
    made = search.SearchPopup(lambda: list(hits), SECTIONS, window,
                              documents=lambda: [("artifacts", list(runs))])
    passes = []
    # The answer at once, and each pass noted: no thread to wait for.
    made._run = lambda work, done, owner=None: passes.append(done) or done(work(), None)
    made.open_over(window)
    made.passes = passes
    yield made
    made.hide()
    dispose(window)


def _rows(popup):
    shown = []
    for row in range(popup.results.count()):
        widget = popup.results.itemWidget(popup.results.item(row))
        shown.append(widget.title.text() if isinstance(widget, search._HitRow)
                     else widget.text())
    return shown


def test_documents_come_after_the_names_pages_first(popup):
    QTest.keyClicks(popup.field, "qa-123")
    assert popup.passes == []                       # not while typing
    popup.read_documents()
    assert len(popup.passes) == 2                   # pages, then the rest
    assert _rows(popup) == ["SCENARIOS", "QA-123 Order export",
                            "ARTIFACTS", "QA-123 - Order export", "queue.html",
                            "stdout.log"]
    assert popup.summary.text() == "4 found in 2 sections"


def test_typing_again_forgets_the_documents_until_they_are_read_again(popup):
    QTest.keyClicks(popup.field, "qa-123")
    popup.read_documents()
    QTest.keyClicks(popup.field, "4")
    assert _rows(popup) == []
    assert popup.empty.text() == "Nothing matches \"qa-1234\"."


def test_while_the_files_are_read_it_says_so(popup):
    popup._run = lambda work, done, owner=None: None     # still out
    QTest.keyClicks(popup.field, "web export")
    popup.read_documents()
    assert popup.empty.text() == "Reading files for \"web export\"..."
    assert popup.summary.text() == "reading files..."
