"""Credentials: one row per person - role, login, password - and never an
environment's key, nor, in Search, a password."""

import collections
import json

import pytest

from cms_gui.pages.credentials import CredentialsPage

LOCAL, SHOP = "localhost:8069", "https://shop.example.invalid/"

Env = collections.namedtuple("Env", "account_key name")


@pytest.fixture
def page(qapp, tmp_path, dispose):
    path = tmp_path / "users.json"
    path.write_text(json.dumps([
        {"env": LOCAL, "class": "Admin", "login": "admin", "password": "s3cret-pass"},
        {"env": SHOP, "class": "Agent", "login": "alice",
         "password": "another-s3cret", "run-tests": ["qa_123_open_order"]},
        {"env": LOCAL, "class": "Agent", "login": "alice", "password": "another-s3cret"},
    ]), encoding="utf-8")
    widget = CredentialsPage()
    widget.set_environments([Env(LOCAL, "Local"), Env(SHOP, "Shop"),
                             Env("staging", "Staging")])
    widget.load(str(path))
    yield widget
    dispose(widget)


def _cells(page, col):
    return [page.table.item(row, col).text() for row in range(page.table.rowCount())]


def _saved(page):
    return json.loads(open(page._path, encoding="utf-8").read())


# ------------------------------------------------------------- what is shown

def test_one_row_per_person_and_no_environment_column(page):
    assert [page.table.horizontalHeaderItem(c).text()
            for c in range(page.table.columnCount())] == ["Role", "Login", "Password", ""]
    assert _cells(page, page.COL_LOGIN) == ["admin", "alice"]
    shown = " ".join(" ".join(_cells(page, col)) for col in range(3))
    assert LOCAL not in shown and SHOP not in shown and "Local" not in shown
    assert page.table.item(1, page.COL_LOGIN).toolTip() == "Signs in to Shop, Local"


def test_search_lists_every_user_but_never_a_password(page):
    hits = page.search_rows()
    assert [h.title for h in hits] == ["admin", "alice"]
    assert hits[1].detail == "Agent · Shop, Local"
    said = " ".join(" ".join((h.title, h.detail, h.status, h.keys)) for h in hits)
    assert "s3cret" not in said


def test_a_found_user_is_selected(page):
    assert page.show_found(("alice", "Agent"))
    assert [r.row() for r in page.table.selectionModel().selectedRows()] == [1]
    assert not page.show_found(("alice", "Admin"))


# ------------------------------------------------------------- changing people

def test_a_change_to_a_person_reaches_every_environment_of_theirs(page):
    page._toggle_reveal(1)
    page.table.item(1, page.COL_PASSWORD).setText("new-pass")
    page.save()
    assert [row["password"] for row in _saved(page) if row["login"] == "alice"] == \
        ["new-pass", "new-pass"]


def test_making_two_people_the_same_makes_them_one(page, qapp):
    page.table.item(0, page.COL_LOGIN).setText("root")
    page.table.item(0, page.COL_CLASS).setText("Agent")
    page._toggle_reveal(0)
    page.table.item(0, page.COL_PASSWORD).setText("another-s3cret")
    page.table.item(0, page.COL_LOGIN).setText("alice")
    qapp.processEvents()
    # Same login, password and role in the same place: the launcher refuses it.
    assert page.table.rowCount() == 1
    assert "duplicates" in page.status.text()
    assert not page.save_button.isEnabled()


def test_a_new_user_is_in_no_environment_and_says_what_it_lacks(page):
    page.add_row()
    assert page.rows()[-1].env == ""
    assert page.table.item(2, page.COL_LOGIN).toolTip() == "In no environment yet"
    assert "A new user: role is required." in page.status.text()
    assert not page.save_button.isEnabled()


def test_duplicate_keeps_role_and_password_and_nothing_else(page):
    page.table.selectRow(1)
    page.duplicate_row()
    copy = page.rows()[-1]
    assert (copy.env, copy.cls, copy.login, copy.password) == (
        "", "Agent", "", "another-s3cret")


def test_delete_takes_a_person_out_of_every_environment(page):
    page.delete_row(1)
    page.save()
    assert [row["login"] for row in _saved(page)] == ["admin"]


# ------------------------------------------------------------- where they sign in

def test_several_people_are_added_to_one_environment(page, monkeypatch):
    from cms_gui import widgets
    monkeypatch.setattr(widgets, "ask_item", lambda *a, **k: "Staging")
    page.table.selectAll()
    page.add_to_environment()
    assert [row.login for row in page.rows() if row.env == "staging"] == \
        ["admin", "alice"]
    monkeypatch.setattr(widgets, "ask_item", lambda *a, **k: "Local")
    page.remove_from_environment()
    assert [(row.env, row.login) for row in page.rows()] == [
        (SHOP, "alice"), ("staging", "admin"), ("staging", "alice")]


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
    rows = _saved(page)
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


def test_saving_settles_the_edit(page):
    page.table.item(0, page.COL_LOGIN).setText("root")
    page.save()
    assert not page.is_dirty()
    assert _saved(page)[0]["login"] == "root"


def test_export_asks_for_the_selected_users(page):
    asked = []
    page.export_requested.connect(asked.append)
    page.table.selectRow(1)
    page.export_button.click()
    assert asked == [{"users": [("alice", "another-s3cret", "Agent")]}]
