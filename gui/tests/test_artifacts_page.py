"""HTML is rendered in the artifact pane; technical files still show source."""

import json
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
