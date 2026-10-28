"""The one address box: shown as it will open, edited as it is written."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from cms_gui import theme, widgets


@pytest.fixture
def field(qapp):
    widget = widgets.UrlField(placeholder="type an address")
    widget.resize(420, widget.sizeHint().height())
    widget.show()
    qapp.processEvents()
    yield widget
    widget.close()


def _committed(field):
    seen = []
    field.edited.connect(seen.append)
    return seen


def test_it_shows_the_address_as_plain_text(field):
    field.set_address([("http://localhost:8069", "")], "")
    assert field.text() == "http://localhost:8069"
    assert not field.is_editing()
    assert 'href="var:' not in field.label.text()


def test_an_empty_box_shows_its_placeholder(field):
    field.set_address([], "")
    assert field.label.property("empty") == "true"
    assert field.label.text() == "type an address"


def test_clicking_the_text_edits_what_was_written(field, qapp):
    field.set_address([("http://localhost:8074", "")], "localhost:8074")
    QTest.mouseClick(field.label, Qt.LeftButton)
    assert field.is_editing()
    assert field.editor.text() == "localhost:8074"


def test_the_pencil_edits_too(field):
    field.set_address([("http://localhost:8069", "")], "")
    field.pencil.click()
    assert field.is_editing()


def test_enter_commits_once_and_shows_the_address_again(field):
    seen = _committed(field)
    field.begin_edit()
    field.editor.setText("localhost:8075")
    QTest.keyClick(field.editor, Qt.Key_Return)
    assert seen == ["localhost:8075"]
    assert not field.is_editing()


def test_escape_puts_back_what_was_there(field):
    seen = _committed(field)
    field.set_address([("http://localhost:8069", "")], "")
    field.begin_edit()
    field.editor.setText("something else")
    QTest.keyClick(field.editor, Qt.Key_Escape)
    assert seen == []
    assert not field.is_editing()
    assert field.text() == "http://localhost:8069"


def test_leaving_the_box_commits(field):
    seen = _committed(field)
    field.begin_edit()
    field.editor.setText("localhost:8076")
    field._commit()                       # what focusing elsewhere sends
    assert seen == ["localhost:8076"]


def test_a_refresh_does_not_take_the_text_from_under_the_typist(field):
    field.begin_edit()
    field.editor.setText("half typed")
    field.set_address([("http://localhost:1", "")], "localhost:1")
    assert field.editor.text() == "half typed"


def test_both_modes_are_one_box(field, qapp):
    shown = field.frame.height()
    field.begin_edit()
    qapp.processEvents()
    assert field.editor.height() == shown


def test_a_value_from_a_variable_is_blue_and_asks_for_its_variable(field):
    field.set_address([("http://localhost:", ""), ("8074", "port"), ("/web", "")])
    text = field.label.text()
    assert 'href="var:port"' in text and theme.VAR_INK in text
    assert text.count("<a ") == 1           # only the value, not the address
    asked = []
    field.value_clicked.connect(asked.append)
    field.label.linkActivated.emit("var:port")
    assert asked == ["port"] and not field.is_editing()


# ------------------------------------------------ finishing a variable's name

def _typing(field, qapp, keys):
    field.begin_edit()
    field.editor.setText("")
    QTest.keyClicks(field.editor, keys)
    qapp.processEvents()
    return field.editor


def test_after_two_braces_the_first_name_is_offered_selected(field, qapp):
    field.set_variables(["port", "host"])
    editor = _typing(field, qapp, "localhost:{{")
    assert editor.text() == "localhost:{{host}}"
    assert editor.selectedText() == "host}}"


def test_typing_on_narrows_it_and_replaces_the_offer(field, qapp):
    field.set_variables(["port", "host"])
    editor = _typing(field, qapp, "localhost:{{p")
    assert editor.text() == "localhost:{{port}}" and editor.selectedText() == "ort}}"
    QTest.keyClicks(editor, "x")
    assert editor.text() == "localhost:{{px", "no name starts so - nothing offered"


def test_enter_keeps_the_offer(field, qapp):
    seen = []
    field.edited.connect(seen.append)
    field.set_variables(["port"])
    editor = _typing(field, qapp, "localhost:{{p")
    QTest.keyClick(editor, Qt.Key_Return)
    assert seen == ["localhost:{{port}}"]


def test_deleting_never_offers_again(field, qapp):
    field.set_variables(["port"])
    editor = _typing(field, qapp, "localhost:{{p")
    QTest.keyClick(editor, Qt.Key_Backspace)      # takes the offer away
    assert editor.text() == "localhost:{{p"
    QTest.keyClick(editor, Qt.Key_Backspace)
    assert editor.text() == "localhost:{{"


def test_the_long_spelling_is_finished_too(field, qapp):
    field.set_variables(["port"])
    editor = _typing(field, qapp, "x{{vars.po")
    assert editor.text() == "x{{vars.port}}"


def test_in_the_middle_of_an_address_the_rest_stays(field, qapp):
    field.set_variables(["port"])
    field.begin_edit()
    field.editor.setText("http://localhost:/web")
    field.editor.setCursorPosition(len("http://localhost:"))
    QTest.keyClicks(field.editor, "{{")
    assert field.editor.text() == "http://localhost:{{port}}/web"


def test_with_nothing_defined_nothing_is_offered(field, qapp):
    editor = _typing(field, qapp, "localhost:{{p")
    assert editor.text() == "localhost:{{p"


def test_the_menu_offers_every_variable(field, qapp):
    from PySide6.QtWidgets import QMenu
    field.set_variables(["port", "host"])
    field.begin_edit()
    field.editor.setText("localhost:")
    menu = QMenu()
    actions = field.editor.variable_actions(menu)
    assert [a.text() for a in actions] == ["{{host}}", "{{port}}"]
    actions[1].trigger()
    assert field.editor.text() == "localhost:{{port}}"
