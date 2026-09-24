"""The stage column: what it draws, and what it does when there is nothing."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QLineEdit                           # noqa: E402,F401

from cms_gui import theme                                         # noqa: E402
from cms_gui.stages import StageList, StageRow, colour_of         # noqa: E402


def stage(kind="tool", title="Read main.py", detail="", status="done"):
    return {"kind": kind, "title": title, "detail": detail, "status": status}


@pytest.fixture
def column(qapp, dispose):
    made = StageList()
    yield made
    dispose(made)


@pytest.fixture
def row(qapp, dispose):
    """Builds rows and takes them down at the end - ``dispose`` deletes the C++
    object at once, so a row disposed before its assertions has no text left."""
    made = []

    def build(*args, **kwargs):
        one = StageRow(*args, **kwargs)
        made.append(one)
        return one

    yield build
    for one in made:
        dispose(one)


def test_every_stage_becomes_a_row_in_the_order_it_happened(column):
    column.set_stages([stage(title="Thinking"), stage(title="Read main.py"),
                       stage(title="Answered")])
    assert column.titles() == ["Thinking", "Read main.py", "Answered"]


def test_showing_a_new_set_replaces_the_old_one_rather_than_adding_to_it(column):
    column.set_stages([stage(title="one"), stage(title="two")])
    column.set_stages([stage(title="three")])
    assert column.titles() == ["three"]
    assert column.count() == 1


# -------------------------------------------------------------- appending
# The column used to clear itself and build every row again on each change.
# The page redraws on every event of a run, so drawing n stages cost the sum of
# 1..n widget trees and a long cycle stopped answering. These are about the
# cheap path: the same rows, plus whatever is new at the end.
def test_a_stage_arriving_leaves_the_rows_already_drawn_alone(column):
    model = [stage(title="one"), stage(title="two")]
    column.set_stages(list(model))
    before = list(column._rows)

    model.append(stage(title="three"))
    column.set_stages(list(model))
    assert column.titles() == ["one", "two", "three"]
    assert column._rows[:2] == before          # the same widgets, not copies


def test_the_row_that_was_last_stops_being_drawn_as_last(column):
    """The line joining one dot to the next is painted from that flag, so a row
    that keeps it has a sequence that visibly stops in the middle."""
    model = [stage(title="one")]
    column.set_stages(list(model))
    assert column._rows[0].marker._last

    model.append(stage(title="two"))
    column.set_stages(list(model))
    assert not column._rows[0].marker._last
    assert column._rows[-1].marker._last


def test_stages_trimmed_off_the_front_take_their_rows_with_them(column):
    """The model keeps the last CYCLE_STAGES and drops the oldest, so what is
    drawn slides down it rather than only growing."""
    model = [stage(title="one"), stage(title="two"), stage(title="three")]
    column.set_stages(list(model))
    kept = column._rows[-1]

    model = model[2:] + [stage(title="four")]
    column.set_stages(list(model))
    assert column.titles() == ["three", "four"]
    assert column._rows[0] is kept             # not rebuilt, just re-headed
    assert column._rows[0].marker._first


def test_a_different_list_is_rebuilt_rather_than_appended_to(column):
    column.set_stages([stage(title="one"), stage(title="two")])
    column.set_stages([stage(title="elsewhere")])
    assert column.titles() == ["elsewhere"]
    assert column.count() == 1


def test_changing_how_rows_are_named_rebuilds_them(column):
    """The step's name is built into a row, so it cannot be appended around."""
    model = [("admit", stage(title="one"))]
    column.set_stages(list(model), named=True, labels={"admit": "Admit"})
    assert "Admit" in column._rows[0].title.text()

    column.set_stages(list(model), named=True, labels={"admit": "Renamed"})
    assert "Renamed" in column._rows[0].title.text()


def test_nothing_to_show_says_so_rather_than_leaving_a_blank_panel(column):
    column.set_stages([])
    assert column.count() == 0
    assert column._empty.isVisible() or not column.isVisible()
    assert "stage" in column._empty.text().lower()


def test_a_row_carries_its_body_when_it_has_one(row):
    one = row(stage(detail="file_path: /repo/main.py"))
    assert one.detail is not None
    assert "/repo/main.py" in one.detail.text()


def test_a_row_without_a_body_has_no_empty_label_under_it(row):
    assert row(stage(detail="   ")).detail is None


