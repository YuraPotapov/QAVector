"""The shared widgets that stand in for a stock Qt control.

Each one replaces something the design system could not restyle, so what matters
is that it still behaves like the control it replaced - and that the affordance
it was built for is actually there.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import icons, theme, widgets


# ------------------------------------------------------------------- stepper

def test_the_stepper_carries_the_spin_box_interface_the_pages_use(qapp):
    """It stands in for a QSpinBox on the Launch page, so it has to answer like one."""
    stepper = widgets.Stepper(1, 64)
    seen = []
    stepper.valueChanged.connect(seen.append)
    stepper.setValue(4)
    assert stepper.value() == 4
    assert seen == [4]
    stepper.setRange(1, 8)
    assert stepper.value() == 4


def test_the_ends_stop_at_the_range(qapp):
    stepper = widgets.Stepper(1, 3)
    assert not stepper.down.isEnabled()          # already at the minimum
    stepper.up.click()
    stepper.up.click()
    assert stepper.value() == 3
    assert not stepper.up.isEnabled()
    assert stepper.down.isEnabled()
    stepper.down.click()
    assert stepper.value() == 2
    assert stepper.up.isEnabled()


def test_the_stepper_disables_as_a_whole(qapp):
    """The Launch page greys it out while "All at once" is ticked."""
    stepper = widgets.Stepper(1, 64, 4)
    stepper.setEnabled(False)
    assert not stepper.spin.isEnabled()
    assert not stepper.up.isEnabled()
    stepper.setEnabled(True)
    assert stepper.spin.isEnabled()
    assert stepper.up.isEnabled()


def test_the_three_pieces_line_up(qapp):
    """A stepper whose buttons are a different height reads as three controls."""
    stepper = widgets.Stepper(1, 64)
    stepper.show()
    qapp.processEvents()
    assert stepper.down.height() == stepper.spin.height() == stepper.up.height()


# ---------------------------------------------------------------- disclosure

def test_a_folded_section_says_it_opens(qapp):
    """Regression: collapsed, Advanced was marked with a middle dot.

    A bullet is decoration - it gives no reason to click, which is the one thing
    a folded section has to do. The mark has to differ between the two states and
    has to be there in the closed one, where most people meet it.
    """
    section = widgets.Disclosure("Advanced")
    closed = section.button.icon().pixmap(16).toImage()
    section.set_expanded(True)
    opened = section.button.icon().pixmap(16).toImage()
    # The label never moves; the chevron is what turns, and it is an icon now
    # rather than a character the font may not have.
    assert section.button.text() == "Advanced"
    assert not closed.isNull() and closed != opened
    # ...and each state shows the chevron that belongs to it.
    assert closed == icons.pixmap("disclosure_closed", 16).toImage()
    assert opened == icons.pixmap("disclosure_open", 16).toImage()


def test_folding_hides_the_body(qapp):
    section = widgets.Disclosure("Advanced")
    section.body().addWidget(widgets.mono("inner"))
    section.show()
    qapp.processEvents()
    assert not section._body.isVisible()
    section.set_expanded(True)
    assert section._body.isVisible()


def test_a_control_on_the_title_line_does_not_narrow_the_body(qapp):
    """Regression: the Server log's box was 150px narrower than its card.

    The button beside it - "Separate Window" - shares the title's line and none
    of the body's, but it was put there by standing the whole section next to it
    in a hbox. A Disclosure is header AND body stacked inside one widget, so
    that arrangement takes the button's width off both.
    """
    from PySide6.QtWidgets import QPlainTextEdit, QPushButton

    section = widgets.Disclosure("Server log", expanded=True)
    inner = QPlainTextEdit()
    section.body().addWidget(inner)
    beside = section.add_to_header(QPushButton("Separate Window"))
    section.resize(600, 300)
    section.show()
    qapp.processEvents()

    assert beside.width() > 0, "the control was never laid out"
    assert inner.width() == section.width(), (
        "the body lost %dpx to a control on the header line"
        % (section.width() - inner.width()))
    # And the control is still where it was asked to be: the far end of the
    # title's line, not below it. Measured against the section, because the two
    # sit in different parents inside it.
    def in_section(widget, corner):
        return widget.mapTo(section, corner(widget.rect()))

    assert in_section(beside, lambda r: r.topRight()).x() >= section.width() - 1
    assert (in_section(beside, lambda r: r.bottomLeft()).y()
            <= in_section(inner, lambda r: r.topLeft()).y())


# --------------------------------------------------------------- file chooser
# A chooser does not list dotted directories, and every path this application
# asks for lives in one - a project's interpreter is .venv/bin/python, an ssh key
# is in ~/.ssh. Opening *inside* one does show its contents, because those are
# not themselves hidden, so where the chooser starts is the whole answer.

def test_a_chooser_opens_inside_the_dotted_directory_it_was_pointed_at(tmp_path):
    venv = tmp_path / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python3").write_text("")
    assert widgets.start_dir(str(venv / "python3")) == str(venv)


def test_a_directory_of_its_own_is_where_it_starts(tmp_path):
    assert widgets.start_dir(str(tmp_path)) == str(tmp_path)


def test_a_path_that_is_not_there_falls_back_rather_than_opening_nowhere(tmp_path):
    assert widgets.start_dir("/no/such/place/at/all", str(tmp_path)) == str(tmp_path)


def test_nothing_at_all_still_opens_somewhere_usable():
    assert widgets.start_dir("") == os.path.expanduser("~")


# ----------------------------------------------------------- empty tables
def _empty_table(qapp, rows=0):
    from PySide6.QtWidgets import QTableWidget, QTableWidgetItem

    table = QTableWidget(rows, 1)
    for row in range(rows):
        table.setItem(row, 0, QTableWidgetItem("x"))
    table.resize(200, 120)
    return table


def test_a_table_with_nothing_in_it_says_so(qapp):
    """Otherwise it is a header and a blank band, which reads as something that
    failed to load rather than as something not configured yet."""
    table = _empty_table(qapp)
    note = widgets.empty_note(table, "No services yet.")
    table.show()
    qapp.processEvents()
    assert note.isVisible()
    assert note.text() == "No services yet."


def test_a_table_with_rows_says_nothing(qapp):
    table = _empty_table(qapp, rows=2)
    note = widgets.empty_note(table, "No services yet.")
    table.show()
    qapp.processEvents()
    assert not note.isVisible()


def test_the_note_follows_the_table_rather_than_being_told(qapp):
    # Whoever fills the table has one less thing to remember, which is the only
    # way this stays true.
    from PySide6.QtWidgets import QTableWidgetItem

    table = _empty_table(qapp)
    note = widgets.empty_note(table, "Nothing yet.")
    table.show()
    qapp.processEvents()
    assert note.isVisible()

    table.insertRow(0)
    table.setItem(0, 0, QTableWidgetItem("something"))
    qapp.processEvents()
    assert not note.isVisible()

    table.removeRow(0)
    qapp.processEvents()
    assert note.isVisible()


def test_the_note_takes_its_colour_from_the_theme_not_from_a_literal(qapp):
    # A colour captured when it was built would go on painting the light palette
    # after dark mode was set.
    table = _empty_table(qapp)
    note = widgets.empty_note(table, "Nothing yet.")
    assert note.property("role") == "hint"
    assert not note.styleSheet()


# ------------------------------------------------------------------ nav badges
def _wide(slots=2, text="Services & Logs", room=140):
    """A nav button with ``room`` px to spare past its label, as the rail gives it."""
    from PySide6.QtCore import Qt

    button = widgets.NavButton(slots)
    button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
    button.setIcon(icons.icon("services"))
    button.setText(text)
    button.resize(button.sizeHint().width() + room, button.sizeHint().height())
    return button


def _painted(button, qapp):
    """Which badge colours the button drew, and the leftmost x of each."""
    qapp.processEvents()
    image = button.grab().toImage()
    seen = {}
    for x in range(image.width()):
        for y in range(image.height()):
            name = image.pixelColor(x, y).name().lower()
            if name in (theme.OK.lower(), theme.BAD.lower()):
                seen.setdefault(name, []).append(x)
    return {name: min(xs) for name, xs in seen.items()}


def test_a_rail_button_with_nothing_to_report_paints_nothing(qapp):
    button = _wide()
    button.set_badges([(0, "ok"), (0, "bad")])
    assert _painted(button, qapp) == {}


def test_the_counts_are_painted_in_their_own_colours(qapp):
    button = _wide()
    button.set_badges([(3, "ok"), (1, "bad")])
    painted = _painted(button, qapp)
    assert theme.OK.lower() in painted and theme.BAD.lower() in painted
    # Green first, red after it: the order they are given is the order they read.
    assert painted[theme.OK.lower()] < painted[theme.BAD.lower()]


def test_a_failure_holds_its_place_whether_or_not_anything_is_running(qapp):
    """Laid out from the right, so the last one given never moves.

    A red that slid across the moment the last service stopped would be a colour
    read twice - once for what it is, once for where it now is.
    """
    both = _wide()
    both.set_badges([(3, "ok"), (1, "bad")])
    beside_green = _painted(both, qapp)[theme.BAD.lower()]

    alone = _wide()
    alone.set_badges([(0, "ok"), (1, "bad")])
    assert _painted(alone, qapp)[theme.BAD.lower()] == beside_green


def test_a_count_never_changes_the_width_of_the_rail(qapp):
    """The rail measures itself from its labels, expanded and collapsed both.

    Nothing is held open for a badge and nothing grows to fit one, so a count
    arriving mid-run cannot move the rail out from under what is being read.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QToolButton

    plain = QToolButton()
    plain.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
    plain.setIcon(icons.icon("services"))
    plain.setText("Services & Logs")
    button = _wide()
    for compact in (False, True):
        # The rail switches both together: marks only, and the counts with them.
        style = Qt.ToolButtonIconOnly if compact else Qt.ToolButtonTextBesideIcon
        button.set_compact(compact)
        button.setToolButtonStyle(style)
        plain.setToolButtonStyle(style)
        for pairs in ([(0, "ok"), (0, "bad")], [(1, "ok"), (0, "bad")],
                      [(99, "ok"), (99, "bad")]):
            button.set_badges(pairs)
            assert button.sizeHint() == plain.sizeHint(), (compact, pairs)


