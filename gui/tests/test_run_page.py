"""The Run page's times: the whole run's, and each scenario's own."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QLabel

from cms_gui.pages.run import SessionPanel, format_duration


def test_durations_read_in_the_unit_that_fits():
    assert format_duration(3.24) == "3.2 s"
    assert format_duration(42.9) == "42 s"
    # The one on the screenshot: 78.3 s has to be worked out.
    assert format_duration(78.3) == "1 min 18 s"
    assert format_duration(3725) == "1 h 02 min"
    assert format_duration(None) == ""


def _session(runs, scenarios=()):
    return {"name": "s", "state": "running", "pid": None, "scenario": "",
            "done": 0, "total": 0, "runs": runs, "scenarios": list(scenarios),
            "server": [], "server_logs": []}


def _texts(panel):
    return [label.text() for label in panel.steps.findChildren(QLabel)]


def test_each_scenario_shows_how_long_it_took(qapp):
    panel = SessionPanel()
    runs = {"claim75_dashboard_backend": {
        "scenario": "DEMO-75 - backend tests", "status": "pass", "tree": {},
        "steps": {}, "total": 14, "done": 14, "started": 1000.0, "ended": 1072.0}}
    panel.update_from(_session(runs, ["claim75_dashboard_backend"]))
    texts = _texts(panel)
    assert "1 min 12 s" in texts
    assert any(t.startswith("DEMO-75 - backend tests") for t in texts)
    # Done under its name, and not listed a second time by id as still to come.
    assert not any("claim75_dashboard_backend" in t for t in texts)


def test_a_scenario_still_to_come_has_no_time_yet(qapp):
    panel = SessionPanel()
    panel.update_from(_session({}, ["next_one"]))
    assert "next_one" in _texts(panel)
    assert not any(t.endswith(" s") for t in _texts(panel))


def test_a_running_scenario_s_time_moves_on_between_events(qapp):
    # A backend test run can go minutes without an event, so the page's own
    # clock has to move the time on; nothing else would.
    panel = SessionPanel()
    runs = {"s1": {"scenario": "s1", "status": "running", "tree": {}, "steps": {},
                   "total": 2, "done": 0, "started": 1000.0, "ended": None}}
    panel.update_from(_session(runs))
    panel.tick(1005.0)
    assert "5.0 s" in _texts(panel)
    panel.tick(1090.0)
    assert "1 min 30 s" in _texts(panel)


# ------------------------------------------------------------------ search (Ctrl+K)
def test_search_lists_the_runs_sessions_in_their_panels_words(qapp, dispose):
    from cms_gui.pages.run import RunPage
    from cms_gui.runner import RunState

    state = RunState()
    page = RunPage(state)
    for name, login, now in (("shop-alice", "alice", "running"),
                             ("shop-bob", "bob", "failed")):
        session = state._session(name, login)
        session["state"] = now
        session["scenario"] = "qa_123_open_order"
    state.started = True
    page.refresh()
    hits = page.search_rows()
    assert [(h.title, h.status, h.tone, h.live) for h in hits] == [
        ("shop-alice", "RUNNING", "outline", True), ("shop-bob", "FAIL", "bad", True)]
    assert hits[0].detail == "alice · qa_123_open_order"
    assert page.show_found("shop-bob")
    assert not page.show_found("gone")
    state.exit_code = 0                       # the run is over: nothing is active
    assert not any(h.live for h in page.search_rows())
    dispose(page)
