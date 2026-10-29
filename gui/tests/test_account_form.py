"""+ Account: one account for the environment on screen, saved and chosen."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QLineEdit

from cms_gui import launchprojectsfile as lpf
from cms_gui.pages.launchaccountform import AccountForm

LOCAL = lpf.Environment("env:localhost:8069", lpf.WORKSPACE, "Local", "localhost:8069",
                        "", {}, "http://localhost:8069", 1, True)


@pytest.fixture
def users(tmp_path):
    path = tmp_path / "users.json"
    path.write_text(json.dumps([
        {"env": "localhost:8069", "class": "Admin", "login": "admin",
         "password": "pw", "note": "kept as it is"},
        {"env": "https://staging.example.invalid/", "class": "Agent",
         "login": "agent", "password": "pw2"}]), encoding="utf-8")
    return path


@pytest.fixture
def form(qapp, users):
    widget = AccountForm(None, LOCAL, str(users))
    yield widget
    widget.close()


def _fill(form, role="Operator", login="operator@example.invalid", password="s3cret"):
    form.role.setText(role)
    form.login.setText(login)
    form.password.setText(password)


def test_it_saves_the_account_into_this_environment_and_keeps_the_rest(form, users):
    _fill(form)
    form.tests.setText("shop_order, tag:smoke")
    assert form.save()
    rows = json.loads(users.read_text(encoding="utf-8"))
    assert rows[-1] == {"env": "localhost:8069", "class": "Operator",
                        "login": "operator@example.invalid", "password": "s3cret",
                        "run-tests": ["shop_order", "tag:smoke"]}
    assert rows[0]["note"] == "kept as it is" and len(rows) == 3
    assert form.saved_login == "operator@example.invalid"


def test_a_login_this_environment_already_has_is_refused(form, users):
    before = users.read_text(encoding="utf-8")
    _fill(form, login="admin")
    assert not form.save()
    assert "unique" in form.problem.text() or "duplicates" in form.problem.text()
    assert users.read_text(encoding="utf-8") == before


def test_the_same_login_in_another_environment_is_fine(form):
    _fill(form, login="agent")
    assert form.save()


def test_a_missing_field_is_said_not_written(form, users):
    before = users.read_text(encoding="utf-8")
    _fill(form, password="")
    assert not form.save()
    assert "password" in form.problem.text()
    assert users.read_text(encoding="utf-8") == before


def test_an_unreadable_users_file_is_not_written_over(qapp, tmp_path):
    path = tmp_path / "users.json"
    path.write_text("[{broken", encoding="utf-8")
    form = AccountForm(None, LOCAL, str(path))
    try:
        _fill(form)
        assert not form.save()
        assert "not valid JSON" in form.problem.text()
        assert path.read_text(encoding="utf-8") == "[{broken"
    finally:
        form.close()


def test_with_no_users_file_configured_it_says_where_to_set_one(qapp):
    form = AccountForm(None, LOCAL, "")
    try:
        _fill(form)
        assert not form.save() and "Settings" in form.problem.text()
    finally:
        form.close()


def test_the_password_is_masked_until_shown(form):
    assert form.password.echoMode() == QLineEdit.Password
    form.reveal.setChecked(True)
    assert form.password.echoMode() == QLineEdit.Normal and form.reveal.text() == "Hide"
    form.reveal.setChecked(False)
    assert form.password.echoMode() == QLineEdit.Password


def test_the_environment_is_said_not_asked(form):
    from PySide6.QtWidgets import QLabel
    said = " ".join(label.text() for label in form.findChildren(QLabel))
    assert "Local" in said
    # The account key is the GUI's business, never the person's.
    assert "localhost:8069" not in said
    assert not any(isinstance(w, QLineEdit) and w.text() == "localhost:8069"
                   for w in form.findChildren(QLineEdit))
