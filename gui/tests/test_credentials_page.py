"""Credentials: what Search finds of an account, and never its password."""

import json

import pytest

from cms_gui.pages.credentials import CredentialsPage


@pytest.fixture
def page(qapp, tmp_path, dispose):
    path = tmp_path / "users.json"
    path.write_text(json.dumps([
        {"env": "localhost:8069", "class": "Admin", "login": "admin",
         "password": "s3cret-pass"},
        {"env": "https://shop.example.invalid/", "class": "Agent", "login": "alice",
         "password": "another-s3cret", "run-tests": ["qa_123_open_order"]},
    ]), encoding="utf-8")
    widget = CredentialsPage()
    widget.load(str(path))
    yield widget
    dispose(widget)


def test_search_lists_every_account_but_never_a_password(page):
    hits = page.search_rows()
    assert [h.title for h in hits] == ["admin", "alice"]
    assert hits[1].detail == "Agent · https://shop.example.invalid/ · qa_123_open_order"
    said = " ".join(" ".join((h.title, h.detail, h.status, h.keys)) for h in hits)
    assert "s3cret" not in said


def test_a_found_account_is_selected(page):
    assert page.show_found(("https://shop.example.invalid/", "alice"))
    assert [r.row() for r in page.table.selectionModel().selectedRows()] == [1]
    assert not page.show_found(("localhost:8069", "alice"))
