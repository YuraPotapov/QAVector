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


# ------------------------------------------------------- asking in our clothes
# QMessageBox is not styled by the stylesheet - it draws its own icon and asks
# the platform for its buttons, so it arrives as every other application on the
# machine and none of this one.
def test_a_confirmation_is_built_from_the_same_widgets_as_everything_else(qapp):
    dialog = widgets.Message(None, "Run", "Run all 23 steps?", "It may hurt.",
                             agree="Yes", refuse="Cancel")
    try:
        assert dialog.agree_button.text() == "Yes"
        assert dialog.refuse_button.text() == "Cancel"
        assert dialog.agree_button.property("variant") == "primary", \
            "the one they came to press"
        assert dialog.refuse_button.property("variant") == "ghost"
    finally:
        dialog.deleteLater()


def test_a_notice_has_one_button_because_there_is_nothing_to_decide(qapp):
    dialog = widgets.Message(None, "Projects", "Could not read the file.")
    try:
        assert dialog.refuse_button is None
        assert dialog.agree_button.text() == "OK"
    finally:
        dialog.deleteLater()


def test_a_warning_is_ruled_and_a_plain_notice_is_not(qapp):
    """Most of what this shows is ordinary, so the mark is for the part that
    is not - and it is a rule rather than a picture, because a stylesheet can
    colour a rule and cannot draw a picture."""
    warned = widgets.Message(None, "Run", "It failed.", kind="error")
    plain = widgets.Message(None, "Run", "It finished.")
    try:
        assert widgets.Message.RULES["error"] == "BAD"
        assert _rules(warned) == 1
        assert _rules(plain) == 0
    finally:
        warned.deleteLater()
        plain.deleteLater()


def _rules(dialog):
    from PySide6.QtWidgets import QFrame

    return len([one for one in dialog.findChildren(QFrame)
                if one.width() == widgets.Message.RULE
                or one.minimumWidth() == widgets.Message.RULE])


def test_it_shows_the_question_and_the_consequence_apart(qapp):
    """Two labels, not one paragraph: the question is answered in a second,
    the consequence is what somebody reads if they hesitate."""
    dialog = widgets.Message(None, "Run", "Run all 23 steps?",
                             "This can change files on your machine.")
    try:
        assert dialog.message.text() == "Run all 23 steps?"
        assert dialog.detail.text() == "This can change files on your machine."
    finally:
        dialog.deleteLater()


def test_a_confirmation_with_nothing_to_warn_about_shows_no_note(qapp):
    """Counted off the dialog's own layout: the title bar has labels of its
    own - the mark and the name - and they are not the question."""
    with_note = widgets.Message(None, "Delete", "Delete it?", "It is final.")
    without = widgets.Message(None, "Delete", "Delete it?")
    try:
        assert with_note.detail is not None
        assert without.detail is None
    finally:
        with_note.deleteLater()
        without.deleteLater()


def test_the_answers_can_be_named_after_what_they_do(qapp):
    """"Run" beats "Yes": somebody reading only the buttons still knows."""
    from PySide6.QtWidgets import QPushButton

    dialog = widgets.Message(None, "Run", "Run it?", agree="Run",
                             refuse="Leave it")
    try:
        assert sorted(one.text() for one
                      in dialog.findChildren(QPushButton)) == ["Leave it",
                                                               "Run"]
    finally:
        dialog.deleteLater()


def test_it_is_modal_because_it_is_a_question(qapp):
    dialog = widgets.Message(None, "Run", "Run it?")
    try:
        assert dialog.isModal()
    finally:
        dialog.deleteLater()


# -------------------------------------------------- the wheel and a dropdown
# Qt's default is that the wheel moves a combo box's selection whenever the
# pointer is over it. On a form inside a scroll area that is a trap: somebody
# scrolls the page, the pointer passes over a dropdown, and the value changes
# underneath them - silently, and often unnoticed until something runs wrong.
def _wheel():
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent

    return QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, -120),
                       QPoint(0, -120), Qt.NoButton, Qt.NoModifier,
                       Qt.NoScrollPhase, False)


