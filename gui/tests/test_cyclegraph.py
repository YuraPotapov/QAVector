"""Laying a cycle out on a grid.

No Qt here, and so no ``qapp`` and no ``dispose``: ``layout_graph`` is a pure
function of the payload and the spec, which is the reason it was written as one.
The items and the canvas are exercised in test_cycles_page.py, where there is a
widget to hang them on.

The assertions are all **relative** - one column per layer, this node above that
one, twice the same answer - never pixel coordinates. A redesign changes SPEC,
and a test that asserted 208 would fail for a change that is not a bug.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The core lives one directory up and is not installed; the GUI never imports it
# at runtime, but a test may - here, to check the statuses the canvas can draw
# against the ones a run can actually produce.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from cms_gui.cyclegraph import (SIDES, SPEC, _elbow, _rule, _sides,
                                layout_graph, scene_size)


def nodes(*spec):
    """``("a", 0, 0), ("b", 1, 0)`` -> the node dicts the core would produce."""
    return [{"id": name, "layer": layer, "row": row, "plugin": "command.shell"}
            for name, layer, row in spec]


CHAIN = nodes(("a", 0, 0), ("b", 1, 0), ("c", 2, 0))
DIAMOND = nodes(("a", 0, 0), ("b", 1, 0), ("c", 1, 1), ("d", 2, 0))


# ------------------------------------------------------------------ the flow
# The graph runs down the page, the way the workflow diagrams it is drawn from
# are: a twenty-step chain is an ordinary cycle, twenty nodes across is a line
# nobody can read, and twenty down is a list, which is a thing people read all
# day. These are written in terms of "along the flow" and "across it" rather
# than in x and y, so they say what is being defended rather than which axis it
# happened to land on.
def along(place):
    """How far down the flow a node sits."""
    return place[1]


def across(place):
    """Where it sits among the others of its layer."""
    return place[0]


def test_a_chain_puts_one_node_in_each_layer():
    places = layout_graph(CHAIN)
    assert len(places) == 3
    steps = [along(places[name]) for name in ("a", "b", "c")]
    assert steps[0] < steps[1] < steps[2]


def test_a_chain_stays_in_one_line_across():
    places = layout_graph(CHAIN)
    assert len({round(across(places[name])) for name in ("a", "b", "c")}) == 1


def test_two_branches_share_a_layer_and_are_set_side_by_side():
    places = layout_graph(DIAMOND)
    assert along(places["b"]) == along(places["c"])
    assert across(places["b"]) < across(places["c"])


def test_rows_follow_the_order_the_core_gave_them():
    """The core puts them in file order; the canvas does not reorder anything."""
    places = layout_graph(nodes(("root", 0, 0), ("second", 1, 0), ("first", 1, 1)))
    assert across(places["second"]) < across(places["first"])


def test_layers_are_evenly_spaced():
    places = layout_graph(CHAIN)
    first = along(places["b"]) - along(places["a"])
    second = along(places["c"]) - along(places["b"])
    assert round(first, 6) == round(second, 6)


def test_a_layer_is_centred_against_the_widest_one():
    """So a fork looks like a fork rather than hanging off one side."""
    places = layout_graph(DIAMOND)
    middle = (across(places["b"]) + across(places["c"])) / 2.0
    assert round(across(places["a"]), 6) == round(middle, 6)
    assert round(across(places["d"]), 6) == round(middle, 6)


def test_the_layer_with_the_most_nodes_sets_the_width():
    places = layout_graph(nodes(("a", 0, 0), ("b", 1, 0), ("c", 1, 1),
                                ("d", 1, 2), ("e", 2, 0)))
    assert across(places["b"]) < across(places["c"]) < across(places["d"])
    assert across(places["a"]) == across(places["e"])


def test_several_roots_all_start_in_the_first_layer():
    places = layout_graph(nodes(("a", 0, 0), ("b", 0, 1), ("c", 0, 2)))
    assert len({along(places[name]) for name in ("a", "b", "c")}) == 1


def test_a_deep_cycle_is_taller_than_it_is_wide():
    """The point of the direction. Twenty steps across is five thousand pixels
    of line; twenty down is a list."""
    deep = nodes(*[(str(n), n, 0) for n in range(20)])
    width, height = scene_size(layout_graph(deep))
    assert height > width * 2


def test_the_direction_is_a_value_rather_than_a_shape_in_the_code():
    """So going back to left-to-right, or offering it, changes the spec and
    nothing else."""
    import copy

    sideways = copy.deepcopy(SPEC)
    sideways["grid"]["flow"] = "right"
    places = layout_graph(CHAIN, sideways)

    xs = [places[name][0] for name in ("a", "b", "c")]
    assert xs[0] < xs[1] < xs[2]
    assert len({round(places[name][1]) for name in ("a", "b", "c")}) == 1


# ---------------------------------------------------------------- properties
def test_the_same_graph_lays_out_the_same_way_twice():
    """The canvas diffs by key across pushes; a moving layout would defeat it."""
    assert layout_graph(DIAMOND) == layout_graph(DIAMOND)


def test_the_order_the_nodes_arrive_in_does_not_change_the_result():
    forwards = layout_graph(DIAMOND)
    backwards = layout_graph(list(reversed(DIAMOND)))
    assert forwards == backwards


def test_nothing_is_ever_placed_at_a_negative_coordinate():
    for places in (layout_graph(CHAIN), layout_graph(DIAMOND)):
        assert all(x >= 0 and y >= 0 for x, y in places.values())


def test_no_two_nodes_land_on_top_of_each_other():
    places = layout_graph(nodes(("a", 0, 0), ("b", 1, 0), ("c", 1, 1),
                                ("d", 1, 2), ("e", 2, 0), ("f", 2, 1)))
    assert len(set(places.values())) == len(places)


def test_an_empty_graph_lays_out_to_nothing():
    assert layout_graph([]) == {}
    assert scene_size({}) == (0.0, 0.0)


def test_one_node_is_a_perfectly_good_graph():
    places = layout_graph(nodes(("only", 0, 0)))
    assert len(places) == 1


def test_a_node_with_no_grid_position_is_treated_as_the_first_one():
    """A payload from a core that predates layers still draws."""
    places = layout_graph([{"id": "a"}, {"id": "b"}])
    assert len(places) == 2


# -------------------------------------------------------------------- extent
def test_the_scene_is_big_enough_to_hold_every_node_and_its_margin():
    places = layout_graph(DIAMOND)
    width, height = scene_size(places)
    margin = SPEC["grid"]["margin"]

    assert width >= max(x for x, _y in places.values()) + SPEC["node"]["w"]
    assert height >= max(y for _x, y in places.values()) + SPEC["node"]["h"]
    assert width - (max(x for x, _y in places.values())
                    + SPEC["node"]["w"]) == margin


def test_a_deeper_graph_needs_a_bigger_scene():
    short = scene_size(layout_graph(nodes(("a", 0, 0))))
    long = scene_size(layout_graph(CHAIN))
    assert long[1] > short[1], "deeper is taller, now that the flow runs down"
    assert long[0] == short[0], "and no wider: a chain has one node per layer"


# ----------------------------------------------------------------- the spec
def test_the_look_is_all_in_one_place():
    """A redesign changes values here and nothing else moves."""
    assert set(SPEC) == {"node", "grid", "edge", "text", "zoom", "canvas"}
    assert SPEC["node"]["w"] > 0 and SPEC["node"]["h"] > 0


def test_a_different_spec_lays_out_differently_without_touching_the_code():
    """Which is the whole point of the spec being an argument."""
    import copy

    taller = copy.deepcopy(SPEC)
    taller["node"]["h"] = SPEC["node"]["h"] * 2
    assert layout_graph(CHAIN, taller)["c"][1] > layout_graph(CHAIN)["c"][1]


def test_no_colour_is_written_into_the_spec():
    """Colours move when dark mode is set, so they are read from theme instead."""
    import json

    text = json.dumps(SPEC)
    assert "#" not in text


# ------------------------------------------------------------------ the surface
def test_the_canvas_declares_its_own_grid_and_how_far_it_roams():
    """Room to roam is what makes the surface draggable when it all fits."""
    assert SPEC["canvas"]["grid"] > 0
    assert SPEC["canvas"]["roam_min"] > 0


# ------------------------------------------------------------------- the edges
# Which sides an edge uses is a pure function of where the two nodes are, so it
# belongs here rather than in the widget tests. The rule exists because a node
# can be dragged anywhere: an edge that always left to the right and arrived
# from the left drew a loop out and back the moment two steps were stacked, and
# stacking them is the first thing people do.

class _At(object):
    """Just enough of a node's position for _sides to read."""

    def __init__(self, x, y):
        self._x, self._y = x, y

    def x(self):
        return self._x

    def y(self):
        return self._y


