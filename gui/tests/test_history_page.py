"""The History page: what it says with nothing picked, and how it filters.

The filters ask what happened - how a run ended, where, and when - rather than
which page started it, and the details no longer carry the command line: an
entry is for running again, not for copying out.
"""

import datetime

import pytest
from PySide6.QtWidgets import QLabel

from cms_gui import history as history_mod
from cms_gui.pages import history as page_mod

NOW = datetime.datetime(2026, 9, 11, 15, 0, 0)


@pytest.fixture
def record(qapp, tmp_path):
    return history_mod.History(str(tmp_path))


@pytest.fixture
def page(record, dispose):
    widget = page_mod.HistoryPage(record)
    yield widget
    dispose(widget)


def _add(record, status=history_mod.OK, env="localhost", kind=history_mod.LAUNCH,
         started="2026-09-11 09:00:00", **extra):
    if kind == history_mod.LAUNCH:
        payload = {"launch_config": {"environment": env}}
    else:
        payload = {"command_state": {"--env": env}}
    payload["started_at"] = started
    payload.update(extra)
    entry_id = record.begin(kind, payload)
    record.finish(entry_id, status=status)
    return entry_id


def _shown(page):
    return [entry.get("id") for entry in page._rows]


# -- nothing selected ---------------------------------------------------------
def test_nothing_selected_is_an_empty_state_not_a_blank_heading(page, record):
    """It was an empty heading beside a blank status pill, read as a disabled box."""
    assert page.detail_stack.currentIndex() == 0
    assert "No run selected" in " ".join(
        label.text() for label in page.empty_state.findChildren(QLabel))
    _add(record)
    page.table.selectRow(0)
    assert page.detail_stack.currentIndex() == 1
    assert page.status_tag.text()
    page.table.clearSelection()
    assert page.detail_stack.currentIndex() == 0


def test_the_details_carry_no_command_line_and_nothing_to_copy_it(page, record):
    _add(record, display_command="qavector-core --a-very-particular-flag",
         argv=["--a-very-particular-flag"])
    page.table.selectRow(0)
    assert not hasattr(page, "command_line")
    assert not hasattr(page, "copy_button")
    assert all("a-very-particular-flag" not in label.text()
               for label in page.findChildren(QLabel))


# -- filters ------------------------------------------------------------------
def test_the_result_filter_keeps_what_ended_that_way(page, record):
    ids = {status: _add(record, status=status)
           for status in (history_mod.OK, history_mod.FAILED, history_mod.ERROR,
                          history_mod.STOPPED, history_mod.RUNNING)}
    page.result_filter.set_current("Passed")
    assert _shown(page) == [ids[history_mod.OK]]
    # Failed takes error too: both say "this did not pass".
    page.result_filter.set_current("Failed")
    assert set(_shown(page)) == {ids[history_mod.FAILED], ids[history_mod.ERROR]}
    page.result_filter.set_current("Stopped")
    assert _shown(page) == [ids[history_mod.STOPPED]]
    page.result_filter.set_current("All")
    assert len(_shown(page)) == 5
    assert page.count.text() == "5 shown of 5 recorded"


def test_the_environment_filter_offers_what_the_record_has(page, record):
    local = _add(record, env="localhost")
    dev = _add(record, env="claim-dev", kind=history_mod.COMMAND)
    everywhere = _add(record, env="")
    combo = page.environment_filter
    assert [combo.itemText(i) for i in range(combo.count())] == [
        page_mod.ALL_ENVIRONMENTS, page_mod.EVERY_ENVIRONMENT, "claim-dev", "localhost"]
    combo.setCurrentIndex(combo.findData("claim-dev"))
    assert _shown(page) == [dev]
    combo.setCurrentIndex(combo.findData(""))
    assert _shown(page) == [everywhere]
    combo.setCurrentIndex(0)
    assert set(_shown(page)) == {local, dev, everywhere}


def test_the_environment_choice_survives_a_new_entry(page, record):
    first = _add(record, env="localhost")
    _add(record, env="claim-dev")
    combo = page.environment_filter
    combo.setCurrentIndex(combo.findData("localhost"))
    second = _add(record, env="localhost")          # refreshes the page
    assert combo.currentData() == "localhost"
    assert set(_shown(page)) == {first, second}
    assert page.count.text() == "2 shown of 3 recorded"


def test_a_chosen_environment_with_nothing_left_falls_back_to_all(page, record):
    _add(record, env="localhost")
    gone = _add(record, env="claim-dev")
    combo = page.environment_filter
    combo.setCurrentIndex(combo.findData("claim-dev"))
    record.remove(gone)
    assert combo.currentData() is None
    assert len(_shown(page)) == 1


def test_the_period_filter_reaches_back_as_far_as_it_says():
    today = {"started_at": "2026-09-11 09:00:00", "status": history_mod.OK}
    earlier = {"started_at": "2026-09-08 09:00:00", "status": history_mod.OK}
    old = {"started_at": "2026-08-30 09:00:00", "status": history_mod.OK}
    kept = lambda period: [e for e in (today, earlier, old)
                           if page_mod.matches(e, period=period, now=NOW)]
    assert kept("Today") == [today]
    assert kept("7 days") == [today, earlier]
    assert kept("All time") == [today, earlier, old]


def test_no_filter_keeps_everything_even_what_cannot_be_dated():
    odd = {"started_at": "not a date", "status": history_mod.RUNNING}
    assert page_mod.matches(odd, now=NOW)
    # ...but a period cannot place it, so a period leaves it out.
    assert not page_mod.matches(odd, period="Today", now=NOW)


def test_an_entry_names_its_environment_whichever_page_it_came_from():
    launch_entry = {"kind": history_mod.LAUNCH,
                    "launch_config": {"environment": "localhost"}}
    command_entry = {"kind": history_mod.COMMAND, "command_state": {"--env": "dev"}}
    assert page_mod.entry_environment(launch_entry) == "localhost"
    assert page_mod.entry_environment(command_entry) == "dev"
    assert page_mod.entry_environment({"kind": history_mod.LAUNCH}) == ""