def test_the_wheel_does_not_change_a_dropdown(qapp):
    from PySide6.QtWidgets import QComboBox, QScrollArea, QWidget

    area = QScrollArea()
    inner = QWidget()
    area.setWidget(inner)
    box = QComboBox(inner)
    box.addItems(["one", "two", "three"])
    filter_ = widgets.stop_wheel_stealing(qapp)
    try:
        before = box.currentIndex()
        qapp.sendEvent(box, _wheel())
        assert box.currentIndex() == before
    finally:
        filter_.stop()
        area.deleteLater()


def test_the_wheel_does_not_change_a_spin_box_either(qapp):
    from PySide6.QtWidgets import QSpinBox

    box = QSpinBox()
    box.setRange(0, 10)
    box.setValue(5)
    filter_ = widgets.stop_wheel_stealing(qapp)
    try:
        qapp.sendEvent(box, _wheel())
        assert box.value() == 5
    finally:
        filter_.stop()
        box.deleteLater()


def test_the_page_under_it_still_scrolls(qapp):
    """Eating the event would trade a silent wrong value for a page that
    mysteriously refuses to scroll over half its own controls."""
    from PySide6.QtWidgets import QComboBox, QScrollArea, QWidget

    area = QScrollArea()
    inner = QWidget()
    inner.setFixedHeight(2000)
    area.setWidget(inner)
    area.setFixedHeight(200)
    box = QComboBox(inner)
    box.addItems(["one", "two", "three"])
    filter_ = widgets.stop_wheel_stealing(qapp)
    try:
        before = area.verticalScrollBar().value()
        qapp.sendEvent(box, _wheel())
        assert area.verticalScrollBar().value() != before
    finally:
        filter_.stop()
        area.deleteLater()


def test_anything_that_is_not_one_of_those_is_left_alone(qapp):
    from PySide6.QtWidgets import QScrollArea

    area = QScrollArea()
    filter_ = widgets.stop_wheel_stealing(qapp)
    try:
        assert filter_.eventFilter(area, _wheel()) is False
    finally:
        filter_.stop()
        area.deleteLater()


# ------------------------------------------------------ and it wears our frame
def test_a_confirmation_wears_the_application_s_own_title_bar(qapp):
    from cms_gui import titlebar

    if not titlebar.wanted():
        import pytest
        pytest.skip("the desktop's frame was asked for")

    dialog = widgets.Message(None, "Run", "Run it?")
    try:
        assert dialog.frame is not None
        assert dialog.frame.bar.name.text() == "Run", "its own name, not the brand"
        assert dialog.frame.grips == [], "a two-button box is not resizable"
        assert not dialog.frame.bar.minimize_button.isVisibleTo(dialog.frame.bar)
        assert dialog.frame.bar.close_button.isVisibleTo(dialog.frame.bar)
    finally:
        dialog.deleteLater()


def test_a_dressed_window_can_still_be_resized(qapp):
    """Taking the desktop's frame off takes its resize handles with it, so a
    window dressed without them silently loses something it had - and a form
    whose field is too small to read what is in it cannot be used."""
    from PySide6.QtWidgets import QDialog

    dialog = QDialog()
    dialog.setWindowTitle("Edit service")
    frame = widgets.dress(dialog)
    try:
        assert frame.grips, "a window that had a frame keeps its handles"
    finally:
        dialog.deleteLater()


def test_only_a_window_that_never_had_a_frame_gives_that_up(qapp):
    message = widgets.Message(None, "Run", "Run it?")
    try:
        assert message.frame.grips == []
    finally:
        message.deleteLater()


def test_a_window_somebody_works_in_gets_all_three_controls(qapp):
    """A console filling with output is minimized and maximized; a form is
    filled in and closed."""
    from PySide6.QtWidgets import QDialog

    worked_in = QDialog()
    worked_in.setWindowTitle("Console")
    form = QDialog()
    form.setWindowTitle("Edit service")
    try:
        assert widgets.dress(worked_in, minimizable=True).bar._controls == \
            ("minimize", "maximize", "close")
        assert widgets.dress(form).bar._controls == ("close",)
    finally:
        worked_in.deleteLater()
        form.deleteLater()


