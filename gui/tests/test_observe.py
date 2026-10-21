"""The observing pages remembering things, and pointing at the newest one.

Artifacts and Log used to hold only the run in front of them: the report directory
arrived in an event and died with the process, and starting a run cleared the log.
Both now read the run record for what to offer and store what they were last
looking at, so these tests are mostly about what survives - a restart, a new run,
a file that has since been deleted.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import history as history_mod
from cms_gui.pages.artifacts import ArtifactsPage, CURRENT, NO_RUNS
from cms_gui.pages.log import LIVE, LogPage
from cms_gui.settings import Settings


#: The profile folder a run writes its artifacts under. The launcher builds this
#: name with session_dir_for(), which keeps ":" on POSIX and replaces it on
#: Windows because NTFS has no such filename - so a fixture that hardcodes the
#: POSIX spelling cannot even create its own directories there.
SESSION = "localhost_8069-admin" if os.name == "nt" else "localhost:8069-admin"


class FakeSettings:
    """Settings without QSettings, so a test's memory is its own."""

    def __init__(self):
        self.artifacts_dir = ""
        self.log_source = ""


@pytest.fixture
def history(tmp_path, qapp):
    return history_mod.History(directory=str(tmp_path / "gui-data"))


def _run_dir(tmp_path, name, files=("result.json", "screenshot.png")):
    directory = tmp_path / "reports" / name / SESSION / "auth.login"
    directory.mkdir(parents=True)
    for index, filename in enumerate(files):
        path = directory / filename
        path.write_text("x" * (10 + index), encoding="utf-8")
        # Distinct mtimes, oldest first, so "newest" is unambiguous.
        stamp = time.time() - (len(files) - index) * 60
        os.utime(path, (stamp, stamp))
    return str(tmp_path / "reports" / name)


def _record(history, tmp_path, name, log_text=None):
    entry_id = history.begin(history_mod.LAUNCH,
                             {"launch_config": {"environment": "localhost:8069"}})
    fields = {"status": history_mod.OK, "exit_code": 0, "passed": 1, "total": 1,
              "run_dir": _run_dir(tmp_path, name)}
    if log_text is not None:
        path = history.log_path(entry_id)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(log_text)
        fields["log_file"] = path
    history.finish(entry_id, **fields)
    return history.entry(entry_id)


# ---------------------------------------------------------------- Artifacts

def test_it_opens_on_the_last_run_after_a_restart(history, tmp_path, qapp):
    entry = _record(history, tmp_path, "20260813-100000")
    settings = FakeSettings()

    page = ArtifactsPage(settings)
    page.set_history(history)
    assert page._shown_dir == entry["run_dir"]
    assert settings.artifacts_dir == entry["run_dir"]

    # A second session starts with nothing but the stored path, and still lands there.
    again = ArtifactsPage(settings)
    assert again._shown_dir == entry["run_dir"]


def test_the_newest_run_is_the_one_it_opens_on(history, tmp_path, qapp):
    _record(history, tmp_path, "20260813-100000")
    newest = _record(history, tmp_path, "20260813-110000")
    page = ArtifactsPage(FakeSettings())
    page.set_history(history)
    assert page._shown_dir == newest["run_dir"]
    # Newest first, and nothing labels it as such: it is the one already selected.
    assert page.run_combo.currentData() == newest["run_dir"]
    assert page.run_combo.currentIndex() == 0


def test_every_recorded_run_is_offered(history, tmp_path, qapp):
    first = _record(history, tmp_path, "20260813-100000")
    second = _record(history, tmp_path, "20260813-110000")
    page = ArtifactsPage(FakeSettings())
    page.set_history(history)
    offered = [page.run_combo.itemData(i) for i in range(page.run_combo.count())]
    assert offered == [second["run_dir"], first["run_dir"]]


def test_choosing_another_run_switches_the_tree_and_is_remembered(history, tmp_path,
                                                                  qapp):
    older = _record(history, tmp_path, "20260813-100000")
    _record(history, tmp_path, "20260813-110000")
    settings = FakeSettings()
    page = ArtifactsPage(settings)
    page.set_history(history)

    page.run_combo.setCurrentIndex(page.run_combo.findData(older["run_dir"]))
    assert page._shown_dir == older["run_dir"]
    assert settings.artifacts_dir == older["run_dir"]


def test_the_live_run_is_offered_as_the_current_one(history, tmp_path, qapp):
    _record(history, tmp_path, "20260813-100000")
    page = ArtifactsPage(FakeSettings())
    page.set_history(history)
    live = _run_dir(tmp_path, "20260813-live")

    page.set_run_dir(live)
    assert page._shown_dir == live
    assert page.run_combo.currentText() == CURRENT
    assert page.run_combo.currentData() == live