def test_a_count_with_room_for_it_is_drawn_on_its_point(qapp):
    """The figure goes on the point, not beside it: beside it is width."""
    button = _wide(slots=1, text="Run", room=140)
    button.set_badges([(7, "ok")])
    qapp.processEvents()
    image = button.grab().toImage()
    fill = [(x, y) for x in range(image.width()) for y in range(image.height())
            if image.pixelColor(x, y).name().lower() == theme.OK.lower()]
    figure = [(x, y) for x, y in
              [(x, y) for x in range(image.width()) for y in range(image.height())]
              if image.pixelColor(x, y).name().lower() == widgets.BADGE_FIGURE]
    assert fill, "no point was drawn"
    # The figure is inside the point, so its ink is bounded by the fill's box.
    inside = [p for p in figure
              if min(x for x, _y in fill) < p[0] < max(x for x, _y in fill)
              and min(y for _x, y in fill) < p[1] < max(y for _x, y in fill)]
    assert inside, "the count was not drawn on the point"


def test_a_label_that_leaves_no_room_says_it_in_colour_alone(qapp):
    """Rather than widen the rail, which is the one thing it must not do.

    The numbers are still there - on the tooltip, which carries them whichever
    form the entry ends up drawing.
    """
    cramped = _wide(room=14)
    cramped.set_badges([(3, "ok"), (1, "bad")], "3 running · 1 failed")
    image = cramped.grab().toImage()
    assert not any(image.pixelColor(x, y).name() == widgets.BADGE_FIGURE
                   for x in range(image.width()) for y in range(image.height()))
    # Both counts are still said, in colour and on the tooltip.
    assert set(_painted(cramped, qapp)) == {theme.OK.lower(), theme.BAD.lower()}
    assert cramped.summary() == "3 running · 1 failed"


