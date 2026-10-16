"""Ctrl+F in a reader: every match lit, "n of m", Enter on, Shift+Enter back."""

import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPlainTextEdit, QTextBrowser, QVBoxLayout, QWidget

from cms_gui import findbar

TASK = ("<h1>QA-123 Shop</h1><p>Open the order.</p><p>Cancel the ORDER.</p>"
        "<p>Nothing here.</p><p>Print the order list.</p>")


def wait_for(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        QTest.qWait(20)
    pytest.fail("the search did not answer in time")


class Host(QWidget):
    """A page with one reader and its bar, the way the application has them."""

    def __init__(self, reader):
        super().__init__()
        column = QVBoxLayout(self)
        self.bar = findbar.FindBar()
        self.reader = reader
        column.addWidget(self.bar)
        column.addWidget(reader, 1)
        findbar.install(self, lambda: (self.bar, findbar.target_for(self.reader)))
        self.resize(700, 500)


@pytest.fixture
def host(qapp, dispose):
    made = []

    def make(reader):
        widget = Host(reader)
        widget.show()
        widget.activateWindow()
        QTest.qWaitForWindowActive(widget)
        made.append(widget)
        return widget

    yield make
    for widget in made:
        dispose(widget)


def _browser(html=TASK):
    reader = QTextBrowser()
    reader.setHtml(html)
    return reader


def test_ctrl_f_opens_the_bar_ready_to_type(host):
    page = host(_browser())
    page.reader.setFocus()
    assert not page.bar.isVisible()
    QTest.keyClick(page.reader, Qt.Key_F, Qt.ControlModifier)
    assert page.bar.isVisible() and page.bar.field.hasFocus()


def test_every_match_is_lit_and_counted_whatever_its_case(host):
    page = host(_browser())
    page.bar.open(findbar.target_for(page.reader))
    QTest.keyClicks(page.bar.field, "order")
    assert page.bar.count.text() == "1 of 3"
    assert len(page.reader.extraSelections()) == 3
    assert page.reader.textCursor().position() == \
        findbar.target_for(page.reader).matches()[0][0]


def test_enter_goes_on_and_shift_enter_goes_back_round_the_ends(host):
    page = host(_browser())
    page.bar.open(findbar.target_for(page.reader))
    QTest.keyClicks(page.bar.field, "order")
    QTest.keyClick(page.bar.field, Qt.Key_Return)
    assert page.bar.count.text() == "2 of 3"
    QTest.keyClick(page.bar.field, Qt.Key_Return)
    QTest.keyClick(page.bar.field, Qt.Key_Return)
    assert page.bar.count.text() == "1 of 3"              # round to the first
    QTest.keyClick(page.bar.field, Qt.Key_Return, Qt.ShiftModifier)
    assert page.bar.count.text() == "3 of 3"              # and back to the last
    QTest.keyClick(page.reader, Qt.Key_F3)
    assert page.bar.count.text() == "1 of 3"
    QTest.keyClick(page.reader, Qt.Key_F3, Qt.ShiftModifier)
    assert page.bar.count.text() == "3 of 3"


def test_no_match_says_so(host):
    page = host(_browser())
    page.bar.open(findbar.target_for(page.reader))
    QTest.keyClicks(page.bar.field, "invoice")
    assert page.bar.count.text() == "No matches"
    assert page.bar.count.property("bad") == "true"
    assert page.reader.extraSelections() == []


def test_esc_puts_the_lights_out_and_gives_the_reader_back(host):
    page = host(_browser())
    page.bar.open(findbar.target_for(page.reader))
    QTest.keyClicks(page.bar.field, "order")
    QTest.keyClick(page.bar.field, Qt.Key_Escape)
    assert not page.bar.isVisible()
    assert page.reader.extraSelections() == []
    assert page.reader.hasFocus()
    # Ctrl+F again comes back with the last search selected, to type over.
    QTest.keyClick(page.reader, Qt.Key_F, Qt.ControlModifier)
    assert page.bar.field.selectedText() == "order"
    assert page.bar.count.text() == "1 of 3"


def test_a_log_being_written_is_searched_again_as_it_grows(host):
    log = QPlainTextEdit()
    log.setReadOnly(True)
    log.setPlainText("INFO started\nERROR one\n")
    page = host(log)
    page.bar.open(findbar.target_for(log))
    QTest.keyClicks(page.bar.field, "error")
    assert page.bar.count.text() == "1 of 1"
    log.appendPlainText("ERROR two")
    wait_for(lambda: page.bar.count.text() == "1 of 2")
    assert len(log.extraSelections()) == 2


def test_a_huge_count_says_where_it_stopped(host, monkeypatch):
    monkeypatch.setattr(findbar, "TEXT_LIMIT", 5)
    log = QPlainTextEdit()
    log.setPlainText("x\n" * 20)
    page = host(log)
    page.bar.open(findbar.target_for(log))
    QTest.keyClicks(page.bar.field, "x")
    assert page.bar.count.text() == "1 of 5+"


def test_a_reader_with_nothing_to_search_says_so(host):
    page = host(_browser())
    page.bar.open(None)
    assert page.bar.count.text() == findbar.NOTHING
    assert not page.bar.field.isEnabled()
    page.bar.retarget(findbar.target_for(page.reader))
    assert page.bar.field.isEnabled()


def test_a_web_page_is_searched_by_the_browser(host, tmp_path):
    from cms_gui.htmlpreview import HtmlPreview

    path = tmp_path / "jira.html"
    path.write_text("<html><body>%s</body></html>" % TASK, encoding="utf-8")
    view = HtmlPreview()
    page = host(view)
    loaded = []
    view.loadFinished.connect(loaded.append)
    view.load_file(str(path), str(tmp_path))
    wait_for(lambda: bool(loaded))
    page.bar.open(findbar.target_for(view))
    QTest.keyClicks(page.bar.field, "order")
    wait_for(lambda: page.bar.count.text() == "1 of 3")
    QTest.keyClick(page.bar.field, Qt.Key_Return)
    wait_for(lambda: page.bar.count.text() == "2 of 3")
    QTest.keyClick(page.bar.field, Qt.Key_Escape)
    assert not page.bar.isVisible()
