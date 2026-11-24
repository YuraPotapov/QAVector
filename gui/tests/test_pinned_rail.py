"""The sidebar's pinned links: as many as fit, the rest behind "+N"."""

import pytest

from cms_gui.pinnedrail import PinnedRail

LINKS = [{"provider": key, "target": None, "title": key.title(), "mark": key} for key in ("jira", "agents", "teams")]


@pytest.fixture
def rail(qapp, dispose):
    made = PinnedRail()
    made.resize(200, 300)
    made.show()
    yield made
    dispose(made)


def test_nothing_pinned_takes_no_room(rail):
    rail.set_links([])
    rail.fit(500)
    assert not rail.isVisible()


def test_what_fits_is_shown_and_the_rest_waits_behind_more(rail):
    rail.set_links(LINKS)
    row = rail.row_height()
    rail.fit(row * 3)
    assert [button.isVisible() for button in rail.buttons] == [True, True, True] and not rail.more.isVisible()
    rail.fit(row * 2 + 5)
    assert [button.isVisible() for button in rail.buttons] == [True, False, False]
    assert rail.more.isVisible() and rail.more.text() == "+2 more"
    assert [action.text() for action in rail.more_menu.actions()] == ["Agents", "Teams"]
    rail.fit(0)
    assert not any(button.isVisible() for button in rail.buttons) and rail.more.text() == "+3 more"


def test_a_link_is_opened_from_its_mark_or_from_more_and_unpinned_from_its_menu(rail):
    opened, unpinned = [], []
    rail.opened.connect(opened.append)
    rail.unpinned.connect(unpinned.append)
    rail.set_links(LINKS)
    rail.fit(rail.row_height() * 2)
    rail.buttons[0].click()
    rail.more_menu.actions()[1].trigger()
    assert [one["provider"] for one in opened] == ["jira", "teams"]
    assert rail.buttons[0].text() == "Jira" and rail.buttons[0].toolTip() == "Jira"


def test_folded_it_shows_marks_only(rail):
    from PySide6.QtCore import Qt
    rail.set_links(LINKS)
    rail.set_collapsed(True)
    assert all(button.toolButtonStyle() == Qt.ToolButtonIconOnly for button in rail.buttons)
    rail.set_collapsed(False)
    assert all(button.toolButtonStyle() == Qt.ToolButtonTextBesideIcon for button in rail.buttons)
