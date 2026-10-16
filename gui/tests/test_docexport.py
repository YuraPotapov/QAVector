"""Export Documentation: which guides a selection holds, and the files they become."""

import base64
import json
import os
import time
import zipfile

import pytest
from PySide6.QtTest import QTest

from cms_gui import docexport

#: A guide as engine.livedoc writes one: a style, a header, contents that link
#: to its pages, a picture beside it in doc/, and the newest step marked.
GUIDE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8">
<title>%(title)s</title>
<style>body { color: #111; } .shot img { width: 100%%; }</style></head><body>
<header>
<p class="kicker">%(scenario)s</p>
<h1>%(title)s</h1>
</header>
<nav class="contents"><p class="kicker">Contents</p><ol><li><a href="#page-1">Orders</a></li></ol></nav>
<section class="page" id="page-1"><h3>Orders</h3>
<figure class="wide"><div class="shot"><div class="clip"><img src="doc/step_001.png" alt=""></div></div></figure>
<div class="item" id="latest"><p>Press New</p></div>
</section>
</body></html>
"""
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def _guide(run, session, scenario, started="2026-01-02 10:00", title=None):
    folder = run / session / scenario
    (folder / "doc").mkdir(parents=True)
    (folder / "doc" / "step_001.png").write_bytes(PNG)
    (folder / "doc" / "documentation.json").write_text(json.dumps(
        {"scenario": scenario, "title": title or scenario, "started": started,
         "chapters": [{"title": "x" * 10000}]}, indent=2), encoding="utf-8")
    (folder / "documentation.html").write_text(
        GUIDE % {"title": title or scenario, "scenario": scenario}, encoding="utf-8")
    (folder / "result.json").write_text("{}", encoding="utf-8")
    (folder / "console.log").write_text("hello", encoding="utf-8")
    return folder


@pytest.fixture
def run(tmp_path):
    """A launch of two accounts; the first ran two scenarios, in this order."""
    run = tmp_path / "20260102-100000"
    _guide(run, "shop.example.invalid-alice", "qa_123_open_order", "2026-01-02 10:00")
    _guide(run, "shop.example.invalid-alice", "qa_124_cancel_order", "2026-01-02 10:05")
    _guide(run, "shop.example.invalid-bob", "qa_123_open_order", "2026-01-02 10:00")
    return run


# -- what a selection exports ---------------------------------------------------
def test_the_run_folder_is_an_archive_of_one_file_per_session(run):
    plan = docexport.plan(str(run), str(run))
    assert plan.archive and plan.name == "20260102-100000-documentation"
    assert [part.name for part in plan.parts] == ["shop.example.invalid-alice",
                                                 "shop.example.invalid-bob"]
    assert [len(part.guides) for part in plan.parts] == [2, 1]
    assert "2 sessions" in docexport.scope(plan)


def test_a_session_is_one_file_with_its_scenarios_in_the_order_they_ran(run):
    session = run / "shop.example.invalid-alice"
    # Written the other way round: the order is when they started, not the names
    # and not which file happens to be newer.
    later = session / "qa_124_cancel_order" / "documentation.html"
    os.utime(later, (time.time() - 3600, time.time() - 3600))
    plan = docexport.plan(str(session), str(run))
    assert not plan.archive and plan.name == "shop.example.invalid-alice"
    assert [os.path.basename(os.path.dirname(g)) for g in plan.parts[0].guides] == [
        "qa_123_open_order", "qa_124_cancel_order"]
    assert docexport.scope(plan) == "one file, 2 scenarios in the order they ran"


@pytest.mark.parametrize("name", ["", "documentation.html", "result.json"])
def test_a_scenario_folder_or_its_guide_is_one_file(run, name):
    folder = run / "shop.example.invalid-bob" / "qa_123_open_order"
    plan = docexport.plan(str(folder / name) if name else str(folder), str(run))
    assert not plan.archive and plan.name == "qa_123_open_order"
    assert plan.parts[0].guides == [str(folder / "documentation.html")]
    assert docexport.scope(plan) == "one file"


@pytest.mark.parametrize("name", ["console.log", "doc"])
def test_what_holds_no_guide_exports_nothing(run, name):
    folder = run / "shop.example.invalid-bob" / "qa_123_open_order"
    assert docexport.plan(str(folder / name), str(run)) is None
    assert docexport.scope(None) == ""


def test_a_run_with_no_guides_offers_no_export(tmp_path):
    (tmp_path / "s" / "x").mkdir(parents=True)
    (tmp_path / "s" / "x" / "result.json").write_text("{}", encoding="utf-8")
    assert docexport.plan(str(tmp_path), str(tmp_path)) is None


def test_file_names_are_safe_everywhere():
    assert docexport.safe_name("localhost:8069-admin") == "localhost_8069-admin"
    assert docexport.safe_name('a/b\\c?"d') == "a_b_c__d"
    assert docexport.safe_name("  ") == "documentation"


# -- the book -------------------------------------------------------------------
def test_a_book_keeps_one_style_and_every_anchor_unique(run):
    plan = docexport.plan(str(run / "shop.example.invalid-alice"), str(run))
    page = docexport.book(plan.parts[0])
    assert page.count("<style>") == 1 and page.count("<body>") == 1
    assert page.count('<article class="guide"') == 2
    # Each guide's own links still reach its own pages, and nothing else's.
    assert 'id="g1-page-1"' in page and 'href="#g1-page-1"' in page
    assert 'id="g2-page-1"' in page and 'href="#g2-page-1"' in page
    assert 'id="page-1"' not in page and 'id="latest"' not in page
    # And the book says what is in it, in the guides' own word for contents.
    assert '<a href="#g1">qa_123_open_order</a>' in page
    assert "break-before: page" in page


def test_a_book_written_to_be_sent_carries_its_pictures(run):
    plan = docexport.plan(str(run / "shop.example.invalid-bob"), str(run))
    page = docexport.book(plan.parts[0], inline=True)
    assert 'src="data:image/png;base64,%s"' % base64.b64encode(PNG).decode() in page
    assert "doc/step_001.png" not in page
    # A single guide is just itself: no book contents of its own.
    assert '<a href="#g1">' not in page


def test_a_book_to_print_points_at_the_pictures_where_they_are(run):
    folder = run / "shop.example.invalid-bob" / "qa_123_open_order"
    plan = docexport.plan(str(folder), str(run))
    page = docexport.book(plan.parts[0], inline=False)
    assert 'src="%s"' % (folder / "doc" / "step_001.png").as_uri() in page


def test_a_picture_from_outside_the_guide_is_never_followed(run, tmp_path):
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG)
    guide = run / "shop.example.invalid-bob" / "qa_123_open_order" / "documentation.html"
    guide.write_text(guide.read_text().replace("doc/step_001.png", "../../../secret.png"),
                     encoding="utf-8")
    page = docexport.book(docexport.plan(str(guide), str(run)).parts[0])
    assert "base64" not in page and "../../../secret.png" in page


def test_html_of_the_run_is_a_zip_with_a_file_per_session(run, tmp_path):
    target = str(tmp_path / "out.zip")
    docexport.write_html(docexport.plan(str(run), str(run)), target)
    with zipfile.ZipFile(target) as archive:
        assert sorted(archive.namelist()) == ["shop.example.invalid-alice.html",
                                              "shop.example.invalid-bob.html"]
        alice = archive.read("shop.example.invalid-alice.html").decode()
    assert alice.count('<article class="guide"') == 2 and "data:image/png" in alice
    assert not os.path.exists(target + ".part")


def test_names_that_meet_after_cleaning_stay_apart(tmp_path):
    run = tmp_path / "run"
    _guide(run, "host:1-a", "s")
    _guide(run, "host_1-a", "s")
    target = str(tmp_path / "out.zip")
    docexport.write_html(docexport.plan(str(run), str(run)), target)
    with zipfile.ZipFile(target) as archive:
        assert sorted(archive.namelist()) == ["host_1-a (2).html", "host_1-a.html"]


# -- doing it, as the page does -------------------------------------------------
def _export(qapp, plan, kind, target):
    exporter = docexport.DocExporter()
    done = []
    exporter.finished.connect(lambda *answer: done.append(answer))
    assert exporter.start(plan, kind, target)
    assert exporter.busy()
    deadline = time.monotonic() + 30
    while not done and time.monotonic() < deadline:
        QTest.qWait(20)
    assert done, "the export did not finish"
    assert not exporter.busy()
    return done[0]


def test_one_session_as_pdf_is_one_pdf(qapp, run, tmp_path):
    target = str(tmp_path / "alice.pdf")
    plan = docexport.plan(str(run / "shop.example.invalid-alice"), str(run))
    assert _export(qapp, plan, docexport.PDF, target) == (target, True, "")
    with open(target, "rb") as handle:
        assert handle.read(5) == b"%PDF-"


def test_the_run_as_pdf_is_a_zip_of_one_pdf_per_session(qapp, run, tmp_path):
    target = str(tmp_path / "run.zip")
    plan = docexport.plan(str(run), str(run))
    assert _export(qapp, plan, docexport.PDF, target) == (target, True, "")
    with zipfile.ZipFile(target) as archive:
        assert sorted(archive.namelist()) == ["shop.example.invalid-alice.pdf",
                                              "shop.example.invalid-bob.pdf"]
        assert all(archive.read(name).startswith(b"%PDF-") for name in archive.namelist())


def test_html_is_written_off_the_gui_thread_and_reported(qapp, run, tmp_path):
    target = str(tmp_path / "bob.html")
    plan = docexport.plan(str(run / "shop.example.invalid-bob"), str(run))
    assert _export(qapp, plan, docexport.HTML, target) == (target, True, "")
    assert "data:image/png" in open(target, encoding="utf-8").read()