def _between(source, target):
    return _sides(_At(*source), _At(*target), SPEC["node"])


def test_a_node_to_the_right_is_left_by_the_right_side():
    out, into = _between((0, 0), (400, 0))
    assert (out, into) == (SIDES["right"], SIDES["left"])


def test_a_node_to_the_left_is_left_by_the_left_side():
    """Otherwise the curve crosses the node it is leaving."""
    out, into = _between((400, 0), (0, 0))
    assert (out, into) == (SIDES["left"], SIDES["right"])


def test_a_node_below_is_left_by_the_bottom():
    out, into = _between((0, 0), (0, 400))
    assert (out, into) == (SIDES["bottom"], SIDES["top"])


def test_a_node_above_is_left_by_the_top():
    out, into = _between((0, 400), (0, 0))
    assert (out, into) == (SIDES["top"], SIDES["bottom"])


def test_two_nodes_stacked_with_a_small_drift_still_go_bottom_to_top():
    """Dragging a column by hand never leaves it perfectly aligned."""
    out, into = _between((60, 20), (90, 150))
    assert (out, into) == (SIDES["bottom"], SIDES["top"])


def test_the_default_layout_leaves_the_bottom_and_arrives_at_the_top():
    """A layer gap is smaller than a node is wide, so the axis has to be judged
    against the node's proportions rather than by raw distance."""
    places = layout_graph(CHAIN)
    out, into = _between(places["a"], places["b"])
    assert (out, into) == (SIDES["bottom"], SIDES["top"])