def test_the_title_comes_from_the_window_when_none_is_given(qapp):
    from PySide6.QtWidgets import QDialog

    dialog = QDialog()
    dialog.setWindowTitle("Edit service")
    try:
        assert widgets.dress(dialog).bar.name.text() == "Edit service"
    finally:
        dialog.deleteLater()


def test_the_wheel_over_an_editable_dropdown_s_own_field_is_caught_too(qapp):
    """An editable combo box is made of child widgets, and a wheel over the
    line edit inside one is a wheel over the control as far as anybody using
    it is concerned. Testing only the box itself missed this entirely."""
    from PySide6.QtWidgets import QComboBox

    box = QComboBox()
    box.setEditable(True)
    box.addItems(["one", "two", "three"])
    box.setCurrentIndex(1)
    filter_ = widgets.stop_wheel_stealing(qapp)
    try:
        qapp.sendEvent(box.lineEdit(), _wheel())
        assert box.currentIndex() == 1
    finally:
        filter_.stop()
        box.deleteLater()


def test_the_wheel_inside_a_spin_box_s_own_field_is_caught_too(qapp):
    from PySide6.QtWidgets import QLineEdit, QSpinBox

    box = QSpinBox()
    box.setRange(0, 10)
    box.setValue(5)
    filter_ = widgets.stop_wheel_stealing(qapp)
    try:
        inner = box.findChild(QLineEdit)
        qapp.sendEvent(inner or box, _wheel())
        assert box.value() == 5
    finally:
        filter_.stop()
        box.deleteLater()


def test_the_open_list_still_scrolls(qapp):
    """The whole point of a long dropdown is getting down it."""
    from PySide6.QtWidgets import QComboBox

    box = QComboBox()
    box.addItems([str(n) for n in range(50)])
    filter_ = widgets.stop_wheel_stealing(qapp)
    try:
        assert filter_.eventFilter(box.view(), _wheel()) is False
    finally:
        filter_.stop()
        box.deleteLater()


def test_a_dropdown_on_a_real_dialog_of_this_application_is_guarded(qapp):
    """Synthetic widgets prove the filter; this proves it reaches the ones
    somebody actually meets."""
    from PySide6.QtWidgets import QComboBox

    from cms_gui.pages.logsources import ConnectionDialog

    filter_ = widgets.stop_wheel_stealing(qapp)
    dialog = ConnectionDialog()
    dialog.ensurePolished()
    try:
        box = dialog.findChild(QComboBox)
        assert box is not None and box.count() > 1
        before = box.currentIndex()
        qapp.sendEvent(box, _wheel())
        assert box.currentIndex() == before
    finally:
        filter_.stop()
        dialog.deleteLater()


def test_new_controls_and_replaced_editors_are_guarded_when_shown(qapp, dispose):
    from PySide6.QtWidgets import QComboBox, QDialog, QLineEdit, QSpinBox, QVBoxLayout

    filter_ = widgets.stop_wheel_stealing(qapp)
    dialog = QDialog()
    layout = QVBoxLayout(dialog)
    combo = QComboBox()
    combo.addItems(["one", "two", "three"])
    combo.setCurrentIndex(1)
    spin = QSpinBox()
    spin.setRange(0, 10)
    spin.setValue(5)
    layout.addWidget(combo)
    layout.addWidget(spin)
    dialog.show()
    try:
        # Editors can be added or replaced after their parent was polished.
        combo.setEditable(True)
        spin.setLineEdit(QLineEdit())
        qapp.processEvents()
        qapp.sendEvent(combo.lineEdit(), _wheel())
        qapp.sendEvent(spin.lineEdit(), _wheel())
        assert combo.currentIndex() == 1
        assert spin.value() == 5
    finally:
        filter_.stop()
        dispose(dialog)
