"""One session's backend log: the folded strip, and the window it opens into.

The strip on the Run page is for glancing while a run goes past. Reading a log is
a different activity - width, height, a search - so these cases are mostly about
the window, and about the two staying in step while lines keep arriving.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui.pages.run import SessionPanel
from cms_gui.pages.serverlogwindow import ALL_LOGS, ServerLogWindow


def _line(text, level="INFO", log="app"):
    return {"log": log, "ts": 1.0, "level": level, "text": text}


def _session(*lines, **kwargs):
    logs = kwargs.get("logs") or ["app"]
    return {"name": kwargs.get("name", "dev-agent"), "state": "launched",
            "pid": 7, "scenario": "", "done": 0, "total": 0, "runs": {},
            "server": list(lines), "server_logs": logs}


@pytest.fixture
def panel(qapp):
    widget = SessionPanel()
    widget.update_from(_session(_line("one"), _line("two", "ERROR")))
    yield widget
    if widget._server_window is not None:
        widget._server_window.close()
    widget.close()


@pytest.fixture
def window(qapp):
    widget = ServerLogWindow("dev-agent")
    yield widget
    widget.close()


# ---------------------------------------------------------------- the button
def test_the_panel_offers_a_way_out_of_the_folded_strip(panel):
    # The reason to reach for a bigger view is usually that the strip is too small
    # to read in, which is a poor place to hide the way out of it.
    assert panel._server_popout.text() == "Separate Window"
    assert not panel._server_popout.isHidden()


def test_opening_hands_the_window_what_the_session_has(panel):
    window = panel.open_server_window()
    try:
        assert [line["text"] for line in window.visible_lines()] == ["one", "two"]
        assert window.windowTitle().endswith("dev-agent")
    finally:
        window.close()


def test_clicking_twice_brings_back_the_same_window(panel):
    first = panel.open_server_window()
    try:
        # Not a second copy of what you were already reading.
        assert panel.open_server_window() is first
    finally:
        first.close()


def test_the_window_keeps_up_as_the_run_goes_on(panel):
    window = panel.open_server_window()
    try:
        panel.update_from(_session(_line("one"), _line("two", "ERROR"),
                                   _line("three")))
        assert [line["text"] for line in window.visible_lines()] == \
            ["one", "two", "three"]
    finally:
        window.close()


def test_a_run_with_no_server_log_shows_none_of_this(qapp):
    widget = SessionPanel()
    widget.update_from(_session())          # no lines at all
    assert not widget.server.isVisibleTo(widget)
    widget.close()


# ---------------------------------------------------------------- filtering
def test_a_level_means_that_one_and_worse(window):
    window.update_from(_session(_line("chatter"), _line("careful", "WARN"),
                                _line("boom", "ERROR")))
    window.level.set_current("WARN")
    assert [line["text"] for line in window.visible_lines()] == ["careful", "boom"]
    window.level.set_current("ERROR")
    assert [line["text"] for line in window.visible_lines()] == ["boom"]
    window.level.set_current("ALL")
    assert len(window.visible_lines()) == 3


def test_search_is_case_insensitive_and_matches_anywhere(window):
    window.update_from(_session(_line("Traceback (most recent call last):"),
                                _line("ValueError: boom"), _line("all fine")))
    window.search.setText("boom")
    assert [line["text"] for line in window.visible_lines()] == ["ValueError: boom"]
    window.search.setText("TRACEBACK")
    assert len(window.visible_lines()) == 1


def test_the_filters_combine_rather_than_replace_each_other(window):
    window.update_from(_session(_line("boom here", "ERROR"),
                                _line("boom there", "INFO")))
    window.level.set_current("ERROR")
    window.search.setText("boom")
    assert [line["text"] for line in window.visible_lines()] == ["boom here"]


def test_one_log_can_be_singled_out_of_several(window):
    window.update_from(_session(_line("from the app", log="app"),
                                _line("from nginx", log="nginx"),
                                logs=["app", "nginx"]))
    assert window.log.isVisibleTo(window)          # offered only when there are two
    window.log.setCurrentText("nginx")
    assert [line["text"] for line in window.visible_lines()] == ["from nginx"]
    window.log.setCurrentText(ALL_LOGS)
    assert len(window.visible_lines()) == 2


def test_the_log_picker_stays_out_of_the_way_when_there_is_one(window):
    window.update_from(_session(_line("only one"), logs=["app"]))
    assert not window.log.isVisibleTo(window)


def test_the_count_says_how_much_the_filters_are_hiding(window):
    window.update_from(_session(_line("a"), _line("b", "ERROR"), _line("c")))
    window.level.set_current("ERROR")
    assert window.count.text() == "1 of 3 lines"


def test_a_filter_that_matches_nothing_says_so_rather_than_looking_broken(window):
    window.update_from(_session(_line("a"), _line("b")))
    window.search.setText("nothing like this")
    assert window.visible_lines() == []
    assert window.count.text() == "0 of 2 lines"


# ------------------------------------------------------------------- saving
def test_saving_writes_what_is_on_screen_not_everything(window, tmp_path,
                                                        monkeypatch):
    window.update_from(_session(_line("keep me", "ERROR"), _line("hide me")))
    window.level.set_current("ERROR")
    target = tmp_path / "out.log"
    monkeypatch.setattr("cms_gui.pages.serverlogwindow.QFileDialog.getSaveFileName",
                        lambda *a, **k: (str(target), ""))
    window._save()
    assert target.read_text() == "[app] keep me"


def test_a_save_that_cannot_be_written_is_reported_not_raised(window, monkeypatch):
    window.update_from(_session(_line("x")))
    monkeypatch.setattr("cms_gui.pages.serverlogwindow.QFileDialog.getSaveFileName",
                        lambda *a, **k: ("/proc/nope/out.log", ""))
    shown = {}
    monkeypatch.setattr("cms_gui.widgets.warn",
                        lambda *a, **k: shown.setdefault("said", a[-1]))
    window._save()
    assert shown                                   # and no exception escaped


def test_cancelling_the_save_dialog_writes_nothing(window, monkeypatch):
    window.update_from(_session(_line("x")))
    monkeypatch.setattr("cms_gui.pages.serverlogwindow.QFileDialog.getSaveFileName",
                        lambda *a, **k: ("", ""))
    window._save()                                 # must simply return


# -------------------------------------------------- the panel while detached
def test_the_strip_stops_showing_the_lines_once_the_window_has_them(panel):
    window = panel.open_server_window()
    try:
        # Not both: two copies of the same lines, one of them where nobody is
        # looking, is exactly the rendering this avoids.
        # isHidden, not isVisibleTo: the section is inside a Disclosure that is
        # folded by default, so "visible" would be False either way and the
        # assertion would pass without meaning anything.
        assert panel._server_view.isHidden()
        assert panel._server_controls.isHidden()
        assert not panel._server_elsewhere.isHidden()
    finally:
        window.close()


def test_the_strip_says_where_the_lines_went_rather_than_going_blank(panel):
    window = panel.open_server_window()
    try:
        labels = [child.text() for child in panel._server_elsewhere.findChildren(
            type(panel.name)) if child.text()]
        assert any("separate window" in text.lower() for text in labels)
    finally:
        window.close()


def test_the_pulse_runs_only_while_it_is_on_screen(panel):
    from PySide6.QtCore import QAbstractAnimation

    dot = panel._server_elsewhere._dot
    assert dot._animation.state() != QAbstractAnimation.Running
    window = panel.open_server_window()
    try:
        assert dot._animation.state() == QAbstractAnimation.Running
    finally:
        window.close()
    # An animation nobody can see is a timer waking the process for nothing.
    assert dot._animation.state() != QAbstractAnimation.Running


def test_closing_the_window_brings_the_lines_back_up_to_date(panel):
    window = panel.open_server_window()
    panel.update_from(_session(_line("one"), _line("two", "ERROR"), _line("late")))
    window.close()
    assert not panel._server_view.isHidden()
    assert panel._server_elsewhere.isHidden()
    # Lines that arrived while the window had them are not missing from the strip.
    assert "late" in panel._server_view.toPlainText()


def test_the_pop_out_button_goes_away_while_the_window_is_open(panel):
    window = panel.open_server_window()
    try:
        # There is nothing to pop out - the indicator's own button raises it.
        assert panel._server_popout.isHidden()
    finally:
        window.close()
    assert not panel._server_popout.isHidden()


def test_the_indicator_can_raise_the_window_again(panel):
    window = panel.open_server_window()
    try:
        panel._server_elsewhere.show_requested.emit()
        assert panel._server_window is window       # raised, not replaced
    finally:
        window.close()


# ------------------------------------------------------------------- colours
def test_every_severity_gets_its_own_colour(qapp):
    from cms_gui import theme
    from cms_gui.pages.serverlogwindow import level_html

    seen = {}
    for level in ("DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"):
        seen[level] = level_html(level, "x")
        assert theme.LOG_LEVEL[level] in seen[level]
    # Five levels, five distinct renderings - a log is read by scanning, and two
    # severities that look alike are two severities nobody can tell apart.
    assert len(set(seen.values())) == 5


def test_critical_escalates_the_error_colour_rather_than_replacing_it(qapp):
    from cms_gui import theme
    from cms_gui.pages.serverlogwindow import level_html

    critical = level_html("CRITICAL", "the process is going down")
    # Weight and a wash: a critical is not a different KIND of thing from an
    # error, it is the same thing gone further.
    assert "font-weight" in critical
    assert theme.LOG_CRITICAL_BG in critical


def test_a_line_is_escaped_before_it_is_coloured(qapp):
    from cms_gui.pages.serverlogwindow import level_html

    # Log lines carry angle brackets all the time (<class 'ValueError'>), and a
    # pane that renders HTML would swallow them.
    assert "&lt;script&gt;" in level_html("INFO", "<script>")


def test_the_colours_follow_the_theme_rather_than_the_import(qapp):
    """The palette is swapped in place when dark mode is toggled.

    A module-level map built at import would keep painting a log in the colours
    of whichever theme happened to be on when the page was first loaded.
    """
    from cms_gui import theme
    from cms_gui.pages.serverlogwindow import level_html

    try:
        theme.set_dark_mode(True)
        dark_colour = theme.LOG_LEVEL["INFO"]
        dark = level_html("INFO", "x")
        theme.set_dark_mode(False)
        light_colour = theme.LOG_LEVEL["INFO"]
        light = level_html("INFO", "x")
    finally:
        theme.set_dark_mode(False)
    assert dark_colour != light_colour          # the palette really does swap
    assert dark_colour in dark and light_colour in light


def test_the_filter_offers_a_threshold_not_a_single_level(qapp, window):
    from cms_gui.pages.serverlogwindow import LEVELS

    window.update_from(_session(_line("chatter", "DEBUG"), _line("normal"),
                                _line("careful", "WARN"), _line("boom", "ERROR"),
                                _line("gone", "CRITICAL")))
    # DEBUG is the floor, so it is not offered: picking it would mean ALL, while
    # picking INFO is the useful thing - hide the chatter.
    assert LEVELS == ["ALL", "INFO", "WARN", "ERROR", "CRITICAL"]
    window.level.set_current("INFO")
    assert [line["text"] for line in window.visible_lines()] == \
        ["normal", "careful", "boom", "gone"]
    window.level.set_current("CRITICAL")
    assert [line["text"] for line in window.visible_lines()] == ["gone"]


def test_debug_lines_are_there_but_can_be_filtered_out(qapp, window):
    window.update_from(_session(_line("noise", "DEBUG"), _line("signal")))
    assert len(window.visible_lines()) == 2          # ALL keeps them
    window.level.set_current("INFO")
    assert [line["text"] for line in window.visible_lines()] == ["signal"]