def test_two_branches_of_one_layer_still_join_at_the_top_of_the_next():
    places = layout_graph(DIAMOND)
    for branch in ("b", "c"):
        out, into = _between(places[branch], places["d"])
        assert (out, into) == (SIDES["bottom"], SIDES["top"]), branch


def test_every_side_is_a_unit_direction():
    """The curve, the arrow and the gap are all built by multiplying by these."""
    for name, (dx, dy) in SIDES.items():
        assert abs(dx) + abs(dy) == 1, name


# ---------------------------------------------------------------- the routing
# Right angles rather than curves, so the canvas reads as a diagram. The points
# are a pure function of the two ends, which is why this is here: the only Qt
# in it is QPointF, and a corner that is not square is arithmetic, not paint.

def _points(start, end, out, stub=18):
    from PySide6.QtCore import QPointF

    found = _elbow(QPointF(*start), QPointF(*end), out, stub)
    return [(round(one.x(), 3), round(one.y(), 3)) for one in found]


def _turns_are_square(points):
    """Every segment runs along one axis. That is what a right angle means."""
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        if ax != bx and ay != by:
            return False
    return True


def test_every_corner_is_a_right_angle_going_across():
    assert _turns_are_square([(0, 0)] + _points((0, 0), (400, 120),
                                                SIDES["right"]))


def test_every_corner_is_a_right_angle_going_down():
    assert _turns_are_square([(0, 0)] + _points((0, 0), (120, 400),
                                                SIDES["bottom"]))