def test_collapsed_the_counts_are_points_on_the_mark_and_nothing_else(qapp):
    from PySide6.QtCore import Qt

    button = widgets.NavButton(2)
    button.setToolButtonStyle(Qt.ToolButtonIconOnly)
    button.setIcon(icons.icon("services"))
    button.set_compact(True)
    button.set_badges([(3, "ok"), (1, "bad")], "3 running · 1 failed")
    button.resize(button.sizeHint())
    qapp.processEvents()
    image = button.grab().toImage()
    seen = {}
    for x in range(image.width()):
        for y in range(image.height()):
            name = image.pixelColor(x, y).name().lower()
            if name in (theme.OK.lower(), theme.BAD.lower()):
                seen.setdefault(name, []).append(y)
    # Both points are there, stacked rather than in a row: there is no room
    # beside the mark, which is why the numbers went to the tooltip.
    assert set(seen) == {theme.OK.lower(), theme.BAD.lower()}
    assert max(seen[theme.OK.lower()]) < min(seen[theme.BAD.lower()])
    assert button.summary() == "3 running · 1 failed"


def test_a_summary_says_nothing_when_there_is_nothing_to_say(qapp):
    button = widgets.NavButton(2)
    button.set_badges([(0, "ok"), (0, "bad")], "0 running")
    assert button.summary() == ""


