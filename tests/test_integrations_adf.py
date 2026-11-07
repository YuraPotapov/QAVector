"""A Cloud description as the description editor holds it, and back again.

Two promises. A document the editor can hold comes back from :func:`editable`
exactly as it was, so opening the editor and saving changes nothing nobody
touched. And what the editor sends is checked by :func:`clean` before Jira sees
it, so a document Jira would refuse - or a link that is not a web address - is
stopped here.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.jira import adf                                       # noqa: E402


def _text(text, *marks):
    node = {"type": "text", "text": text}
    if marks:
        node["marks"] = [mark if isinstance(mark, dict) else {"type": mark} for mark in marks]
    return node


def _para(*content):
    return {"type": "paragraph", "content": list(content)}


def _item(*content):
    return {"type": "listItem", "content": list(content)}


EVERYTHING = {"type": "doc", "version": 1, "content": [
    {"type": "heading", "attrs": {"level": 3}, "content": [_text("What to do")]},
    _para(_text("Module", "strong"), _text(" on prod by "), _text("15.12", "em"),
          _text(" code: "), _text("origin/dev", "code"), {"type": "hardBreak"},
          _text("see "), _text("QA-1", {"type": "link", "attrs": {
              "href": "https://qa.example.invalid/browse/QA-1"}}),
          _text(" gone", "strike"), _text(" under", "underline"),
          _text(" red", {"type": "textColor", "attrs": {"color": "#ff5630"}}),
          {"type": "mention", "attrs": {"id": "acc-1", "text": "@Olena"}},
          {"type": "emoji", "attrs": {"shortName": ":smile:", "text": "x"}},
          {"type": "inlineCard", "attrs": {"url": "https://qa.example.invalid/x"}}),
    {"type": "orderedList", "attrs": {"order": 3}, "content": [
        _item(_para(_text("first")), {"type": "bulletList", "content": [
            _item(_para(_text("nested")))]}),
        _item(_para(_text("second")), _para(_text("more of second")))]},
    {"type": "codeBlock", "attrs": {"language": "python"}, "content": [
        _text("print(1)\nprint(2)")]},
    {"type": "blockquote", "content": [_para(_text("quoted"))]},
    {"type": "rule"},
    {"type": "taskList", "attrs": {"localId": "t1"}, "content": [
        {"type": "taskItem", "attrs": {"localId": "i1", "state": "DONE"},
         "content": [_text("done")]},
        {"type": "taskItem", "attrs": {"localId": "i2", "state": "TODO"},
         "content": [_text("open")]}]},
]}


def test_a_document_the_editor_can_hold_comes_back_as_it_was():
    document, lost = adf.editable(EVERYTHING)
    assert lost == []
    assert document == EVERYTHING


def test_no_description_is_an_empty_document_and_server_s_string_is_not_one():
    assert adf.editable(None) == ({"type": "doc", "version": 1, "content": []}, [])
    assert adf.editable("h1. Wiki markup") == (None, [])


def test_what_the_editor_cannot_hold_is_named_and_its_words_are_kept():
    document, lost = adf.editable({"type": "doc", "version": 1, "content": [
        {"type": "table", "content": [{"type": "tableRow", "content": [
            {"type": "tableCell", "content": [_para(_text("a"))]},
            {"type": "tableCell", "content": [_para(_text("b"))]}]}]},
        {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"id": "m"}}]},
        {"type": "panel", "attrs": {"panelType": "info"}, "content": [_para(_text("inside"))]},
        _para(_text("now "), {"type": "status", "attrs": {"text": "DONE"}},
              _text("high", {"type": "backgroundColor", "attrs": {"color": "#fff"}}))]})
    assert lost == ["highlighting", "panels", "pictures", "status labels", "tables"]
    assert document["content"] == [
        _para(_text("| a | b |")), _para(_text("inside")),
        _para(_text("now "), _text("DONE"), _text("high"))]


def test_a_clean_document_is_sent_as_it_came():
    assert adf.clean(EVERYTHING) == EVERYTHING


def test_code_keeps_only_a_link_beside_it_and_odd_links_are_dropped():
    cleaned = adf.clean({"type": "doc", "content": [_para(
        _text("x", "code", "strong", {"type": "link", "attrs": {"href": "https://a.example"}}),
        _text("y", {"type": "link", "attrs": {"href": "javascript:alert(1)"}}),
        _text("z", {"type": "link", "attrs": {"href": "mailto:qa@example.invalid"}}))]})
    assert cleaned["content"][0]["content"] == [
        _text("x", "code", {"type": "link", "attrs": {"href": "https://a.example"}}),
        _text("y"),
        _text("z", {"type": "link", "attrs": {"href": "mailto:qa@example.invalid"}})]


def test_an_empty_document_clears_the_description():
    assert adf.clean({"type": "doc", "content": [_para(), _para({"type": "hardBreak"})]}) is None


def test_a_list_item_starts_with_a_paragraph_and_a_checklist_gets_its_ids():
    cleaned = adf.clean({"type": "doc", "content": [
        {"type": "bulletList", "content": [_item({"type": "bulletList", "content": [
            _item(_para(_text("inner")))]})]},
        {"type": "taskList", "content": [{"type": "taskItem", "content": [_text("t")]}]}]})
    first = cleaned["content"][0]["content"][0]["content"]
    assert first[0] == {"type": "paragraph", "content": []}
    task = cleaned["content"][1]
    assert task["attrs"]["localId"] and task["content"][0]["attrs"]["state"] == "TODO"


@pytest.mark.parametrize("document, why", [
    ("text", "not a document"),
    ({"type": "doc", "content": [{"type": "table", "content": []}]}, "table where it may not"),
    ({"type": "doc", "content": [_para({"type": "media"})]}, "media inside a line"),
    ({"type": "doc", "content": [{"type": "heading", "attrs": {"level": 9},
                                  "content": [_text("x")]}]}, "level must be 1 to 6"),
    ({"type": "doc", "content": [{"type": "bulletList", "content": []}]}, "at least one item"),
])
def test_a_document_jira_would_refuse_is_stopped_here(document, why):
    with pytest.raises(ValueError) as caught:
        adf.clean(document)
    assert why in str(caught.value)