def test_each_file_shows_when_it_was_written(history, tmp_path, qapp):
    """The write time is information; nothing is singled out from it.

    Marking the newest file only made sense against a list of every report at
    once. The picker shows one run at a time, so the column just says when.
    """
    _record(history, tmp_path, "20260813-100000")
    page = ArtifactsPage(FakeSettings())
    page.set_history(history)

    written = []
    for item in page.tree.findItems("", Qt_match_recursive(), 0):
        if item.text(0) in ("result.json", "screenshot.png"):
            written.append(item.text(2))
            assert not item.font(0).bold()
            assert "newest" not in item.text(2)
    assert len(written) == 2
    assert all(len(stamp) == len("00:00:00") for stamp in written)


def test_a_selection_the_user_made_survives_a_rescan(history, tmp_path, qapp):
    _record(history, tmp_path, "20260813-100000")
    page = ArtifactsPage(FakeSettings())
    page.set_history(history)
    target = os.path.join(page._shown_dir, SESSION, "auth.login", "result.json")

    assert page._select_path(target)
    page.rescan()
    assert page._selected_path() == target


def test_a_remembered_folder_that_is_gone_falls_back_to_the_record(history, tmp_path,
                                                                  qapp):
    entry = _record(history, tmp_path, "20260813-100000")
    settings = FakeSettings()
    settings.artifacts_dir = str(tmp_path / "deleted-by-someone")

    page = ArtifactsPage(settings)
    page.set_history(history)
    assert page._shown_dir == entry["run_dir"]


def test_with_no_runs_at_all_it_says_so_rather_than_looking_broken(history, qapp):
    page = ArtifactsPage(FakeSettings())
    page.set_history(history)
    assert page._shown_dir == ""
    assert page.run_combo.currentText() == NO_RUNS
    assert not page.run_combo.isEnabled()
    assert page.tree.topLevelItemCount() == 0


def test_a_folder_opened_by_hand_is_added_to_the_picker(history, tmp_path, qapp):
    page = ArtifactsPage(FakeSettings())
    page.set_history(history)
    somewhere = _run_dir(tmp_path, "opened-by-hand")

    page.show_dir(somewhere)
    assert page.run_combo.currentData() == somewhere
    assert page._shown_dir == somewhere


# ---------------------------------------------------------------- Log

ARCHIVE = ("12:00:00 INFO    fake launcher starting\n"
           "12:00:01 INFO    [localhost:8069-admin] step 0 done\n"
           "12:00:02 ERROR   [localhost:8069-admin] step 1 failed\n"
           "12:00:03 INFO    fake launcher done\n")


def test_archived_logs_are_offered_newest_first(history, tmp_path, qapp):
    older = _record(history, tmp_path, "20260813-100000", log_text=ARCHIVE)
    newest = _record(history, tmp_path, "20260813-110000", log_text=ARCHIVE)
    page = LogPage(FakeSettings())
    page.set_history(history)

    assert page.source.itemText(0) == LIVE
    # Order carries it; no entry is labelled the newest.
    assert page.source.itemData(1) == newest["log_file"]
    assert page.source.itemData(2) == older["log_file"]
    assert "newest" not in page.source.itemText(1)


def test_opening_an_archive_parses_it_back_into_records(history, tmp_path, qapp):
    entry = _record(history, tmp_path, "20260813-100000", log_text=ARCHIVE)
    page = LogPage(FakeSettings())
    page.set_history(history)

    assert page.show_file(entry["log_file"])
    assert len(page._lines) == 4
    # Parsed, not dumped as text: the level and the session came back.
    assert page._lines[2]["level"] == "ERROR"
    assert page._lines[1]["session"] == "localhost:8069-admin"
    # So the session picker works on an archive exactly as it does live.
    assert page.session.count() == 2
    assert page.session.itemText(1) == "localhost:8069-admin"


def test_a_long_session_name_does_not_widen_the_page(qapp):
    """Every page in the stack sets the window's minimum width; a picker as
    wide as its longest session held the window wider than the screen."""
    def shown(session):
        # Filled before it is shown, the way a restored log is: a combo sizes
        # itself to its items when it first appears.
        page = LogPage(FakeSettings())
        if session:
            page.append({"ts": "12:00:00", "level": "INFO", "text": "step",
                         "session": session})
        page.show()     # a hidden layout never recomputes its minimum
        qapp.processEvents()
        width = page.minimumSizeHint().width()
        page.close()
        return page, width

    # The picker grows to its cap and no further.
    _long, long_width = shown("shop.example.invalid-a-very-long-account-name")
    page, longer_width = shown("shop.example.invalid-" + "a-very-long-name-" * 6)
    assert page.session.itemText(1).startswith("shop.example.invalid")
    assert longer_width == long_width


