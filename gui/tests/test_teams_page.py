"""Integrations -> Teams (QCR ADR-015 D6): the page's side.

* Only Teams' channels, of every provider's connections; the webhook shown by
  its host, never asked back.
* New post: the card as it will look; Send only with text and names the
  channel knows, and only after asking; a refusal (one post a minute) is said.
* History: read when its tab is opened; Send again asks first.
* Connections: read-only until Edit; Save sends a webhook only when one was
  typed, and the default access as the core spells it.
"""

import pytest

from cms_gui import widgets
from cms_gui.pages import teams as teams_page

RELEASE = {"id": "release", "provider": "teams", "name": "Release", "target": "channel", "about": "",
           "token_set": True, "webhook_host": "flows.example.invalid",
           "settings": {"title": "Release", "footer": "via QAVector", "aliases": {"Dana": "dana@team.example"},
                        "max_text": 20000, "access": {"agents": "approve", "cycles": "post"}}}
JIRA = {"id": "product", "provider": "jira", "name": "Product", "token_set": True, "settings": {}}
POSTS = [{"id": "p2", "at": 1794900000.0, "connection": "release", "title": "Release 1.2", "text": "- shipped",
          "mentions": [], "outcome": "accepted", "status": 202, "source": {"label": "This page"}},
         {"id": "p1", "at": 1794800000.0, "connection": "release", "title": "", "text": "Try", "mentions": [],
          "outcome": "failed", "status": 404, "error": "HTTP 404", "source": {"label": "Cycle Ready To Release"}}]


class Core:
    def __init__(self, post_answer=None):
        self.calls = []
        self.post_answer = post_answer or {"ok": True, "result": {"post": {"outcome": "accepted", "status": 202}}}

    def integrations(self, *args, document=None, cancel=None, timeout=120):
        self.calls.append((args, document))
        if args == ("list",):
            return {"ok": True, "connections": [JIRA, RELEASE]}
        if args[:1] == ("call",) and args[2] == "history":
            return {"ok": True, "result": {"items": POSTS}}
        if args[:1] == ("call",) and args[2] in ("post", "again", "test"):
            return self.post_answer
        if args == ("save",):
            return {"ok": True, "connection": dict(document["connection"], id="release")}
        return {"ok": False, "error": "unexpected"}


@pytest.fixture
def view(qapp, monkeypatch):
    monkeypatch.setattr(widgets, "confirm", lambda *a, **k: True)
    made = teams_page.TeamsView(None)
    made.core = Core()
    made.list_connections()
    yield made
    made.shutdown()
    made.close()


def test_only_teams_channels_and_the_webhook_by_its_host(view):
    assert [view.picker.itemText(i) for i in range(view.picker.count())] == ["Release"]
    table = view.connections_tab.table
    assert [table.item(0, c).text() for c in range(table.columnCount())] == [
        "Release", "Channel", "flows.example.invalid", "Ask", "Auto"]
    assert "flows.example.invalid" in view.connections_tab.webhook.placeholderText()
    assert view.connections_tab.webhook.text() == ""


def test_a_post_is_sent_only_with_known_names_and_after_asking(view):
    tab = view.post_tab
    assert not tab.send.isEnabled()
    tab.text.setPlainText("- shipped")
    tab.mentions.setText("Dana, nobody")
    assert not tab.send.isEnabled() and "nobody" in tab.message.text()
    tab.mentions.setText("Dana, sam@team.example")
    assert tab.send.isEnabled()
    html = tab.preview.toHtml()
    assert "Release" in html and "via QAVector" in html and "@Dana" in html
    tab.send.click()
    args, document = view.core.calls[-1]
    assert args == ("call", "release", "post")
    assert document == {"title": "", "text": "- shipped", "mentions": ["Dana", "sam@team.example"]}
    assert tab.text.toPlainText() == "" and "Posted to Release" in tab.message.text()


def test_one_post_a_minute_is_said_and_the_draft_kept(view):
    view.core.post_answer = {"ok": False, "kind": "refused", "retry_after": 42,
                             "error": "One post a minute to Release: the last one went 18 s ago. Try again in 42 s."}
    view.post_tab.text.setPlainText("again")
    view.post_tab.send.click()
    assert "Try again in 42 s" in view.post_tab.message.text()
    assert view.post_tab.text.toPlainText() == "again" and view.has_unsaved()


def test_history_is_read_on_its_tab_and_send_again_asks(view):
    view.tabs.setCurrentWidget(view.history)
    assert view.core.calls[-1] == (("call", "release", "history"), {"size": 200})
    table = view.history.table
    assert [table.item(r, 2).text() for r in range(table.rowCount())] == ["Accepted", "Failed"]
    table.selectRow(0)
    assert "Release 1.2" in view.history.shown.toHtml()
    view.history.again.click()
    assert (("call", "release", "again"), {"id": "p2"}) in view.core.calls


def test_a_channel_is_read_only_until_edit_and_saves_the_webhook_only_when_typed(view):
    tab = view.connections_tab
    assert not tab.name.isEnabled() and not tab.edit.isHidden() and tab.save.isHidden()
    tab.edit.click()
    assert tab.name.isEnabled() and not tab.save.isHidden()
    tab.agents.set_current("Auto")
    tab.save.click()
    args, document = view.core.calls[-2] if view.core.calls[-1][0] == ("list",) else view.core.calls[-1]
    assert args == ("save",) and "token" not in document
    assert document["connection"]["settings"]["access"] == {"agents": "post", "cycles": "post"}
    assert document["connection"]["settings"]["aliases"] == {"Dana": "dana@team.example"}
    assert "webhook_host" not in document["connection"] and "token_set" not in document["connection"]
