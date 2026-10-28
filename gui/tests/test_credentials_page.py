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


# ------------------------------------------- nothing lost, nothing written over

def test_an_unreadable_file_keeps_its_error_and_cannot_be_saved_over(qapp, tmp_path,
                                                                     dispose):
    """Saving an empty table over it would erase every password - and the
    launcher refuses an empty list, so every launch would stop too."""
    path = tmp_path / "users.json"
    path.write_text('[{"env": "localhost:8069", "class": "A",', encoding="utf-8")
    widget = CredentialsPage()
    try:
        widget.load(str(path))
        assert "not valid JSON" in widget.status.text()
        assert "Reload" in widget.status.text()
        assert not widget.save_button.isEnabled()
        assert not widget.add_button.isEnabled()
        assert not widget.table.isEnabled()
        widget.save()                       # even if asked directly
        assert path.read_text(encoding="utf-8").startswith('[{"env"')
    finally:
        dispose(widget)


def test_a_refresh_keeps_an_edit_that_is_not_saved_yet(page, tmp_path):
    page.table.setCurrentCell(0, page.COL_LOGIN)
    page.table.item(0, page.COL_LOGIN).setText("root")
    assert page.is_dirty()
    assert page.save_button.property("dirty") == "true"
    page.refresh(page._path)                # what saving a scenario elsewhere does
    assert page.rows()[0].login == "root"


def test_a_refresh_says_when_the_file_changed_under_the_edit(page):
    page.table.item(0, page.COL_LOGIN).setText("root")
    with open(page._path, "a", encoding="utf-8") as handle:
        handle.write("\n")                  # someone else wrote it meanwhile
    page.refresh(page._path)
    assert page.rows()[0].login == "root"
    assert "changed on disk" in page.status.text()


def test_without_an_edit_a_refresh_reads_the_file(page):
    rows = json.loads(open(page._path, encoding="utf-8").read())
    rows[0]["login"] = "renamed"
    with open(page._path, "w", encoding="utf-8") as handle:
        json.dump(rows, handle)
    page.refresh(page._path)
    assert page.rows()[0].login == "renamed"
    assert not page.is_dirty()


def test_reload_asks_before_dropping_an_edit(page, monkeypatch):
    from cms_gui import widgets
    page.table.item(0, page.COL_LOGIN).setText("root")
    monkeypatch.setattr(widgets, "confirm", lambda *a, **k: False)
    page.reload()
    assert page.rows()[0].login == "root"
    monkeypatch.setattr(widgets, "confirm", lambda *a, **k: True)
    page.reload()
    assert page.rows()[0].login == "admin" and not page.is_dirty()


def test_a_new_row_takes_the_selected_rows_environment(page):
    page.table.setCurrentCell(0, page.COL_CLASS)
    page.add_row()
    assert page.rows()[-1].env == "localhost:8069"
    page.table.clearSelection()
    page.table.setCurrentCell(-1, -1)
    page.add_row()
    assert page.rows()[-1].env == ""


def test_saving_settles_the_edit(page):
    page.table.item(0, page.COL_LOGIN).setText("root")
    page.save()
    assert not page.is_dirty()
    assert json.loads(open(page._path, encoding="utf-8").read())[0]["login"] == "root"