def test_the_filters_apply_to_an_archive(history, tmp_path, qapp):
    entry = _record(history, tmp_path, "20260813-100000", log_text=ARCHIVE)
    page = LogPage(FakeSettings())
    page.set_history(history)
    page.show_file(entry["log_file"])

    page.level.set_current("ERROR")
    assert "step 1 failed" in page.view.toPlainText()
    assert "step 0 done" not in page.view.toPlainText()


def test_an_archive_says_it_is_one_and_disables_what_makes_no_sense(history, tmp_path,
                                                                   qapp):
    entry = _record(history, tmp_path, "20260813-100000", log_text=ARCHIVE)
    page = LogPage(FakeSettings())
    page.set_history(history)
    page.show_file(entry["log_file"])

    assert "archived log" in page.note.text()
    # Following and clearing belong to a log that is still growing.
    assert not page.follow.isEnabled()
    assert not page.clear_button.isEnabled()

    page.show_live()
    assert "archived log" not in page.note.text()
    assert page.follow.isEnabled() and page.clear_button.isEnabled()


def test_live_lines_are_kept_while_an_archive_is_on_screen(history, tmp_path, qapp):
    entry = _record(history, tmp_path, "20260813-100000", log_text=ARCHIVE)
    page = LogPage(FakeSettings())
    page.set_history(history)
    page.show_file(entry["log_file"])

    page.append({"ts": "13:00:00", "level": "INFO", "session": "", "text": "live one"})
    # Not mixed into what is being read...
    assert "live one" not in page.view.toPlainText()
    # ...but not thrown away either.
    page.show_live()
    assert "live one" in page.view.toPlainText()


def test_a_new_run_brings_the_page_back_to_the_live_log(history, tmp_path, qapp):
    entry = _record(history, tmp_path, "20260813-100000", log_text=ARCHIVE)
    page = LogPage(FakeSettings())
    page.set_history(history)
    page.show_file(entry["log_file"])

    page.clear()          # what MainWindow.start_run does
    assert page._showing == ""
    assert page.source.currentText() == LIVE
    assert page.view.toPlainText() == ""


def test_the_chosen_log_is_remembered_across_a_restart(history, tmp_path, qapp):
    entry = _record(history, tmp_path, "20260813-100000", log_text=ARCHIVE)
    settings = FakeSettings()
    page = LogPage(settings)
    page.set_history(history)
    page.show_file(entry["log_file"])
    assert settings.log_source == entry["log_file"]

    again = LogPage(settings)
    assert again._showing == entry["log_file"]
    assert len(again._lines) == 4


def test_a_remembered_log_that_is_gone_is_ignored(history, tmp_path, qapp):
    settings = FakeSettings()
    settings.log_source = str(tmp_path / "deleted.log")
    page = LogPage(settings)
    page.set_history(history)
    assert page._showing == ""


def test_a_log_too_large_to_read_is_refused_rather_than_hanging(history, tmp_path,
                                                               qapp, monkeypatch):
    from cms_gui.pages import log as log_page

    entry = _record(history, tmp_path, "20260813-100000", log_text=ARCHIVE)
    monkeypatch.setattr(log_page, "MAX_ARCHIVE_BYTES", 4)
    assert log_page._read_archive(entry["log_file"]) is None


def test_round_tripping_a_live_log_through_the_archive_keeps_it_intact(history,
                                                                      tmp_path, qapp):
    """write_to and _read_archive have to agree, or history's logs read as noise."""
    live = LogPage(FakeSettings())
    for record in ({"ts": "12:00:00", "level": "INFO", "session": "s1",
                    "text": "hello"},
                   {"ts": "12:00:01", "level": "WARNING", "session": "",
                    "text": "careful"}):
        live.append(record)
    path = str(tmp_path / "out.log")
    assert live.write_to(path)

    from cms_gui.pages.log import _read_archive
    back = _read_archive(path)
    assert [(r["ts"], r["level"], r["session"], r["text"]) for r in back] == [
        ("12:00:00", "INFO", "s1", "hello"),
        ("12:00:01", "WARNING", "", "careful")]


# ---------------------------------------------------------------- wiring

def test_the_real_settings_carry_both_values(qapp):
    settings = Settings()
    settings.artifacts_dir = "/tmp/some-run"
    settings.log_source = "/tmp/some.log"
    fresh = Settings()
    assert fresh.artifacts_dir == "/tmp/some-run"
    assert fresh.log_source == "/tmp/some.log"
    settings.artifacts_dir = ""
    settings.log_source = ""


def Qt_match_recursive():
    from PySide6.QtCore import Qt
    return Qt.MatchContains | Qt.MatchRecursive
