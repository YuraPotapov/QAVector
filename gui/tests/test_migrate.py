"""The move from chrome-multi-session's folders and settings to QAVector's.

It runs on a real person's home directory the first time the new version starts,
so every way it can go wrong is pinned here: nothing overwritten, nothing moved
without a way back, nothing touched under $CMS_HOME.
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QSettings

from cms_gui import core, migrate, store

posix_only = pytest.mark.skipif(os.name == "nt", reason="the link is a junction there")


# ------------------------------------------------------------------ folders

@posix_only
def test_a_folder_moves_and_leaves_a_link_behind(tmp_path):
    old, new = tmp_path / "ChromeMultiSession", tmp_path / "QAVector"
    (old / "scripts").mkdir(parents=True)
    (old / "scripts" / "run.py").write_text("print()", encoding="utf-8")
    assert core.move_folder(str(old), str(new)) == str(new)
    assert (new / "scripts" / "run.py").is_file()
    # The old path still reaches the same files: services.json names scripts by it.
    assert old.is_symlink() and (old / "scripts" / "run.py").is_file()


def test_nothing_moves_when_there_is_nothing_to_move(tmp_path):
    new = tmp_path / "QAVector"
    assert core.move_folder(str(tmp_path / "ChromeMultiSession"), str(new)) == str(new)
    assert not new.exists()


def test_an_existing_new_folder_is_never_overwritten(tmp_path):
    old, new = tmp_path / "ChromeMultiSession", tmp_path / "QAVector"
    old.mkdir()
    new.mkdir()
    (old / "users.json").write_text("old", encoding="utf-8")
    assert core.move_folder(str(old), str(new)) == str(new)
    assert (old / "users.json").read_text(encoding="utf-8") == "old"    # left alone
    assert not (new / "users.json").exists()


def test_a_folder_that_cannot_be_linked_is_not_moved(tmp_path, monkeypatch):
    # Moving it without a way back would break every absolute path into it.
    old, new = tmp_path / "ChromeMultiSession", tmp_path / "QAVector"
    old.mkdir()
    (old / "services.json").write_text("{}", encoding="utf-8")

    def refuse(target, link):
        raise OSError("no links here")

    monkeypatch.setattr(core, "_link_folder", refuse)
    assert core.move_folder(str(old), str(new)) == str(old)
    assert (old / "services.json").is_file() and not new.exists()


def test_the_home_folder_is_the_old_one_until_it_has_moved(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert core.home_folder() == str(tmp_path / "QAVector")      # a fresh install
    (tmp_path / "ChromeMultiSession").mkdir()
    assert core.home_folder() == str(tmp_path / "ChromeMultiSession")
    (tmp_path / "QAVector").mkdir()
    assert core.home_folder() == str(tmp_path / "QAVector")


def test_the_history_is_read_where_it_is_until_it_moves(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "data_base", lambda: str(tmp_path))
    (tmp_path / "chrome-multi-session" / "gui").mkdir(parents=True)
    assert store.app_data_dir() == str(tmp_path / "chrome-multi-session" / "gui")
    (tmp_path / "qavector").mkdir()
    assert store.app_data_dir() == str(tmp_path / "qavector" / "gui")


def test_a_test_run_never_touches_the_real_home(tmp_path, monkeypatch):
    # $CMS_HOME is the escape hatch for a second copy and for tests.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CMS_HOME", str(tmp_path / "scratch"))
    (tmp_path / "ChromeMultiSession").mkdir()
    migrate.run()
    assert (tmp_path / "ChromeMultiSession").is_dir()
    assert not (tmp_path / "ChromeMultiSession").is_symlink()
    assert not (tmp_path / "QAVector").exists()


# ----------------------------------------------------------------- settings

def _orgs():
    tag = uuid.uuid4().hex[:8]
    return "test-old-%s" % tag, "test-new-%s" % tag


def test_settings_are_copied_with_their_paths_moved(qapp):
    old_org, new_org = _orgs()
    old = QSettings(old_org, "gui")
    old.setValue("core/config", "/home/u/ChromeMultiSession/users.json")
    old.setValue("flows/path", "/home/u/ChromeMultiSessionOld/flows")    # not ours
    old.setValue("window/dark_mode", "true")
    old.sync()
    moved = [("/home/u/ChromeMultiSession", "/home/u/QAVector")]
    assert migrate.copy_settings(old_org, new_org, "gui", moved)
    new = QSettings(new_org, "gui")
    assert new.value("core/config") == "/home/u/QAVector/users.json"
    assert new.value("flows/path") == "/home/u/ChromeMultiSessionOld/flows"
    assert new.value("window/dark_mode") == "true"


def test_settings_already_in_the_new_place_are_never_overwritten(qapp):
    old_org, new_org = _orgs()
    old = QSettings(old_org, "gui")
    old.setValue("page", "run")
    old.sync()
    new = QSettings(new_org, "gui")
    new.setValue("page", "launch")
    new.sync()
    assert not migrate.copy_settings(old_org, new_org, "gui")
    assert QSettings(new_org, "gui").value("page") == "launch"


def test_a_path_inside_a_json_value_is_rewritten_too():
    # The launch page keeps its whole state as one JSON string.
    text = '{"reports_dir": "/home/u/ChromeMultiSession/reports", "x": 1}'
    assert migrate.rewrite_paths(
        text, [("/home/u/ChromeMultiSession", "/home/u/QAVector")]) == \
        '{"reports_dir": "/home/u/QAVector/reports", "x": 1}'