def test_a_long_body_is_cut_and_says_how_much_was_left_out(row):
    one = row(stage(detail="\n".join("line %d" % n for n in range(100))))
    assert "more lines" in one.detail.text()
    assert len(one.detail.text().splitlines()) < 100


def test_the_step_id_goes_in_front_only_when_the_rows_are_from_several_steps(row):
    """One step's stages need no label; a whole run's would be unreadable."""
    assert "review" in row(stage(title="Thinking"), step_id="review").title.text()
    assert row(stage(title="Thinking")).title.text() == "Thinking"


def test_a_run_wide_list_labels_its_rows(column):
    column.set_stages([("review", stage(title="Thinking")),
                       ("report", stage(title="Answered"))], named=True)
    assert column.titles() == ["Thinking", "Answered"]
    assert column.count() == 2


# ----------------------------------------------------------------- following
def _many(count):
    return [stage(title="step %d" % index, detail="line one\nline two")
            for index in range(count)]


def _pump(qapp):
    """Let the layout finish.

    Twice, not once, and that is the whole reason this code exists: the body
    is still at its old height after the first pass, so the scroll range is
    still zero and anything that scrolled then would land at the top.
    """
    qapp.processEvents()
    qapp.processEvents()


def _shown(qapp, widget):
    widget.resize(700, 300)
    widget.show()
    _pump(qapp)


def test_new_rows_scroll_the_view_to_the_newest(qapp, column):
    """The range grows one layout pass after the rows go in, so scrolling
    straight after inserting them landed on the old maximum - which is to say
    it stayed at the top."""
    _shown(qapp, column)
    column.set_stages(_many(17))
    _pump(qapp)

    bar = column.verticalScrollBar()
    assert bar.maximum() > 0, "the rows have to overflow for this to mean anything"
    assert bar.value() == bar.maximum()


def test_scrolling_up_stops_the_view_following(qapp, column):
    """A run emits stages for half a minute. Being yanked back down every time
    one arrives makes it unreadable exactly when somebody is reading it."""
    _shown(qapp, column)
    column.set_stages(_many(17))
    _pump(qapp)
    column.verticalScrollBar().setValue(0)
    _pump(qapp)
    assert not column.at_bottom()

    column.set_stages(_many(25))
    _pump(qapp)
    assert column.verticalScrollBar().value() == 0


def test_scrolling_back_down_starts_it_following_again(qapp, column):
    _shown(qapp, column)
    column.set_stages(_many(17))
    _pump(qapp)
    bar = column.verticalScrollBar()
    bar.setValue(0)
    _pump(qapp)
    bar.setValue(bar.maximum())
    _pump(qapp)
    assert column.at_bottom()

    column.set_stages(_many(40))
    _pump(qapp)
    assert bar.value() == bar.maximum()


def test_a_view_with_nothing_to_scroll_is_still_following(qapp, column):
    _shown(qapp, column)
    column.set_stages(_many(1))
    _pump(qapp)
    assert column.at_bottom()


# ------------------------------------------------------------------- reviews
REVIEW = {"summary": "Prints the line and exits.", "risk": "medium",
          "issues": [{"severity": "high", "description": "MD5 without a salt",
                      "file": "login.py", "line": 42},
                     {"severity": "low", "description": "Unused import"}],
          "recommendations": ["Use a slow KDF", "Drop the import"]}


def _review_stage(**changed):
    return {"kind": "review", "title": "Review - medium risk, 2 issues",
            "detail": REVIEW["summary"], "status": "done",
            "body": dict(REVIEW, **changed)}


def _labels(widget):
    """Every label under a row, for a test that wants how they are drawn."""
    from PySide6.QtWidgets import QLabel
    return widget.findChildren(QLabel)


def _texts(widget):
    """Every label under a row, so a test can read what it says."""
    return [one.text() for one in _labels(widget)]


def test_a_review_is_laid_out_rather_than_printed_as_json(row):
    said = " ".join(_texts(row(_review_stage())))
    assert "{" not in said and '"summary"' not in said
    assert REVIEW["summary"] in said


def test_a_review_names_what_it_found_and_where(row):
    said = " ".join(_texts(row(_review_stage())))
    assert "login.py:42" in said
    assert "MD5 without a salt" in said