def test_a_line_leaves_its_node_before_it_is_allowed_to_turn():
    """A corner on the node's own edge reads as part of the box."""
    points = _points((0, 0), (400, 120), SIDES["right"])
    assert points[0] == (18, 0)


def test_two_nodes_in_line_are_joined_without_a_detour():
    """The middle points collapse onto the ends, and lineTo a point it is
    already at draws nothing - so the straight case needs no branch."""
    points = _points((0, 50), (400, 50), SIDES["right"])
    assert all(y == 50 for _x, y in points)
    assert _turns_are_square(points)


def test_a_sideways_line_turns_halfway_between_the_two():
    """So a fork's branches bend on the same line and read as one splitting."""
    points = _points((0, 0), (400, 200), SIDES["right"])
    middles = [x for x, _y in points[1:3]]
    assert middles == [middles[0], middles[0]]
    assert middles[0] == (18 + 400 - 18) / 2


def test_two_branches_of_a_fork_turn_on_the_same_line():
    up = _points((0, 0), (400, -120), SIDES["right"])
    down = _points((0, 0), (400, 120), SIDES["right"])
    assert up[1][0] == down[1][0]


def test_a_downward_line_turns_halfway_down():
    points = _points((0, 0), (200, 400), SIDES["bottom"])
    middles = [y for _x, y in points[1:3]]
    assert middles == [middles[0], middles[0]]
    assert middles[0] == (18 + 400 - 18) / 2


def test_the_last_point_is_where_the_head_will_be():
    """The arrow is drawn separately and has to meet the line it belongs to."""
    assert _points((0, 0), (400, 120), SIDES["right"])[-1] == (400, 120)


# --------------------------------------------------------------- the ruling
# _rule only ever calls setPen and drawLine, so a recorder stands in for the
# painter and the geometry is checked without a widget or a pixel.

class _Recorder(object):
    def __init__(self):
        self.lines = []          # (pen, x1, y1, x2, y2)
        self._pen = None

    def setPen(self, pen):       # noqa: N802 - QPainter's own spelling
        self._pen = pen

    def drawLine(self, first, second):    # noqa: N802
        self.lines.append((self._pen, first.x(), first.y(),
                           second.x(), second.y()))

    def ruled(self, rect, canvas=None):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor

        _rule(self, QRectF(*rect), canvas or SPEC["canvas"],
              QColor("#000001"), QColor("#000002"))
        return self.lines

    def verticals(self):
        return sorted(x1 for _p, x1, _y1, x2, _y2 in self.lines if x1 == x2)

    def horizontals(self):
        return sorted(y1 for _p, _x1, y1, _x2, y2 in self.lines if y1 == y2)

    def major_verticals(self):
        return sorted(x1 for pen, x1, _y1, x2, _y2 in self.lines
                      if x1 == x2 and pen.color().name() == "#000002")


def test_the_canvas_is_ruled_both_ways():
    rec = _Recorder()
    rec.ruled((0, 0, 100, 100))
    assert rec.verticals() and rec.horizontals()


def test_every_line_sits_on_a_multiple_of_the_step():
    step = SPEC["canvas"]["grid"]
    rec = _Recorder()
    rec.ruled((0, 0, 100, 100))
    assert all(x % step == 0 for x in rec.verticals())
    assert all(y % step == 0 for y in rec.horizontals())


def test_the_heavy_lines_are_every_so_many_squares():
    canvas = SPEC["canvas"]
    rec = _Recorder()
    rec.ruled((0, 0, 400, 100))
    heavy = rec.major_verticals()
    assert len(heavy) >= 2
    assert all(x % (canvas["grid"] * canvas["major"]) == 0 for x in heavy)


