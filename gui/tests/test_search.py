"""Search (Ctrl+K): what matches, how it is shown, and how the key reaches it."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog, QLineEdit, QVBoxLayout, QWidget

from cms_gui import search

SECTIONS = [("logsources", "Services & Logs"), ("scenarios", "Scenarios"),
            ("history", "History")]


def _hits():
    return [
        search.hit("Web", "Shop · Python · app.py", "Running", "accent",
                   target=("runner", "Shop", "Web"), live=True, section="logsources"),
        search.hit("Worker", "Shop · Python", "Failed", "bad",
                   target=("runner", "Shop", "Worker"), live=True, section="logsources"),
        search.hit("Database", "Shop · Docker", "Stopped",
                   target=("runner", "Shop", "Database"), section="logsources"),
        search.hit("QA-123 Open an order", "qa_123_open_order · smoke",
                   keys="The shop's first order", target="qa_123_open_order",
                   section="scenarios"),
        search.hit("Web order export", "qa_124_web_export",
                   target="qa_124_web_export", section="scenarios"),
        search.hit("qa_123_open_order", "2026-01-02 10:00 · Launch", "passed",
                   "accent", target="20260102-100000", section="history"),
    ]


# -- matching ---------------------------------------------------------------------
def test_every_word_has_to_be_somewhere_whatever_its_case():
    groups = search.find(_hits(), "SHOP running", [s for s, _l in SECTIONS])
    assert [(g.section, [h.title for h in g.hits]) for g in groups] == [
        ("logsources", ["Web"])]


def test_the_best_matches_come_first_and_sections_keep_the_rails_order():
    groups = search.find(_hits(), "web", ["history", "scenarios", "logsources"])
    assert [g.section for g in groups] == ["scenarios", "logsources"]
    # A title starting with the words beats one that only holds them.
    assert [h.title for h in groups[0].hits] == ["Web order export"]


def test_ranking_title_start_then_word_start_then_anywhere():
    hits = [search.hit("Reorder lines", section="s"),
            search.hit("Open an order", section="s"),
            search.hit("Order list", section="s"),
            search.hit("Invoice", "order", section="s")]
    titles = [h.title for h in search.find(hits, "order", ["s"])[0].hits]
    assert titles == ["Order list", "Open an order", "Reorder lines", "Invoice"]


def test_what_else_a_hit_is_known_by_finds_it_too():
    groups = search.find(_hits(), "first order", ["scenarios"])
    assert [h.target for h in groups[0].hits] == ["qa_123_open_order"]


def test_an_empty_field_shows_what_is_active_now():
    groups = search.active(_hits(), [s for s, _l in SECTIONS])
    assert [(g.section, [h.title for h in g.hits]) for g in groups] == [
        ("logsources", ["Web", "Worker"])]
    assert search.find(_hits(), "   ") == []


# -- the popup ----------------------------------------------------------------------
@pytest.fixture
def popup(qapp, dispose):
    hits = list(_hits())
    window = QWidget()
    window.resize(1000, 700)
    window.show()
    made = search.SearchPopup(lambda: list(hits), SECTIONS, window)
    opened = []
    made.opened.connect(opened.append)
    made.open_over(window)
    made.hits, made.chosen = hits, opened
    yield made
    made.hide()
    dispose(window)


def _rows(popup):
    """What the list shows, top to bottom: headings in caps, hits by title."""
    shown = []
    for row in range(popup.results.count()):
        widget = popup.results.itemWidget(popup.results.item(row))
        if isinstance(widget, search._HitRow):
            shown.append(widget.title.text())
        else:
            shown.append(widget.text())
    return shown


def test_it_opens_on_what_is_active_with_the_field_ready(popup):
    assert popup.isVisible() and popup.field.hasFocus()
    assert _rows(popup) == ["SERVICES & LOGS", "Web", "Worker"]
    assert popup.summary.text() == "2 active now"
    # The first hit, not the heading above it, is where the keys are.
    assert popup.results.currentRow() == 1


def test_typing_finds_and_groups(popup):
    QTest.keyClicks(popup.field, "order")
    assert popup.isVisible()                    # building the rows never closes it
    assert _rows(popup) == ["SCENARIOS", "QA-123 Open an order", "Web order export",
                            "HISTORY", "qa_123_open_order"]
    assert popup.summary.text() == "3 found in 2 sections"


def test_down_and_up_go_over_the_hits_and_never_stop_on_a_heading(popup):
    QTest.keyClicks(popup.field, "order")
    QTest.keyClick(popup.field, Qt.Key_Down)
    assert popup.results.currentRow() == 2
    QTest.keyClick(popup.field, Qt.Key_Down)
    assert popup.results.currentRow() == 4                # past "HISTORY"
    QTest.keyClick(popup.field, Qt.Key_Down)
    assert popup.results.currentRow() == 4                # the last stays the last
    QTest.keyClick(popup.field, Qt.Key_Up)
    QTest.keyClick(popup.field, Qt.Key_Up)
    assert popup.results.currentRow() == 1


def test_enter_opens_the_hit_and_the_popup_goes(popup):
    QTest.keyClicks(popup.field, "order")
    QTest.keyClick(popup.field, Qt.Key_Down)
    QTest.keyClick(popup.field, Qt.Key_Return)
    assert [h.target for h in popup.chosen] == ["qa_124_web_export"]
    assert not popup.isVisible()


def test_a_click_opens_it_too(popup):
    QTest.keyClicks(popup.field, "web")
    assert _rows(popup)[:2] == ["SERVICES & LOGS", "Web"]
    popup.results.itemClicked.emit(popup.results.item(1))
    assert [h.target for h in popup.chosen] == [("runner", "Shop", "Web")]


def test_nothing_found_says_so(popup):
    QTest.keyClicks(popup.field, "invoice")
    assert not popup.results.isVisible()
    assert popup.empty.text() == 'Nothing matches "invoice".'
    QTest.keyClick(popup.field, Qt.Key_Return)          # and Enter does nothing
    assert popup.chosen == []


def test_a_long_section_folds_into_more_and_opens_in_place(popup, monkeypatch):
    monkeypatch.setattr(search, "PER_SECTION", 1)
    popup.field.setText("shop")
    assert _rows(popup)[:3] == ["SERVICES & LOGS", "Web", "2 more in Services & Logs"]
    QTest.keyClick(popup.field, Qt.Key_Down)
    QTest.keyClick(popup.field, Qt.Key_Return)          # on "2 more"
    assert _rows(popup)[:4] == ["SERVICES & LOGS", "Web", "Worker", "Database"]
    assert popup.chosen == [] and popup.isVisible()
    assert popup.results.currentRow() == 2              # on the first of the rest


def test_states_are_read_again_while_it_is_open(popup):
    popup.hits[0] = popup.hits[0]._replace(status="Failed", tone="bad")
    popup._read_again()
    first = popup.results.itemWidget(popup.results.item(1))
    assert first.tag.text() == "Failed"


def test_esc_closes_it(popup):
    QTest.keyClick(popup.field, Qt.Key_Escape)
    assert not popup.isVisible()


# -- the key ------------------------------------------------------------------------
@pytest.fixture
def window(qapp, dispose):
    """A window with a text field, the Search shortcut, and the startup wiring."""
    from PySide6.QtGui import QAction, QKeySequence

    made = QWidget()
    column = QVBoxLayout(made)
    made.field = QLineEdit("hello world")
    column.addWidget(made.field)
    made.calls = []
    action = QAction("Search", made)
    action.setShortcut(QKeySequence(search.SHORTCUT))
    action.setShortcutContext(Qt.ApplicationShortcut)
    action.triggered.connect(lambda: made.calls.append(1))
    made.addAction(action)
    made.catcher = search.catch_ctrl_k(qapp)
    made.show()
    made.activateWindow()
    QTest.qWaitForWindowActive(made)
    yield made
    dispose(made)


def _override(widget, key, modifiers):
    """Ask ``widget`` whether it wants ``key`` itself, the way the shortcut map does."""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    event = QKeyEvent(QEvent.ShortcutOverride, key, modifiers)
    event.ignore()
    QApplication.sendEvent(widget, event)
    return event.isAccepted()


def test_ctrl_k_opens_search_even_from_a_text_field(window):
    window.field.setFocus()
    window.field.setCursorPosition(5)
    QTest.keyClick(window.field, Qt.Key_K, Qt.ControlModifier)
    assert window.calls == [1]
    assert window.field.text() == "hello world"


def test_a_text_field_lets_ctrl_k_through_and_keeps_its_other_keys(window):
    # On X11 a field answers this override with "mine" and deletes to the end
    # of its line; the guard answers first, with "not mine".
    assert window.field in window.catcher._watched
    assert not _override(window.field, Qt.Key_K, Qt.ControlModifier)
    # Its other keys are still its own: Ctrl+A selects all of its text.
    assert _override(window.field, Qt.Key_A, Qt.ControlModifier)


def test_a_field_made_later_is_guarded_as_the_style_polishes_it(window, qapp):
    from cms_gui import theme

    qapp.setStyle(theme.app_style())
    later = QLineEdit("made later", window)
    later.ensurePolished()
    assert later in window.catcher._watched


def test_a_modal_dialog_keeps_its_keys(window):
    dialog = QDialog(window)
    dialog.setModal(True)
    field = QLineEdit("hello world", dialog)
    dialog.show()
    window.catcher.watch(field)
    field.setFocus()
    assert not window.catcher.eventFilter(field, _key_event(Qt.Key_K))
    QTest.keyClick(field, Qt.Key_K, Qt.ControlModifier)
    assert window.calls == []
    dialog.close()


def _key_event(key):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent

    return QKeyEvent(QEvent.ShortcutOverride, key, Qt.ControlModifier)


def test_html_still_opens_with_the_keys_guarded(window, qapp, tmp_path):
    """The guard is on the fields, never on the application.

    An application-wide Python event filter took the whole program down the
    moment WebEngine opened an HTML page - a crash, which here would end the
    test run rather than fail one test.
    """
    from cms_gui.htmlpreview import HtmlPreview

    page = tmp_path / "jira.html"
    page.write_text("<html><body><h1>QA-123</h1></body></html>", encoding="utf-8")
    view = HtmlPreview(window)
    window.layout().addWidget(view)
    loaded = []
    view.loadFinished.connect(loaded.append)
    view.load_file(str(page), str(tmp_path))
    deadline = __import__("time").monotonic() + 10
    while not loaded and __import__("time").monotonic() < deadline:
        QTest.qWait(20)
    assert loaded == [True]
    QTest.keyClick(window.field, Qt.Key_K, Qt.ControlModifier)
    assert window.calls == [1]


def test_ctrl_k_in_search_itself_selects_the_query(popup):
    popup.field.setText("order list")
    popup.field.setCursorPosition(3)
    QTest.keyClick(popup.field, Qt.Key_K, Qt.ControlModifier)
    assert popup.field.text() == "order list"
    assert popup.field.selectedText() == "order list"