def test_two_counts_fit_what_the_longest_label_leaves_spare(qapp):
    """The bubbles are sized for the entry that carries two of them.

    The rail is measured from its names and adds nothing for a tally, so what
    the longest label leaves past itself is the whole budget - and a bubble that
    reads well at some comfortable size but does not fit here is one nobody
    sees: it falls back to a bare point. "Services & Logs" is that entry, and it
    is also very nearly the widest label in the rail.
    """
    from PySide6.QtGui import QFontMetrics

    metrics = QFontMetrics(theme.mono_font(8))
    one = max(widgets.BADGE_HEIGHT,
              metrics.horizontalAdvance("9") + 2 * widgets.BADGE_PAD)
    pair = one * 2 + widgets.BADGE_GAP + widgets.BADGE_RIGHT
    # RAIL_SLACK past the widest label, plus the part of the entry's own right
    # padding that a label never draws in.
    assert pair <= 12 + widgets.BADGE_ENCROACH + 15


# ---------------------------------------------------------------- check list

def _scenarios(ticked=()):
    checks = widgets.CheckList(selected_view=True)
    checks.set_noun("scenarios")
    for value in ("alpha", "beta", "gamma", "tag:smoke"):
        checks.add(value, value)
    checks.set_checked(ticked)
    return checks


def _visible(checks):
    return [checks.list.item(i).data(0x0100) for i in range(checks.list.count())
            if not checks.list.item(i).isHidden()]        # 0x0100: Qt.UserRole


def test_the_selected_view_narrows_the_list_to_what_is_ticked(qapp):
    """What is ticked, in the list's own place - however long the selection."""
    checks = _scenarios(["beta", "tag:smoke"])
    assert checks.view._buttons["All"].text() == "All 4"
    assert checks.view._buttons["Selected"].text() == "Selected 2"
    assert _visible(checks) == ["alpha", "beta", "gamma", "tag:smoke"]
    checks.view.set_current("Selected")
    assert _visible(checks) == ["beta", "tag:smoke"]
    checks.view.set_current("All")
    assert _visible(checks) == ["alpha", "beta", "gamma", "tag:smoke"]


def test_unticking_under_selected_takes_the_row_away_at_once(qapp):
    from PySide6.QtCore import Qt

    checks = _scenarios(["alpha", "beta"])
    seen = []
    checks.changed.connect(lambda: seen.append(True))
    checks.view.set_current("Selected")
    checks.list.item(0).setCheckState(Qt.Unchecked)            # alpha
    assert _visible(checks) == ["beta"]
    assert checks.view._buttons["Selected"].text() == "Selected 1"
    assert seen, "an untick is still a change to the selection"


def test_the_search_and_the_view_narrow_together(qapp):
    checks = _scenarios(["alpha", "beta", "tag:smoke"])
    checks.view.set_current("Selected")
    checks.search.setText("smoke")
    assert _visible(checks) == ["tag:smoke"]
    # Only what the search hides is counted as hidden by it.
    assert checks.count.text() == "3 of 4 scenarios   ·   3 hidden by the search"
    checks.search.setText("")
    assert _visible(checks) == ["alpha", "beta", "tag:smoke"]
    assert checks.count.text() == "3 of 4 scenarios"


def test_clear_all_unticks_everything_and_then_has_nothing_to_do(qapp):
    checks = _scenarios(["alpha", "gamma"])
    seen = []
    checks.changed.connect(lambda: seen.append(True))
    assert checks.clear_button.isEnabled()
    checks.clear_button.click()
    assert checks.checked() == []
    assert seen
    assert not checks.clear_button.isEnabled()


def test_an_empty_selected_view_says_why_it_is_empty(qapp):
    checks = _scenarios()
    checks.view.set_current("Selected")
    assert _visible(checks) == []
    assert not checks._empty.isHidden()
    assert "Nothing selected yet" in checks._empty.text()
    checks.set_checked(["gamma"])
    assert checks._empty.isHidden()


def test_a_list_without_the_view_behaves_as_it_always_did(qapp):
    checks = widgets.CheckList()
    assert checks.view is None and checks.clear_button is None
    checks.add("a")
    checks.add("b")
    checks.set_checked(["a"])
    assert _visible(checks) == ["a", "b"]           # ticked or not, all shown
    checks.search.setText("b")
    assert _visible(checks) == ["b"]
    assert checks.count.text() == "1 of 2 selected   ·   1 hidden by the search"
