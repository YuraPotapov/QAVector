"""HTML is rendered in the artifact pane; technical files still show source."""

import json
import os
import time

import pytest
from PySide6.QtCore import QPoint, Qt, QUrl
from PySide6.QtGui import QPixmap
from PySide6.QtTest import QTest

from cms_gui.pages.artifacts import ArtifactsPage, PREVIEW_LIMIT

# Ukrainian, as a Jira task arrives: "Task description", "Field", "Value".
# Escaped so the source stays English; the page receives the real characters.
TITLE = "\u041e\u043f\u0438\u0441 \u0437\u0430\u0434\u0430\u0447\u0456"
FIELD = "\u041f\u043e\u043b\u0435"
VALUE = "\u0417\u043d\u0430\u0447\u0435\u043d\u043d\u044f"


def wait_for(predicate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if predicate():
            return
        QTest.qWait(20)
    pytest.fail("HTML preview did not finish in time")


def javascript(view, expression):
    result = []
    # The isolated application world can inspect layout even though document
    # JavaScript is disabled in this read-only preview.
    view.page().runJavaScript(expression, 1, result.append)
    wait_for(lambda: bool(result))
    return result[0]


@pytest.fixture
def page(qapp, tmp_path, dispose):
    widget = ArtifactsPage()
    widget.resize(1280, 900)
    widget.show_dir(str(tmp_path))
    widget.show()
    yield widget
    dispose(widget)


def select_html(page, tmp_path, body, name="report.html"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    page.rescan()
    assert page._select_path(str(path))
    assert page.html is not None and not page.html.isHidden()
    loaded = []
    page.html.loadFinished.connect(loaded.append)
    wait_for(lambda: bool(loaded))
    page.html.loadFinished.disconnect(loaded.append)
    assert loaded[-1], "HTML file could not be loaded"
    return path


def test_html_renders_css_tables_and_unicode_without_running_scripts(page, tmp_path):
    (tmp_path / "style.css").write_text(
        "h1 { color: rgb(12, 34, 56) } .columns { display: grid }", encoding="utf-8")
    path = select_html(page, tmp_path,
        '<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="style.css">'
        '<h1>%s</h1><div class="columns"><table><tr><th>%s</th>'
        '<td>%s</td></tr></table></div>' % (TITLE, FIELD, VALUE) +
        '<script>document.querySelector("h1").textContent="executed";</script>')
    wait_for(lambda: javascript(page.html, "document.querySelector('h1')?.textContent") == TITLE)
    assert javascript(page.html, "getComputedStyle(document.querySelector('h1')).color") == "rgb(12, 34, 56)"
    assert javascript(page.html, "getComputedStyle(document.querySelector('.columns')).display") == "grid"
    assert javascript(page.html, "document.querySelector('td').textContent") == VALUE
    assert page.text.isHidden() and page.image_area.isHidden()
    assert page.path_label.text() == str(path)
    assert page.html.page().profile().isOffTheRecord()


def test_preview_source_json_image_and_directory_switch_cleanly(page, tmp_path):
    source = '<h1>Readable</h1><a id="json" href="jira.json">JSON</a>'
    json_file = tmp_path / "jira.json"
    json_file.write_text('{"key":"QA-1"}', encoding="utf-8")
    image_file = tmp_path / "image.png"
    pixmap = QPixmap(10, 10)
    pixmap.fill()
    pixmap.save(str(image_file))
    html_file = select_html(page, tmp_path, source, "jira.htm")
    page.preview_mode.setCurrentIndex(1)
    assert page.text.toPlainText() == source
    assert page.html.isHidden() and not page.text.isHidden()
    loaded = []
    page.html.loadFinished.connect(loaded.append)
    page.preview_mode.setCurrentIndex(0)
    wait_for(lambda: bool(loaded))
    assert loaded[-1]
    assert javascript(page.html, "document.getElementById('json') !== null")
    # Click the actual link so navigation travels through WebEngine as it does
    # for a reader, then selects the sibling artifact in the tree.
    click_link(page.html, "json")
    wait_for(lambda: page._selected_path() == str(json_file))
    assert '"key": "QA-1"' in page.text.toPlainText()
    assert page.preview_mode.isHidden() and page.html.isHidden()
    page._select_path(str(html_file))
    assert not page.html.isHidden()
    page._select_path(str(image_file))
    assert not page.image_area.isHidden() and page.html.isHidden() and page.text.isHidden()
    page._select_path(str(tmp_path))
    assert page.text.toPlainText() == "Directory"
    assert page.html.isHidden() and page.image_area.isHidden()


def click_link(view, element_id):
    position = json.loads(javascript(view, "(() => {const r=document.getElementById('%s').getBoundingClientRect();"
                          "return JSON.stringify([r.x+r.width/2,r.y+r.height/2])})()" % element_id))
    QTest.mouseClick(view.focusProxy(), Qt.LeftButton,
                     pos=QPoint(round(position[0]), round(position[1])))


def test_jira_links_targeting_a_new_window_open_in_the_browser(page, tmp_path, monkeypatch):
    from cms_gui import htmlpreview

    opened = []
    monkeypatch.setattr(htmlpreview.QDesktopServices, "openUrl", opened.append)
    select_html(page, tmp_path, '<a id="issue" target="_blank" '
                'href="https://jira.example/browse/QA-1">QA-1</a>')
    click_link(page.html, "issue")
    wait_for(lambda: bool(opened))
    assert opened == [QUrl("https://jira.example/browse/QA-1")]


def test_html_preview_does_not_truncate_at_the_text_limit(page, tmp_path):
    select_html(page, tmp_path, '<!--' + 'x' * (PREVIEW_LIMIT * 6) + '--><h1>Last section</h1>')
    wait_for(lambda: javascript(page.html, "document.querySelector('h1')?.textContent") == "Last section")
    page.preview_mode.setCurrentIndex(1)
    assert "truncated" in page.text.toPlainText()


def test_preview_blocks_remote_and_outside_resources_and_handles_web_links(page, tmp_path, monkeypatch):
    from cms_gui import htmlpreview

    select_html(page, tmp_path, '<h1>Local report</h1>')
    interceptor = page.html.page().requests
    class Request:
        def __init__(self, url):
            self.url, self.blocked = QUrl(url), None
        def requestUrl(self):
            return self.url
        def block(self, blocked):
            self.blocked = blocked

    for url, blocked in (("https://example.test/tracker.png", True),
                         (QUrl.fromLocalFile(str(tmp_path.parent / "private.txt")).toString(), True),
                         (QUrl.fromLocalFile(str(tmp_path / "style.css")).toString(), False)):
        request = Request(url)
        interceptor.interceptRequest(request)
        assert request.blocked is blocked
    outside = tmp_path.parent / "private.txt"
    outside.write_text("private", encoding="utf-8")
    (tmp_path / "linked.txt").symlink_to(outside)
    request = Request(QUrl.fromLocalFile(str(tmp_path / "linked.txt")).toString())
    interceptor.interceptRequest(request)
    assert request.blocked
    opened = []
    monkeypatch.setattr(htmlpreview.QDesktopServices, "openUrl", opened.append)
    report = page.html.page()
    assert not report.acceptNavigationRequest(QUrl("https://jira.example/browse/QA-1"),
                                              htmlpreview.QWebEnginePage.NavigationTypeLinkClicked, True)
    assert opened == [QUrl("https://jira.example/browse/QA-1")]
    assert not report.acceptNavigationRequest(QUrl("https://example.test/redirect"),
                                              htmlpreview.QWebEnginePage.NavigationTypeOther, True)
    assert len(opened) == 1
    assert not report.acceptNavigationRequest(QUrl("file:///etc/passwd"),
                                              htmlpreview.QWebEnginePage.NavigationTypeOther, False)
    anchor = QUrl(report.document_url)
    anchor.setFragment("section")
    assert report.acceptNavigationRequest(anchor, htmlpreview.QWebEnginePage.NavigationTypeLinkClicked, True)


# ------------------------------------------------------------------- reveal
def a_cycle_run(tmp_path):
    run = tmp_path / "cycle-runs" / "20260929-101500-dev"
    (run / "steps" / "fetch").mkdir(parents=True)
    (run / "steps" / "fetch" / "jira.json").write_text("{}", encoding="utf-8")
    (run / "reports").mkdir()
    return run


def test_reveal_opens_the_run_and_selects_a_path_it_records_relatively(page, tmp_path):
    """A cycle records its files relative to its own directory."""
    run = a_cycle_run(tmp_path)
    assert page.reveal(str(run), "steps/fetch/jira.json")
    assert page._shown_dir == str(run)
    assert page._selected_path() == str(run / "steps" / "fetch" / "jira.json")
    assert page.path_label.text() == str(run / "steps" / "fetch" / "jira.json")


def test_reveal_selects_a_folder_too(page, tmp_path):
    run = a_cycle_run(tmp_path)
    assert page.reveal(str(run), "steps/fetch")
    assert page._selected_path() == str(run / "steps" / "fetch")


def test_reveal_with_nothing_to_select_shows_the_folder(page, tmp_path):
    run = a_cycle_run(tmp_path)
    assert not page.reveal(str(run))
    assert page._shown_dir == str(run)
    assert page._selected_path() == ""


def test_reveal_of_a_file_that_has_gone_leaves_the_folder_and_says_so(page, tmp_path):
    run = a_cycle_run(tmp_path)
    assert not page.reveal(str(run), "steps/fetch/deleted.html")
    assert page._shown_dir == str(run)
    assert page._selected_path() == ""
    assert "no longer there" in page.path_label.text()


def test_reveal_of_the_folder_already_shown_picks_up_new_files(page, tmp_path):
    run = a_cycle_run(tmp_path)
    page.reveal(str(run))
    (run / "reports" / "run.html").write_text("<p>done</p>", encoding="utf-8")
    assert page.reveal(str(run), "reports/run.html")


# ------------------------------------------------ a scenario's step-by-step guide
GUIDE = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<h1>Place an order</h1><p id="first">%s</p>
<div style="height: 3000px"></div><p id="latest">newest step</p></body></html>"""


def _scenario(tmp_path, guide=True):
    folder = tmp_path / "run" / "localhost:8069-admin" / "s"
    folder.mkdir(parents=True)
    (folder / "result.json").write_text(json.dumps({
        "scenario": "s", "session": "localhost:8069-admin", "status": "pass",
        "steps": [{"index": 0, "action": "click", "status": "pass", "target": ".x",
                   "message": "ok", "duration_ms": 10, "attempts": 1}]}),
        encoding="utf-8")
    if guide:
        (folder / "documentation.html").write_text(GUIDE % "Press New", encoding="utf-8")
    (folder / "console.log").write_text("hello", encoding="utf-8")
    return folder


def test_a_scenario_folder_opens_on_its_guide_and_switches_to_its_json(page, tmp_path):
    folder = _scenario(tmp_path)
    page.show_dir(str(tmp_path / "run"))
    page._select_path(str(folder))
    assert page.guide_mode.isVisibleTo(page)
    assert page.html is not None and page.html.isVisibleTo(page)
    assert page.export_docs.isVisibleTo(page)
    wait_for(lambda: "Press New" in javascript(page.html, "document.body.innerText"))
    page.guide_mode.setCurrentIndex(1)                    # Source
    assert not page.html.isVisibleTo(page)
    assert '"action": "click"' in page.text.toPlainText()
    # The export is made from the files, not from what the pane shows.
    assert page.export_docs.isVisibleTo(page)


def test_a_run_without_a_guide_says_how_to_get_one(page, tmp_path):
    folder = _scenario(tmp_path, guide=False)
    page.show_dir(str(tmp_path / "run"))
    page._select_path(str(folder / "result.json"))
    assert page.guide_mode.isVisibleTo(page)
    assert page.no_guide is not None and page.no_guide.isVisibleTo(page)
    assert not page.export_docs.isVisibleTo(page)
    page._select_path(str(folder / "console.log"))
    assert not page.guide_mode.isVisibleTo(page) and not page.no_guide.isVisibleTo(page)
    assert page.text.toPlainText() == "hello"


def test_the_guide_selected_as_a_file_offers_the_export_too(page, tmp_path):
    folder = _scenario(tmp_path)
    page.show_dir(str(tmp_path / "run"))
    page._select_path(str(folder / "documentation.html"))
    assert page.html.isVisibleTo(page) and page.export_docs.isVisibleTo(page)
    page._select_path(str(folder / "console.log"))
    assert not page.export_docs.isVisibleTo(page)


def test_a_guide_being_written_follows_the_run(page, tmp_path):
    folder = _scenario(tmp_path)
    page.show_dir(str(tmp_path / "run"))
    loaded = []
    page._select_path(str(folder))
    page.html.loadFinished.connect(loaded.append)
    wait_for(lambda: "Press New" in javascript(page.html, "document.body.innerText"))
    assert javascript(page.html, "window.scrollY") == 0       # chosen: from the top

    # A rescan with nothing new leaves the page alone, where the reader had it.
    count = len(loaded)
    page.rescan()
    QTest.qWait(150)
    assert len(loaded) == count

    # The run writes a step: the page is read again, and shows the newest one.
    (folder / "documentation.html").write_text(GUIDE % "Save the order",
                                               encoding="utf-8")
    page.rescan()
    wait_for(lambda: "Save the order" in javascript(page.html, "document.body.innerText"))
    wait_for(lambda: javascript(page.html, "window.scrollY") > 1000)


def test_export_writes_a_pdf_into_the_scenario_folder(page, tmp_path, monkeypatch):
    from cms_gui.pages import artifacts as artifacts_page

    folder = _scenario(tmp_path)
    page.show_dir(str(tmp_path / "run"))
    loaded = []
    page._select_path(str(folder))
    page.html.loadFinished.connect(loaded.append)
    wait_for(lambda: bool(loaded))
    offered = []

    def save_as(_parent, _caption, suggested, _filter):
        offered.append(suggested)
        return str(folder / "guide"), "PDF (*.pdf)"

    monkeypatch.setattr(artifacts_page.QFileDialog, "getSaveFileName", save_as)
    assert [action.text() for action in page.export_menu.actions()] == ["HTML", "PDF"]
    page.export_menu.actions()[1].trigger()
    assert offered == [str(folder / "s.pdf")]
    wait_for(lambda: page.export_docs.isEnabled())
    pdf = folder / "guide.pdf"
    assert pdf.read_bytes().startswith(b"%PDF")
    assert page.path_label.text().startswith("Saved")


def _second_session(tmp_path):
    folder = tmp_path / "run" / "localhost:8069-bob" / "t"
    folder.mkdir(parents=True)
    (folder / "result.json").write_text("{}", encoding="utf-8")
    (folder / "documentation.html").write_text(GUIDE % "Press Save", encoding="utf-8")
    return folder


def _save_as(monkeypatch, target, offered):
    from cms_gui.pages import artifacts as artifacts_page

    def save_as(_parent, _caption, suggested, _filter):
        offered.append(suggested)
        return str(target), ""

    monkeypatch.setattr(artifacts_page.QFileDialog, "getSaveFileName", save_as)


def test_the_run_folder_exports_a_zip_with_a_file_per_session(page, tmp_path,
                                                             monkeypatch):
    import zipfile

    _scenario(tmp_path)
    _second_session(tmp_path)
    run = tmp_path / "run"
    page.show_dir(str(run))
    page._select_path(str(run))
    assert page.export_docs.isVisibleTo(page)
    assert "2 sessions" in page.export_docs.toolTip()
    offered = []
    _save_as(monkeypatch, tmp_path / "out", offered)
    page.export_documentation("html")
    assert offered == [str(run / "run-documentation.zip")]
    wait_for(lambda: page.export_docs.isEnabled())
    with zipfile.ZipFile(tmp_path / "out.zip") as archive:
        assert sorted(archive.namelist()) == ["localhost_8069-admin.html",
                                              "localhost_8069-bob.html"]
    assert page.path_label.text() == "Saved %s" % (tmp_path / "out.zip")


def test_a_session_folder_exports_one_file(page, tmp_path, monkeypatch):
    _scenario(tmp_path)
    session = tmp_path / "run" / "localhost:8069-admin"
    page.show_dir(str(tmp_path / "run"))
    page._select_path(str(session))
    assert page.export_docs.isVisibleTo(page)
    assert page.export_docs.toolTip().endswith(": one file.")
    offered = []
    _save_as(monkeypatch, tmp_path / "admin.html", offered)
    page.export_documentation("html")
    assert offered == [str(session / "localhost_8069-admin.html")]
    wait_for(lambda: page.export_docs.isEnabled())
    assert "Press New" in (tmp_path / "admin.html").read_text(encoding="utf-8")


def test_an_export_that_fails_says_why(page, tmp_path, monkeypatch):
    _scenario(tmp_path)
    page.show_dir(str(tmp_path / "run"))
    page._select_path(str(tmp_path / "run" / "localhost:8069-admin"))
    _save_as(monkeypatch, tmp_path / "nowhere" / "x.html", [])
    page.export_documentation("html")
    wait_for(lambda: page.export_docs.isEnabled())
    assert page.path_label.text().startswith("Could not write %s: " % (
        tmp_path / "nowhere" / "x.html"))


# ------------------------------------------------- files announced during a run
def _count_rescans(page, monkeypatch):
    calls = []
    real = page.rescan
    monkeypatch.setattr(page, "rescan", lambda: calls.append(1) or real())
    return calls


def test_files_announced_during_a_run_are_read_in_one_go(page, monkeypatch):
    # A guide is rewritten after every step it describes; reading the whole run
    # for each announcement kept the window busy.
    from cms_gui.pages.artifacts import RESCAN_GATHER_MS

    calls = _count_rescans(page, monkeypatch)
    for _ in range(5):
        page.note_artifacts({})
    assert calls == []
    wait_for(lambda: len(calls) == 1)
    QTest.qWait(RESCAN_GATHER_MS + 200)
    assert len(calls) == 1


def test_a_page_nobody_is_looking_at_catches_up_when_it_is_shown(page, monkeypatch):
    from cms_gui.pages.artifacts import RESCAN_GATHER_MS

    page.hide()
    calls = _count_rescans(page, monkeypatch)
    page.note_artifacts({})
    QTest.qWait(RESCAN_GATHER_MS + 200)
    assert calls == []
    page.show()
    assert calls == [1]
    page.hide()
    page.show()
    assert calls == [1]                     # nothing new: not read again


# ------------------------------------------------------- Ctrl+F in the preview
def test_ctrl_f_finds_in_whatever_the_preview_shows(page, tmp_path):
    from cms_gui import findbar

    folder = _scenario(tmp_path)
    (folder / "screen.png").write_bytes(_png())
    page.show_dir(str(tmp_path / "run"))
    loaded = []
    page._select_path(str(folder))
    page.html.loadFinished.connect(loaded.append)
    wait_for(lambda: bool(loaded))
    page.activateWindow()
    QTest.qWaitForWindowActive(page)
    page.tree.setFocus()
    # From the file tree: the key belongs to the page, and the page searches
    # what it shows - the guide.
    QTest.keyClick(page.tree, Qt.Key_F, Qt.ControlModifier)
    assert page.find_bar.isVisible()
    assert page.find_bar.target() is findbar.target_for(page.html)
    QTest.keyClicks(page.find_bar.field, "Press")
    wait_for(lambda: page.find_bar.count.text() == "1 of 1")

    # Another file: the open search follows it, as text this time.
    page._select_path(str(folder / "console.log"))
    assert page.find_bar.target() is findbar.target_for(page.text)
    page.find_bar.field.setText("hello")
    assert page.find_bar.count.text() == "1 of 1"

    # A picture has nothing to search, and says so rather than going away.
    page._select_path(str(folder / "screen.png"))
    assert page.find_bar.isVisible()
    assert page.find_bar.count.text() == findbar.NOTHING


def _png():
    import base64

    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


# ------------------------------------------------------------------ search (Ctrl+K)
def test_search_lists_the_runs_with_reports_and_reveal_shows_one(page, tmp_path):
    current = tmp_path / "run"
    current.mkdir()
    page.set_run_dir(str(current))
    hits = page.search_rows()
    assert [(h.title, h.status) for h in hits] == [("Current run", "Current run")]
    other = tmp_path / "older"
    other.mkdir()
    assert page.show_found(str(other))
    assert page._shown_dir == str(other)
    assert not page.show_found(str(tmp_path / "gone"))


def test_search_reads_every_run_and_opens_a_document_with_find_ready(page, tmp_path):
    current = tmp_path / "run"
    (current / "steps").mkdir(parents=True)
    (current / "steps" / "notes.txt").write_text("QA-123 is ready\n", encoding="utf-8")
    page.set_run_dir(str(current))
    assert page.search_documents() == [(str(current), "Current run")]
    target = ("file", str(current), os.path.join("steps", "notes.txt"), "qa-123")
    assert page.show_found(target)
    assert page._selected_path() == str(current / "steps" / "notes.txt")
    assert page.find_bar.isVisible() and page.find_bar.field.text() == "qa-123"
    assert page.find_bar.count.text() == "1 of 1"
    # A document gone since the search: the run is shown, and Search says so.
    assert not page.show_found(("file", str(current), "gone.txt", "qa-123"))
