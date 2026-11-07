"""A Jira Cloud description in the editor: in as ADF, out as the same ADF.

The promise worth more than the rest: a description loaded and saved
untouched is the description Jira already has. Then each thing the toolbar
and the keys do comes out as the ADF it stands for, and what is pasted keeps
only what Jira can hold.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QMimeData, QPoint, Qt
from PySide6.QtGui import QTextBlockFormat, QTextCursor, QTextDocument
from PySide6.QtTest import QTest

from cms_gui import widgets
from cms_gui.pages import jiradoc


def _text(text, *marks):
    node = {"type": "text", "text": text}
    if marks:
        node["marks"] = [mark if isinstance(mark, dict) else {"type": mark} for mark in marks]
    return node


def _para(*content):
    return {"type": "paragraph", "content": list(content)}


def _doc(*content):
    return {"type": "doc", "version": 1, "content": list(content)}


LINK = {"type": "link", "attrs": {"href": "https://qa.example.invalid/browse/QA-1"}}

EVERYTHING = _doc(
    {"type": "heading", "attrs": {"level": 3}, "content": [_text("What to do")]},
    _para(_text("Module", "strong"), _text(" on prod by "), _text("15.12", "em"),
          _text(" code: "), _text("origin/dev", "code"), {"type": "hardBreak"},
          _text("see "), _text("QA-1", LINK), _text(" gone", "strike"),
          _text(" under", "underline"),
          _text(" red", {"type": "textColor", "attrs": {"color": "#ff5630"}}),
          {"type": "mention", "attrs": {"id": "acc-1", "text": "@Olena"}},
          {"type": "mention", "attrs": {"id": "acc-1", "text": "@Olena"}},
          {"type": "emoji", "attrs": {"shortName": ":smile:", "text": "x"}},
          {"type": "inlineCard", "attrs": {"url": "https://qa.example.invalid/x"}}),
    {"type": "orderedList", "attrs": {"order": 3}, "content": [
        {"type": "listItem", "content": [_para(_text("first")), {
            "type": "bulletList", "content": [
                {"type": "listItem", "content": [_para(_text("nested"))]}]}]},
        {"type": "listItem", "content": [_para(_text("second")),
                                         _para(_text("more of second"))]}]},
    {"type": "codeBlock", "attrs": {"language": "python"},
     "content": [_text("print(1)\n\nprint(2)")]},
    {"type": "codeBlock", "content": []},
    {"type": "blockquote", "content": [_para(_text("quoted")), _para(_text("twice"))]},
    {"type": "rule"},
    {"type": "taskList", "attrs": {"localId": "t1"}, "content": [
        {"type": "taskItem", "attrs": {"localId": "i1", "state": "DONE"},
         "content": [_text("done")]},
        {"type": "taskItem", "attrs": {"localId": "i2", "state": "TODO"},
         "content": [_text("open")]}]},
    {"type": "bulletList", "content": [{"type": "listItem", "content": [_para(_text("a"))]}]},
    {"type": "bulletList", "content": [{"type": "listItem", "content": [_para(_text("b"))]}]},
    _para(),
)


def _canon(node):
    """Marks in one order: ADF does not care which comes first."""
    if isinstance(node, dict):
        node = {key: _canon(value) for key, value in node.items()}
        if "marks" in node:
            node["marks"] = sorted(node["marks"], key=lambda one: json.dumps(one, sort_keys=True))
        return node
    if isinstance(node, list):
        return [_canon(one) for one in node]
    return node


@pytest.fixture
def editor(qapp, dispose):
    made = []

    def build(doc=None):
        one = jiradoc.DescriptionEditor()
        one.resize(600, 400)
        one.set_document(doc or _doc(_para()))
        one.show()
        qapp.processEvents()
        made.append(one)
        return one

    yield build
    for one in made:
        dispose(one)


def _select(editor, start, end):
    cursor = editor.text.textCursor()
    cursor.setPosition(start)
    cursor.setPosition(end, QTextCursor.KeepAnchor)
    editor.text.setTextCursor(cursor)


def _type(editor, text):
    """Typed, with Return for each new line: QTest cannot type "\\n" itself."""
    editor.text.setFocus()
    for index, line in enumerate(text.split("\n")):
        if index:
            QTest.keyClick(editor.text, Qt.Key_Return)
        if line:
            QTest.keyClicks(editor.text, line)


# -- in and out ---------------------------------------------------------------
def test_a_description_loaded_and_read_back_is_the_same_description(qapp):
    document = QTextDocument()
    jiradoc.load(document, EVERYTHING)
    assert _canon(jiradoc.dump(document)) == _canon(EVERYTHING)


def test_an_untouched_editor_has_no_change_to_send(editor):
    one = editor(EVERYTHING)
    assert one.changed_document() is None
    _select(one, 0, 4)
    one.toggle("italic")
    assert one.changed_document() is not None


# -- what the toolbar does ----------------------------------------------------
@pytest.mark.parametrize("name, mark", [("bold", "strong"), ("italic", "em"),
                                        ("underline", "underline"), ("strike", "strike"),
                                        ("code", "code")])
def test_each_mark_on_a_selection_is_the_adf_mark(editor, name, mark):
    one = editor(_doc(_para(_text("make this one"))))
    _select(one, 5, 9)
    one.toggle(name)
    assert one.document()["content"][0]["content"] == [
        _text("make "), _text("this", mark), _text(" one")]
    assert one.buttons[name].isChecked()
    one.toggle(name)
    assert one.document()["content"][0]["content"] == [_text("make this one")]


def test_bold_in_a_heading_is_bold_and_the_heading_s_own_weight_is_not(editor):
    one = editor(_doc({"type": "heading", "attrs": {"level": 2},
                       "content": [_text("Title words")]}))
    _select(one, 0, 5)
    one.toggle("bold")
    assert one.document()["content"][0]["content"] == [_text("Title", "strong"),
                                                       _text(" words")]
    assert one.style_box.currentText() == "Heading 2"


def test_a_paragraph_becomes_a_heading_and_back(editor):
    one = editor(_doc(_para(_text("Steps"))))
    one.style_box.setCurrentIndex(one.style_box.findData(1))
    one.style_box.activated.emit(one.style_box.currentIndex())
    assert one.document()["content"] == [
        {"type": "heading", "attrs": {"level": 1}, "content": [_text("Steps")]}]
    one.style_box.setCurrentIndex(0)
    one.style_box.activated.emit(0)
    assert one.document()["content"] == [_para(_text("Steps"))]


def test_lists_are_made_nested_with_tab_and_taken_out_again(editor):
    one = editor(_doc(_para(_text("one")), _para(_text("two")), _para(_text("three"))))
    _select(one, 0, len("one\ntwo\nthree"))
    one.act("list_bullet")
    cursor = one.text.textCursor()
    cursor.setPosition(len("one\n") + 1)
    one.text.setTextCursor(cursor)
    QTest.keyClick(one.text, Qt.Key_Tab)
    assert one.document()["content"] == [{"type": "bulletList", "content": [
        {"type": "listItem", "content": [_para(_text("one")), {
            "type": "bulletList", "content": [
                {"type": "listItem", "content": [_para(_text("two"))]}]}]},
        {"type": "listItem", "content": [_para(_text("three"))]}]}]
    QTest.keyClick(one.text, Qt.Key_Backtab)
    QTest.keyClick(one.text, Qt.Key_Backtab)
    assert one.document()["content"][1] == _para(_text("two"))


def test_a_numbered_list_and_a_checklist_from_the_toolbar(editor):
    one = editor(_doc(_para(_text("step"))))
    one.act("list_number")
    assert one.document()["content"][0]["type"] == "orderedList"
    assert one.buttons["list_number"].isChecked()
    one.act("list_number")
    assert one.document()["content"] == [_para(_text("step"))]
    one.act("checklist")
    task = one.document()["content"][0]
    assert task["type"] == "taskList" and task["content"][0]["attrs"]["state"] == "TODO"
    assert task["content"][0]["content"] == [_text("step")]


def test_a_checklist_box_ticks_on_a_click_beside_its_words(editor):
    one = editor(_doc({"type": "taskList", "attrs": {"localId": "t"}, "content": [
        {"type": "taskItem", "attrs": {"localId": "i", "state": "TODO"},
         "content": [_text("check me")]}]}))
    block = one.text.document().begin()
    rect = one.text.cursorRect(QTextCursor(block))
    QTest.mouseClick(one.text.viewport(), Qt.LeftButton, Qt.NoModifier,
                     QPoint(rect.left() - 10, rect.center().y()))
    assert one.text.document().begin().blockFormat().marker() == \
        QTextBlockFormat.MarkerType.Checked
    assert one.document()["content"][0]["content"][0]["attrs"]["state"] == "DONE"


def test_a_new_checklist_item_gets_an_id_of_its_own(editor):
    one = editor(_doc({"type": "taskList", "attrs": {"localId": "t"}, "content": [
        {"type": "taskItem", "attrs": {"localId": "i", "state": "TODO"},
         "content": [_text("first")]}]}))
    one.text.moveCursor(QTextCursor.End)
    _type(one, "\nsecond")
    items = one.document()["content"][0]["content"]
    assert [item["content"] for item in items] == [[_text("first")], [_text("second")]]
    assert items[0]["attrs"]["localId"] == "i" and items[1]["attrs"]["localId"] != "i"


def test_enter_at_the_end_of_a_heading_starts_a_paragraph(editor):
    one = editor(_doc({"type": "heading", "attrs": {"level": 1}, "content": [_text("Title")]}))
    one.text.moveCursor(QTextCursor.End)
    _type(one, "\nbody")
    assert one.document()["content"][1] == _para(_text("body"))


def test_a_code_block_holds_lines_and_enter_on_an_empty_last_line_leaves_it(editor):
    one = editor(_doc(_para(_text("x = 1"))))
    one.act("code_block")
    one.text.moveCursor(QTextCursor.End)
    _type(one, "\ny = 2\n")
    QTest.keyClick(one.text, Qt.Key_Return)
    _type(one, "after")
    assert one.document()["content"] == [
        {"type": "codeBlock", "content": [_text("x = 1\ny = 2")]}, _para(_text("after"))]


def test_a_quote_from_the_toolbar(editor):
    one = editor(_doc(_para(_text("said"))))
    one.act("quote")
    assert one.document()["content"] == [{"type": "blockquote",
                                          "content": [_para(_text("said"))]}]


def test_a_link_is_put_on_the_selection_and_taken_off_again(editor, monkeypatch):
    answers = ["qa.example.invalid/browse/QA-1", ""]
    monkeypatch.setattr(widgets, "ask_text", lambda *args, **kw: answers.pop(0))
    one = editor(_doc(_para(_text("see QA-1 now"))))
    _select(one, 4, 8)
    one.act("link")
    assert one.document()["content"][0]["content"][1] == _text("QA-1", LINK)
    _select(one, 4, 8)
    one.act("link")
    assert one.document()["content"][0]["content"] == [_text("see QA-1 now")]


def test_clear_formatting_keeps_the_words_and_the_link(editor):
    one = editor(_doc(_para(_text("bold", "strong", "em"), _text(" link", LINK))))
    _select(one, 0, len("bold link"))
    one.act("clear")
    assert one.document()["content"][0]["content"] == [_text("bold"), _text(" link", LINK)]


def test_pasted_formatting_keeps_only_what_jira_holds(editor):
    one = editor()
    data = QMimeData()
    data.setHtml("<p><b>Bold</b> and <span style='color:#123456'>blue</span></p>"
                 "<ul><li>item</li></ul><img src='x.png'><pre>code line</pre>")
    one.text.setFocus()
    one.text.insertFromMimeData(data)
    content = one.document()["content"]
    assert content[0] == _para(_text("Bold", "strong"), _text(" and blue"))
    assert content[1]["type"] == "bulletList"
    assert {"type": "codeBlock", "content": [_text("code line")]} in content
    assert "x.png" not in json.dumps(content)


def test_the_toolbar_does_nothing_while_the_editor_is_read_only(editor):
    one = editor(_doc(_para(_text("text"))))
    one.setReadOnly(True)
    _select(one, 0, 4)
    one.toggle("bold")
    one.act("list_bullet")
    assert one.document()["content"] == [_para(_text("text"))]
    assert not one.bar.isEnabled()