def test_the_heavy_lines_do_not_slide_about_as_the_canvas_is_panned():
    """They are numbered from the scene's origin, not from the corner of the
    view - otherwise the ruling would crawl while you drag."""
    step = SPEC["canvas"]["grid"] * SPEC["canvas"]["major"]
    here = _Recorder()
    here.ruled((0, 0, 400, 100))
    there = _Recorder()
    there.ruled((step * 3, 0, step * 3 + 400, 100))

    assert set(here.major_verticals()) <= set(range(0, 100000, step))
    assert set(there.major_verticals()) <= set(range(0, 100000, step))
    assert there.major_verticals()[0] >= step * 3


def test_a_view_zoomed_out_past_reason_is_not_ruled_at_all():
    """A million lines would take a second to paint and read as a grey wash."""
    rec = _Recorder()
    rec.ruled((0, 0, 10 ** 7, 10 ** 7))
    assert rec.lines == []


def test_the_ruling_has_a_heavier_line_worth_having():
    assert SPEC["canvas"]["major"] > 1


def test_the_grid_is_square_not_skewed():
    """The artwork's grid runs at an angle because the whole scene there is
    drawn in projection. The paper itself is not skewed."""
    rec = _Recorder()
    rec.ruled((0, 0, 100, 100))
    for _pen, x1, y1, x2, y2 in rec.lines:
        assert x1 == x2 or y1 == y2


# ------------------------------------------------------------------ statuses
def test_every_status_a_step_can_wear_has_a_look_and_a_word():
    """A status the tables do not know falls back to "pending", which draws a
    live step as one that has not started."""
    from cms_gui.cyclegraph import STATUS_LOOK, STATUS_WORD
    from domain.cycle import (CANCELLED, FAILED, PENDING, RUNNING, SKIPPED,
                              SUCCESS, TIMEOUT, WAITING)

    for status in (PENDING, RUNNING, WAITING, SUCCESS, FAILED, TIMEOUT,
                   SKIPPED, CANCELLED):
        assert status in STATUS_LOOK, status
        assert status in STATUS_WORD, status


def test_waiting_is_drawn_as_live_rather_than_as_finished():
    """It is still the run's business, and drawing it grey like a skipped step
    would say the opposite."""
    from cms_gui.cyclegraph import STATUS_LOOK, ink

    assert ink(STATUS_LOOK["waiting"][1]) != ink(STATUS_LOOK["skipped"][1])
    assert ink(STATUS_LOOK["waiting"][1]) != ink(STATUS_LOOK["pending"][1])


def test_waiting_is_told_apart_from_running():
    """The difference worth seeing at a glance is working against parked."""
    from cms_gui.cyclegraph import STATUS_LOOK, STATUS_WORD, ink

    assert ink(STATUS_LOOK["waiting"][1]) != ink(STATUS_LOOK["running"][1])
    assert STATUS_WORD["waiting"] != STATUS_WORD["running"]


def test_a_colour_is_still_never_a_value_in_the_tables():
    from cms_gui.cyclegraph import STATUS_LOOK, ink

    for _mark, colour in STATUS_LOOK.values():
        assert not colour.startswith("#")
        assert ink(colour).startswith("#"), colour


# ------------------------------------------------------- going round things
# Two edges drawn on the same pixels are one edge as far as a reader is
# concerned. In a column every node shares an x, so an edge that skips a layer
# runs straight down through the nodes between its ends and along every short
# link on the way - and a real cycle has more of those than it has short ones.
def test_a_line_through_empty_space_is_left_alone():
    """Only the edges that need it should be bent; a short link is already
    the clearest thing it can be."""
    from cms_gui.cyclegraph import _detour, _elbow

    start, end = _point(0, 0), _point(0, 200)
    assert _elbow(start, end, SIDES["bottom"], 18) != \
        _detour(start, end, SIDES["bottom"], 18, 400)


def test_a_detour_turns_only_at_right_angles():
    from cms_gui.cyclegraph import _detour

    points = _detour(_point(100, 0), _point(100, 500), SIDES["bottom"], 18, 400)
    for one, two in zip(points, points[1:]):
        assert one.x() == two.x() or one.y() == two.y()


