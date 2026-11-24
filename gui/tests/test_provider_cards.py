"""Integrations' catalog as provider cards: what a card says, how the grid lays
them out, and what clicking one does.

* A card's standing is what the page read: not read yet is said so, never 0.
* Three to a row when there is room, fewer when not; never holding the window wide.
* Cards in a row are one size, and their figures stand at one height.
* Each provider wears its maker's mark; Agents the program used last.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QPoint, QRectF, QSettings, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtTest import QTest

from cms_gui import brandmarks
from cms_gui.pages import integrations as integrations_module
from cms_gui.pages.integrations import IntegrationsPage
from cms_gui.pages.providercards import Provider, ProviderGrid, standing_of
from cms_gui.settings import Settings

from test_integrations_page import FakeCore, _connection

PROVIDERS = [Provider(*one) for one in integrations_module.PROVIDERS]


@pytest.fixture
def settings(tmp_path):
    made = Settings()
    made._qs = QSettings(str(tmp_path / "gui.ini"), QSettings.IniFormat)
    return made


@pytest.fixture
def grid(qapp, dispose):
    made = []

    def build(width=1200):
        shown = ProviderGrid(PROVIDERS)
        shown.resize(width, 420)
        shown.show()
        qapp.processEvents()
        made.append(shown)
        return shown

    yield build
    for one in made:
        dispose(one)


@pytest.fixture
def page(qapp, settings, dispose):
    made = []

    def build(core=None):
        shown = IntegrationsPage(settings)
        shown.resize(1200, 800)
        shown.set_core(core or FakeCore())
        shown.show_catalog()
        shown.show()
        qapp.processEvents()
        made.append(shown)
        return shown

    yield build
    for one in made:
        dispose(one)


# -- what a card says ---------------------------------------------------------------
def test_a_provider_not_read_yet_says_so_rather_than_nought():
    standing = standing_of([], listed=False)
    assert standing.count is None and standing.tag == "NOT READ YET" and standing.state == "idle"


def test_a_provider_with_no_connection_is_not_set_up():
    standing = standing_of([], listed=True)
    assert (standing.count, standing.tag, standing.state) == (0, "NOT SET UP", "idle")


def test_a_connection_without_its_token_needs_attention():
    standing = standing_of([_connection(), _connection("b", "B", token=False)], listed=True)
    assert (standing.count, standing.state, standing.tag) == (2, "warn", "NEEDS ATTENTION")
    assert standing.detail == "1 without a token"


def test_changes_waiting_for_the_person_are_told_on_the_card():
    standing = standing_of([_connection()], listed=True, waiting=2)
    assert standing.state == "warn" and standing.tag == "2 WAITING"
    assert standing.detail == "2 changes agents asked for wait for you"


def test_ready_connections_and_agent_programs_by_name():
    assert standing_of([_connection()], listed=True).detail == "The connection is ready"
    agents = [{"id": "a", "kind": "claude"}, {"id": "b", "kind": "codex"}, {"id": "c", "kind": "claude"}]
    standing = standing_of(agents, listed=True, kinds=True)
    assert (standing.state, standing.detail) == ("ok", "Claude, Codex")


def test_the_card_shows_its_standing_and_names_set_up_when_there_is_nothing(grid):
    shown = grid()
    card = shown.cards["jira"]
    assert card.count.text() == "—" and card.tag.text() == "NOT READ YET"
    card.set_standing(standing_of([], listed=True))
    assert card.count.text() == "0" and card.open_button.text() == "Set up"
    card.set_standing(standing_of([_connection()], listed=True))
    assert card.count.text() == "1" and card.open_button.text() == "Open"
    assert [chip.text() for chip in card.chips] == ["READ", "SEARCH", "CHANGE", "AGENTS", "CYCLES"]


# -- the grid -----------------------------------------------------------------------
@pytest.mark.parametrize("width, columns", [(1200, 3), (760, 2), (420, 1)])
def test_the_grid_drops_columns_as_the_page_narrows(grid, width, columns):
    assert grid(width).columns() == columns


def test_the_grid_never_holds_the_window_wide(grid):
    assert grid().minimumSizeHint().width() == ProviderGrid.CARD_WIDTH


def test_cards_in_a_row_are_one_size_and_their_figures_one_height(grid, qapp):
    assert all(card.minimumSizeHint().width() <= ProviderGrid.CARD_WIDTH for card in grid().cards.values())
    shown = grid(1200)
    shown.cards["teams"].set_standing(standing_of([_connection()], listed=True))
    qapp.processEvents()
    cards = list(shown.cards.values())
    assert len({card.geometry().height() for card in cards}) == 1
    assert max(card.width() for card in cards) - min(card.width() for card in cards) <= 1
    tops = {card.count.mapTo(card, QPoint(0, 0)).y() for card in cards}
    assert len(tops) == 1


def test_a_card_alone_in_its_row_keeps_the_figures_where_the_others_have_them(grid, qapp):
    shown = grid(760)                      # two columns: Agents alone on the second row
    qapp.processEvents()
    jira, agents = shown.cards["jira"], shown.cards["agents"]
    assert jira.count.mapTo(jira, QPoint(0, 0)).y() == agents.count.mapTo(agents, QPoint(0, 0)).y()


# -- what clicking does ---------------------------------------------------------------
def test_a_click_picks_a_card_a_double_click_or_enter_opens_it(grid, qapp):
    shown = grid()
    opened, detached = [], []
    shown.open_requested.connect(opened.append)
    shown.detach_requested.connect(detached.append)
    assert shown.selected() == "jira"
    teams = shown.cards["teams"]
    QTest.mouseClick(teams, Qt.LeftButton, pos=QPoint(10, 10))
    assert shown.selected() == "teams" and not shown.cards["jira"].selected()
    QTest.mouseDClick(teams, Qt.LeftButton, pos=QPoint(10, 10))
    QTest.keyClick(shown.cards["agents"], Qt.Key_Return)
    assert opened == ["teams", "agents"]
    shown.cards["jira"].window_button.click()
    shown.cards["teams"].open_button.click()
    assert detached == ["jira"] and opened[-1] == "teams"


def test_the_page_opens_the_card_picked_and_detaches_from_its_corner(page, monkeypatch):
    shown = page()
    shown.cards.pick("teams")
    assert shown.selected_provider() == "teams"
    asked = []
    monkeypatch.setattr(shown, "detach_provider", asked.append)
    shown.cards.detach_requested.disconnect()
    shown.cards.detach_requested.connect(shown.detach_provider)
    shown.cards.cards["agents"].window_button.click()
    assert asked == ["agents"]
    shown.open_selected()
    assert shown.stack.currentWidget() is shown.teams


def test_the_page_counts_connections_and_says_unread_ones_as_a_dash(page):
    shown = page(FakeCore(connections=[_connection(), _connection("b", "B", token=False)]))
    jira = shown.cards.cards["jira"]
    assert jira.count.text() == "2" and jira.tag.text() == "NEEDS ATTENTION"
    assert shown.tiles["providers"].value.text() == "3"
    assert shown.tiles["waiting"].value.text() == "0"


# -- the makers' marks ------------------------------------------------------------------
def _painted(name):
    image = QImage(48, 48, QImage.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    brandmarks.paint(painter, name, QRectF(0, 0, 48, 48))
    painter.end()
    return sum(1 for x in range(48) for y in range(48) if image.pixelColor(x, y).alpha())


@pytest.mark.parametrize("name", sorted(brandmarks.MARKS))
def test_every_mark_draws_something(qapp, name):
    assert _painted(name) > 200


def test_an_unknown_mark_draws_nothing(qapp):
    assert _painted("nobody") == 0


def test_every_provider_has_a_mark():
    assert all(provider.mark in brandmarks.MARKS for provider in PROVIDERS)


def test_agents_wears_the_mark_of_the_program_used_last(page):
    core = FakeCore()
    asked = []

    def agents(*args, document=None, cancel=None, **_rest):
        asked.append((args, document))
        if args[0] == "sessions":
            return {"ok": True, "sessions": [{"id": "s1", "kind": "codex"}]}
        return {"ok": True, "connections": [{"id": "codex", "name": "Codex", "kind": "codex"}]}
    core.agents = agents
    shown = page(core)
    assert shown.cards.cards["agents"].mark.name == "codex"
    # One conversation is read for it, the newest; no program is started.
    assert (("sessions",), {"limit": 1}) in asked


def test_agents_wears_both_marks_before_any_conversation(page):
    core = FakeCore()
    core.agents = lambda *args, document=None, cancel=None, **_rest: (
        {"ok": True, "sessions": []} if args[0] == "sessions" else {"ok": True, "connections": []})
    shown = page(core)
    assert shown.cards.cards["agents"].mark.name == "agents"
    shown.cards.cards["agents"].set_mark("someone-else")
    assert shown.cards.cards["agents"].mark.name == "agents"


def test_switching_projects_on_the_catalog_reads_the_new_project_s_connections(page, qapp):
    """The user's report (2026-11-18): after a project switch every card said
    NOT READ YET until the page was left and shown again."""
    shown = page(FakeCore(connections=[_connection()]))
    assert shown.cards.cards["jira"].count.text() == "1"
    shown.set_core(FakeCore(connections=[_connection(), _connection("b", "B"), _connection("c", "C")]))
    qapp.processEvents()
    assert shown.stack.currentWidget() is shown.catalog
    assert shown.cards.cards["jira"].count.text() == "3"
    assert shown.cards.cards["jira"].tag.text() == "CONNECTED"


# -- pinned to the sidebar ------------------------------------------------------------
def test_a_card_s_pin_puts_its_provider_on_the_sidebar_and_takes_it_off(page, settings):
    shown = page()
    told = []
    shown.pinned_changed.connect(lambda: told.append(True))
    jira = shown.cards.cards["jira"]
    assert not jira.pin_button.isChecked() and jira.pin_button.text() == ""
    assert jira.pin_button.toolTip() == "Pin Jira to the sidebar"
    jira.pin_button.click()
    assert settings.pinned_links() == [{"provider": "jira", "target": None}] and told == [True]
    assert [(one["provider"], one["title"], one["mark"]) for one in shown.pinned()] == [("jira", "Jira", "jira")]
    assert jira.pin_button.toolTip() == "Take Jira off the sidebar"
    shown.cards.cards["teams"].pin_button.click()
    assert [one["provider"] for one in shown.pinned()] == ["jira", "teams"]       # in the order pinned
    shown.unpin({"provider": "jira", "target": None})
    assert not jira.pin_button.isChecked() and [one["provider"] for one in shown.pinned()] == ["teams"]
    jira.pin_button.click()
    jira.pin_button.click()
    assert [one["provider"] for one in shown.pinned()] == ["teams"]


def test_pins_are_the_application_s_and_unknown_providers_are_left_out(page, settings):
    settings.save_pinned_links([{"provider": "teams", "target": None}, {"provider": "gone", "target": None},
                                "junk"])
    shown = page()
    assert shown.cards.cards["teams"].pin_button.isChecked()
    assert [one["provider"] for one in shown.pinned()] == ["teams"]


def test_a_link_opens_its_provider_as_open_does(page):
    shown = page()
    assert shown.open_link({"provider": "teams", "target": None}) is True
    assert shown.stack.currentWidget() is shown.hosts["teams"].view
    assert shown.open_link({"provider": "nowhere", "target": None}) is False


def test_the_agents_card_s_new_mark_reaches_the_sidebar(page, settings):
    settings.save_pinned_links([{"provider": "agents", "target": None}])
    shown = page()
    told = []
    shown.pinned_changed.connect(lambda: told.append(True))
    shown.cards.cards["agents"].set_mark("codex")
    assert told == [True] and shown.pinned()[0]["mark"] == "codex"