def test_an_issue_with_no_file_still_says_what_it_is(row):
    said = " ".join(_texts(row(_review_stage())))
    assert "Unused import" in said


def test_a_review_lists_what_to_do_about_it(row):
    said = " ".join(_texts(row(_review_stage())))
    assert "Use a slow KDF" in said


def test_a_clean_review_shows_no_empty_headings(row):
    said = " ".join(_texts(row(_review_stage(issues=[], recommendations=[]))))
    assert "ISSUES" not in said
    assert "RECOMMENDATIONS" not in said
    assert REVIEW["summary"] in said


def test_a_review_s_marker_is_coloured_by_its_risk():
    """A high-risk review should be findable without reading a word of it."""
    assert colour_of(_review_stage(risk="high")) == theme.BAD
    assert colour_of(_review_stage(risk="medium")) == theme.WARN
    assert colour_of(_review_stage(risk="low")) == theme.OK
    assert colour_of(_review_stage(risk="unknown")) == theme.NEUTRAL[500]


def test_a_review_row_with_no_body_does_not_fall_over(row):
    """An older core sending the kind without the fields is a plainer row."""
    one = row({"kind": "review", "title": "Review", "detail": "said this",
               "status": "done"})
    assert "said this" in " ".join(_texts(one))


# --------------------------------------------------------------------- gates
def _gate_stage(**changed):
    """What ``check.gate`` sends: every check, and which way each one went."""
    one = {"kind": "gate", "title": "Gate refused: within its budget",
           "status": "failed",
           "detail": ("there is a task: 1 > 0\n"
                      "within its budget: 5 <= 3"),
           "body": {"checks": [
               {"name": "there is a task", "expression": "1 > 0",
                "account": "1 > 0", "held": True},
               {"name": "within its budget", "expression": "5 <= 3",
                "account": "5 <= 3", "held": False}]}}
    one.update(changed)
    return one


def test_a_gate_shows_every_check_on_its_own_line(row):
    said = _texts(row(_gate_stage()))
    assert "there is a task" in said
    assert "within its budget" in said
    assert "5 <= 3" in said


def test_a_gate_says_which_check_refused_without_reading_the_heading(row):
    """The whole point of the row: the joined text could only say what was
    compared, so the one that refused had to be found by eye."""
    one = row(_gate_stage())
    colours = {label.text(): label.styleSheet() for label in _labels(one)}
    assert theme.BAD in colours["within its budget"]
    assert theme.OK in colours["there is a task"]


def test_a_gate_that_held_is_drawn_as_passing(row):
    assert colour_of(_gate_stage(status="done")) == theme.OK
    assert colour_of(_gate_stage()) == theme.BAD


def test_a_gate_row_with_no_checks_falls_back_to_what_it_said(row):
    """An older core sends the kind without the rows, and a blank row would be
    worse than the paragraph it used to draw."""
    one = row(_gate_stage(body={}))
    assert "within its budget: 5 <= 3" in " ".join(_texts(one))


def test_a_row_is_named_by_what_the_step_is_called(column, qapp):
    """"Is this ours to do" says which step this is; "admit" says what its id
    happens to be spelt like."""
    column.set_stages([("admit", _gate_stage())], named=True,
                      labels={"admit": "Is this ours to do"})
    assert "Is this ours to do" in column._rows[0].title.text()

    column.set_stages([("admit", _gate_stage())], named=True)
    assert "admit" in column._rows[0].title.text()


# ------------------------------------------------------------------- colour
def test_a_failed_stage_is_drawn_as_one_whatever_kind_it_is():
    assert colour_of(stage(kind="tool_result", status="failed")) == theme.BAD
    assert colour_of(stage(kind="thinking", status="failed")) == theme.BAD


def test_a_stage_still_running_is_drawn_in_the_accent():
    assert colour_of(stage(status="running")) == theme.ACCENT


def test_an_unknown_kind_still_has_a_colour_rather_than_raising():
    assert colour_of(stage(kind="something_new")) == theme.NEUTRAL[500]


def test_colour_is_read_at_call_time_not_captured_at_import():
    """theme.set_dark_mode rewrites those globals in place."""
    was = theme.BAD
    try:
        theme.BAD = "#123456"
        assert colour_of(stage(status="failed")) == "#123456"
    finally:
        theme.BAD = was