def test_a_detour_actually_travels_in_the_lane_it_was_given():
    from cms_gui.cyclegraph import _detour

    points = _detour(_point(100, 0), _point(100, 500), SIDES["bottom"], 18, 400)
    assert any(point.x() == 400 for point in points)


def test_a_detour_starts_and_ends_where_the_edge_does():
    """It goes around; it does not go somewhere else."""
    from cms_gui.cyclegraph import _detour

    start, end = _point(100, 0), _point(100, 500)
    points = _detour(start, end, SIDES["bottom"], 18, 400)
    assert points[0].x() == start.x()
    assert points[-1] == end


def test_crossing_is_noticed():
    from cms_gui.cyclegraph import _crosses
    from PySide6.QtCore import QRectF

    box = QRectF(50, 50, 100, 50)
    assert _crosses([_point(100, 0), _point(100, 200)], [box]) is True
    assert _crosses([_point(300, 0), _point(300, 200)], [box]) is False


# ------------------------------------------------------ sharing one side
def test_one_edge_attaches_to_the_middle_of_its_side():
    """A node with a single link must look exactly as it always did."""
    from cms_gui.cyclegraph import _anchor

    place = _point(0, 0)
    assert _anchor(place, SPEC["node"], SIDES["bottom"]) == \
        _anchor(place, SPEC["node"], SIDES["bottom"], 0, 1)
    assert _anchor(place, SPEC["node"], SIDES["bottom"]).x() == \
        SPEC["node"]["w"] / 2.0


def test_edges_sharing_a_side_are_spread_across_it():
    """Three lines leaving one pixel are one line to a reader."""
    from cms_gui.cyclegraph import _anchor

    place = _point(0, 0)
    xs = [_anchor(place, SPEC["node"], SIDES["bottom"], slot, 3).x()
          for slot in range(3)]
    assert len(set(xs)) == 3
    assert xs == sorted(xs)
    assert all(0 < x < SPEC["node"]["w"] for x in xs)


def test_spreading_down_a_left_or_right_side_moves_y_instead():
    from cms_gui.cyclegraph import _anchor

    place = _point(0, 0)
    ys = [_anchor(place, SPEC["node"], SIDES["right"], slot, 3).y()
          for slot in range(3)]
    xs = {_anchor(place, SPEC["node"], SIDES["right"], slot, 3).x()
          for slot in range(3)}
    assert len(set(ys)) == 3
    assert len(xs) == 1, "the side itself does not move"


def _point(x, y):
    from PySide6.QtCore import QPointF
    return QPointF(x, y)


# --------------------------------------------------------- what a step is
# The stripe down a node's left edge says what *happened* to it. The wash
# behind it says what it *is*. Two channels, and the whole risk here is that
# they start speaking the same language.
def test_the_family_is_the_part_of_the_id_before_the_dot():
    """The naming convention is already the grouping, so a plugin added later
    is grouped correctly the moment it is named."""
    from cms_gui.cyclegraph import family_of

    assert family_of("git.commit") == "git"
    assert family_of("git.checkout") == "git"
    assert family_of("agent.implement") == "agent"
    assert family_of("") == ""
    assert family_of(None) == ""


def test_the_families_that_have_a_colour_get_different_ones():
    from cms_gui.cyclegraph import tint_of

    families = ("agent.review", "git.commit", "jira.issues", "memory.recall",
                "service.start", "scenario.run", "report.html")
    assert len({tint_of(one) for one in families}) == len(families)


def test_two_plugins_of_one_family_look_the_same():
    from cms_gui.cyclegraph import tint_of

    assert tint_of("git.commit") == tint_of("git.checkout")
    assert tint_of("agent.edit") == tint_of("agent.implement")


def test_the_two_gates_are_one_colour_because_they_are_one_idea():
    """One asks a person, one asks the facts; both are the run deciding."""
    from cms_gui.cyclegraph import tint_of

    assert tint_of("approval.gate") == tint_of("check.gate")


