"""The interface's marks, and the sheets that paint around them.

The GUI runs on three platforms with three different font sets, and a character
the font lacks renders as an empty box - or, once a fallback is chosen for it,
as "%" where a gear was meant to be. So no mark is a character at all any more:
they are painted (cms_gui/icons.py), and these tests hold that line.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import icons, theme

GUI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_every_mark_paints_something(qapp):
    """The property a character could never guarantee: it is actually drawn."""
    faint = []
    for name in icons.DRAWINGS:
        image = icons.pixmap(name, 32).toImage()
        inked = sum(1 for y in range(32) for x in range(32)
                    if image.pixelColor(x, y).alpha() > 0)
        if inked < 20:
            faint.append("%s (%d pixels)" % (name, inked))
    assert not faint, "these would look blank: %s" % ", ".join(faint)


def test_every_mark_the_interface_asks_for_exists(qapp):
    """A name with no drawing is a silently empty button, so none may be missing."""
    import re
    wanted = set()
    for root, _dirs, files in os.walk(os.path.join(GUI_DIR, "cms_gui")):
        for name in sorted(files):
            if not name.endswith(".py") or name == "icons.py":
                continue
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                text = fh.read()
            wanted |= set(re.findall(r"""icons\.(?:icon|pixmap|button)\(\s*[^,)]*,\s*["'](\w+)["']""", text))
            wanted |= set(re.findall(r"""icons\.(?:icon|pixmap)\(["'](\w+)["']""", text))
    assert wanted, "the scan found no icon call sites at all"
    assert wanted <= set(icons.DRAWINGS), (
        "no drawing for: %s" % ", ".join(sorted(wanted - set(icons.DRAWINGS))))


def test_a_mark_is_the_widgets_icon_not_part_of_its_label(qapp):
    from PySide6.QtWidgets import QPushButton
    button = icons.button(QPushButton("Settings"), "settings")
    assert button.text() == "Settings"       # no character smuggled into the text
    assert not button.icon().isNull()


def test_an_unknown_mark_is_blank_rather_than_a_crash(qapp):
    # A page asking for a name nobody drew must not take the window down.
    assert icons.pixmap("no-such-icon", 16).toImage().pixelColor(8, 8).alpha() == 0
    assert icons.icon("no-such-icon").availableSizes()


def test_colour_is_the_callers_choice(qapp):
    """Status marks are drawn in the colour of the status they report."""
    red = icons.pixmap("fail", 32, theme.BAD).toImage()
    seen = {red.pixelColor(x, y).name().lower()
            for x in range(32) for y in range(32)
            if red.pixelColor(x, y).alpha() > 200}
    assert theme.BAD.lower() in seen


def test_no_module_hard_codes_a_symbol():
    """Guards the rule, not just today's symbols.

    A literal glyph in a page is exactly how the broken '⧉' and '⌗' got
    in: they looked fine in the design's web font, rendered as boxes in DejaVu,
    and on Windows resolved to their ASCII stand-ins.
    """
    offenders = []
    for root, _dirs, files in os.walk(os.path.join(GUI_DIR, "cms_gui")):
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as fh:
                for number, line in enumerate(fh, start=1):
                    if line.lstrip().startswith("#"):
                        continue        # prose in a comment is fine
                    for char in line:
                        # Symbols/pictographs live well above Latin-1; the em
                        # dash, ellipsis and middle dot used in prose do not.
                        if ord(char) > 0x2100:
                            offenders.append("%s:%d %r" % (name, number, char))
    assert not offenders, ("draw it in icons.py instead of typing it: %s"
                           % ", ".join(offenders[:8]))


# ------------------------------------------------- stylesheets that cascade

def _dominant_colour(widget):
    image = widget.grab().toImage()
    counts = {}
    for x in range(image.width()):
        for y in range(image.height()):
            name = image.pixelColor(x, y).name()
            counts[name] = counts.get(name, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]


def test_a_primary_button_keeps_its_fill_inside_a_styled_container(qapp):
    """A container's stylesheet must not repaint the widgets inside it.

    Regression: the preview strip set a plain `background:`, which cascades to
    its children and outranks the application sheet. The RUN button lost its
    accent fill and painted its near-white label onto the strip's near-white
    background - a button that looked empty.
    """
    from cms_gui.pages.command import CommandPage
    from cms_gui.settings import Settings
    page = CommandPage(Settings())
    page.resize(1100, 700)
    page.show()
    qapp.processEvents()
    assert _dominant_colour(page.run_button).lower() == theme.ACCENT.lower()


def test_a_ticked_box_actually_has_a_tick_in_it(qapp):
    """Regression: a stylesheet can fill an indicator but cannot mark it.

    `::indicator:checked` only ever set a background, so a ticked box was an
    accent square with nothing inside - indistinguishable at a glance from a
    disabled one. The mark is painted by the style now, which this proves is
    still reached: a stylesheet rule for `::indicator` would silently take the
    drawing back off it, and the box would go blank again.
    """
    from PySide6.QtWidgets import QCheckBox
    box = QCheckBox("Leave the windows open")
    box.setChecked(True)
    box.show()
    qapp.processEvents()
    inside = _indicator(box).copy(3, 3, theme.INDICATOR - 6, theme.INDICATOR - 6)
    lightnesses = [inside.pixelColor(x, y).lightness()
                   for x in range(inside.width()) for y in range(inside.height())]
    assert min(lightnesses) < 160, "no accent fill"
    assert max(lightnesses) > 200, "filled, but nothing drawn in it"


def test_an_unticked_box_is_not_filled(qapp):
    from PySide6.QtWidgets import QCheckBox
    box = QCheckBox("Leave the windows open")
    box.show()
    qapp.processEvents()
    inside = _indicator(box).copy(3, 3, theme.INDICATOR - 6, theme.INDICATOR - 6)
    darkest = min(inside.pixelColor(x, y).lightness()
                  for x in range(inside.width()) for y in range(inside.height()))
    assert darkest > 200


def test_the_rows_of_a_list_get_the_same_tick_as_a_standalone_box(qapp):
    """The Accounts, Extensions and Scenarios lists are check boxes too.

    Those indicators are drawn by the style rather than by a widget, so they are
    the half of the change a stylesheet could never have covered.
    """
    from cms_gui import widgets
    check_list = widgets.CheckList()
    check_list.add("role_admin", "role_admin", "VRR Admin")
    check_list.set_checked(["role_admin"])
    check_list.resize(300, 160)
    check_list.show()
    qapp.processEvents()
    image = check_list.list.viewport().grab().toImage()
    accent = theme.color(theme.ACCENT).lightness()
    assert any(abs(image.pixelColor(x, y).lightness() - accent) < 12
               for x in range(min(image.width(), theme.INDICATOR * 2))
               for y in range(min(image.height(), theme.INDICATOR * 2)))


def _indicator(box):
    """The tick box cropped out of a rendered QCheckBox."""
    from PySide6.QtWidgets import QStyle, QStyleOptionButton
    option = QStyleOptionButton()
    option.initFrom(box)
    rect = box.style().subElementRect(QStyle.SE_CheckBoxIndicator, option, box)
    return box.grab().toImage().copy(rect)


def test_scoped_style_does_not_reach_children(qapp):
    from PySide6.QtWidgets import QPushButton, QVBoxLayout, QWidget
    from cms_gui import widgets
    box = QWidget()
    layout = QVBoxLayout(box)
    button = QPushButton("x")
    layout.addWidget(button)
    widgets.scoped_style(box, "background: #123456;")
    assert box.styleSheet().startswith("#")          # scoped by object name
    assert "#123456" not in button.styleSheet()


# ----------------------------------------------------- tables that read as tables
def _table_tones(qapp):
    """(page, header, selected row) as actually painted, in the current mode."""
    from PySide6.QtWidgets import QTableWidget, QTableWidgetItem

    table = QTableWidget(2, 2)
    table.setHorizontalHeaderLabels(["One", "Two"])
    table.verticalHeader().setVisible(False)
    table.setProperty("rows", "quiet")
    for row in range(2):
        for column in range(2):
            table.setItem(row, column, QTableWidgetItem(""))
    table.resize(240, 120)
    table.selectRow(0)
    table.show()
    qapp.processEvents()
    image = table.grab().toImage()
    header = table.horizontalHeader().height()
    return (theme.BG,
            image.pixelColor(120, header // 2).name(),
            image.pixelColor(120, header + table.rowHeight(0) // 2).name())


def test_a_table_header_is_a_band_rather_than_the_page(qapp):
    """Painted in the page's own colour it is not a band at all.

    It is small grey text floating above some rows, and a page carrying four
    tables then reads as one undifferentiated field with words scattered
    through it.
    """
    page, header, _selected = _table_tones(qapp)
    assert header != page


def test_a_selected_row_is_never_mistaken_for_a_header(qapp):
    # Both are a tint off the page, so they have to be different tints - a
    # selection that looked like a header would put the confusion back.
    page, header, selected = _table_tones(qapp)
    assert len({page, header, selected}) == 3


def test_the_header_and_the_selection_step_away_from_the_page_in_both_modes(qapp):
    """The ramp inverts with the mode, so the ordering does rather than the values.

    Light: the page is lightest and each tint is darker. Dark: the page is
    darkest and each tint is lighter. What must hold either way is that the
    selection is one step further out than the header.
    """
    from PySide6.QtGui import QColor

    for dark in (False, True):
        theme.set_dark_mode(dark)
        qapp.setStyleSheet(theme.stylesheet())
        try:
            page, header, selected = _table_tones(qapp)
            levels = [QColor(name).lightness()
                      for name in (page, header, selected)]
            assert levels == sorted(levels, reverse=not dark), (
                "%s mode: %s" % ("dark" if dark else "light", levels))
        finally:
            theme.set_dark_mode(False)
            qapp.setStyleSheet(theme.stylesheet())


def test_a_selected_row_in_the_dark_theme_is_a_deep_band_with_light_ink(qapp, dispose):
    """The accent ramp is the brand and stays pale in both themes. Selection
    painted from it put a pale band with dark ink on a dark page - the row
    looked cut out of the page rather than picked."""
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem

    theme.set_dark_mode(True)
    qapp.setStyleSheet(theme.stylesheet())
    tree = QTreeWidget()
    try:
        tree.setHeaderHidden(True)
        item = QTreeWidgetItem(["picked"])
        tree.addTopLevelItem(item)
        tree.resize(200, 80)
        tree.setCurrentItem(item)
        tree.show()
        qapp.processEvents()
        rect = tree.visualItemRect(item)
        band = tree.viewport().grab().toImage().pixelColor(rect.right() - 4,
                                                           rect.center().y())
        assert band.name() == QColor(theme.SELECT_BG).name()
        assert band.lightness() < 128, "a deep band, not a pale one"
        assert QColor(theme.SELECT_INK).lightness() > band.lightness() + 100
    finally:
        dispose(tree)
        theme.set_dark_mode(False)
        qapp.setStyleSheet(theme.stylesheet())


def test_the_light_theme_selection_is_what_it_always_was():
    assert (theme.SELECT_BG, theme.SELECT_INK) == (theme.ACCENT_RAMP[200],
                                                   theme.ACCENT_RAMP[900])