def test_a_family_nobody_has_coloured_gets_the_ordinary_background():
    """Which is the right answer for a plugin that arrived after the palette."""
    from cms_gui.cyclegraph import ink, tint_of

    assert tint_of("something.new") == ink("NEUTRAL100")
    assert tint_of("") == ink("NEUTRAL100")


def test_no_family_is_coloured_like_a_verdict():
    """The trap this palette exists to avoid: a node tinted green for "this is
    the commit" reads as one that has already succeeded, before the run has
    even started."""
    from cms_gui import theme

    verdicts = {_rgb(theme.OK), _rgb(theme.WARN), _rgb(theme.BAD)}
    for family, colour in theme.PLUGIN_TINT.items():
        assert _rgb(colour) not in verdicts, family


def test_every_family_colour_is_a_wash_rather_than_a_signal(qapp):
    """Far enough from the page to group a graph, nowhere near far enough to
    be read as a status. Measured against the node background it replaces."""
    from cms_gui import theme
    from cms_gui.cyclegraph import ink

    for dark in (False, True):
        theme.set_dark_mode(dark)
        plain = _rgb(ink("NEUTRAL100"))
        for family, colour in theme.PLUGIN_TINT.items():
            distance = max(abs(one - two)
                           for one, two in zip(_rgb(colour), plain))
            assert 2 <= distance <= 40, (family, dark, distance)
    theme.set_dark_mode(False)


def test_the_palette_survives_the_lights_going_out(qapp):
    """set_dark_mode rewrites the module's globals, and a colour captured at
    import would be the light one for the life of the process."""
    from cms_gui import theme
    from cms_gui.cyclegraph import tint_of

    theme.set_dark_mode(False)
    light = tint_of("agent.review")
    theme.set_dark_mode(True)
    dark = tint_of("agent.review")
    theme.set_dark_mode(False)

    assert light != dark
    assert set(theme.PLUGIN_TINT) == set(_families_in_light())


def _families_in_light():
    from cms_gui import theme

    theme.set_dark_mode(False)
    return dict(theme.PLUGIN_TINT)


def _rgb(value):
    from PySide6.QtGui import QColor

    colour = QColor(value)
    return (colour.red(), colour.green(), colour.blue())


def test_the_plugin_id_stays_readable_on_every_family_colour(qapp):
    """It is small, it sits on the node's own tint rather than on the page,
    and it is the channel somebody reads when they cannot tell two of the
    colours apart - so it is the one line here that has to clear the bar."""
    from cms_gui import theme
    from cms_gui.cyclegraph import ink

    for dark in (False, True):
        theme.set_dark_mode(dark)
        grounds = list(theme.PLUGIN_TINT.values()) + [ink("NEUTRAL100")]
        for ground in grounds:
            assert _contrast(ink("NEUTRAL700"), ground) >= 4.5, (ground, dark)
    theme.set_dark_mode(False)


def _contrast(one, two):
    """WCAG contrast ratio, so "readable" is a number rather than an opinion."""
    def luminance(value):
        colour = _rgb(value)
        channels = []
        for raw in colour:
            share = raw / 255.0
            channels.append(share / 12.92 if share <= 0.03928
                            else ((share + 0.055) / 1.055) ** 2.4)
        return (0.2126 * channels[0] + 0.7152 * channels[1]
                + 0.0722 * channels[2])

    lighter, darker = sorted((luminance(one), luminance(two)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_a_lit_line_is_visibly_heavier_and_a_different_colour(qapp):
    """Two channels, because a difference of half a pixel is no difference at
    all at the size a long cycle opens at."""
    from cms_gui import theme
    from cms_gui.cyclegraph import ink

    assert SPEC["edge"]["lit_width"] > SPEC["edge"]["width"] * 1.5
    assert ink("ACCENT") != ink("NEUTRAL500")


def test_edges_sit_below_every_node_lit_or_not():
    """A line over a node's writing is a line in the way, and the highlight
    must not be a reason for one to climb over."""
    from cms_gui.cyclegraph import EDGE_LIT_Z, EDGE_Z

    assert EDGE_Z < EDGE_LIT_Z < 0
