"""Drawing a cycle as nodes and the connections between them.

The one place in this application where something is drawn rather than styled.
Everything else is a Qt widget wearing the stylesheet; a graph is not a widget
arrangement, so this is a ``QGraphicsView`` with items that paint themselves.

**The whole look lives in :data:`SPEC`.** Not scattered through ``paint()``, and
not read from the stylesheet - a redesign changes values in one dictionary and
nothing else moves. That matters more here than elsewhere: this is a first cut
at a visual language the application has never had, and the next version of it
should cost an afternoon rather than a rewrite. Three things keep that true:

* **Layout, painting and data are three separate things.** :func:`layout_graph`
  is a pure function of the payload and the spec - no Qt, no widgets, testable
  on its own. The items read only :data:`SPEC` and ``theme``. The payload comes
  from the core and knows nothing about pixels.
* **Colour is never captured at import.** ``theme.set_dark_mode`` rewrites those
  module globals in place, so every colour is read inside ``paint()`` and
  :meth:`CycleCanvas.restyle` is called when the palette moves.
* **Nothing is typed that should be drawn.** No arrow characters, no bullets:
  ``gui/tests/test_theme.py`` fails the build on any non-comment character above
  U+2100, because those render as boxes in DejaVu and as ASCII stand-ins on
  Windows. Arrowheads are a ``QPainterPath``; status marks come from
  ``icons.DRAWINGS``.

The payload is ``cycle.model.to_graph`` exactly as the core produced it. Each
node carries a ``layer`` (how far from a step with no dependencies) and a
``row`` (its place within that layer), both computed in the core so the
arrangement is the same for any front-end and can be tested without a
QApplication. This module turns that grid into pixels and does nothing else to
it.
"""

import contextlib
import math
import re

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QPainter,
                           QPainterPath, QPainterPathStroker, QPen, QPolygonF)
from PySide6.QtWidgets import (QApplication, QGraphicsItem, QGraphicsObject,
                               QGraphicsPathItem, QGraphicsPolygonItem,
                               QGraphicsScene, QGraphicsView, QToolTip)

from . import icons, theme, widgets

#: Everything the canvas looks like, in one place. A redesign changes values
#: here; it does not change layout_graph, the items, or the page. Colours are
#: not here - they are read from ``theme`` at paint time, because the palette
#: moves when dark mode is set.
SPEC = {
    "node": {"w": 208, "h": 76, "radius": 8, "border": 1, "border_selected": 2,
             "status_bar": 3, "pad_x": 12, "pad_y": 10, "badge": 13,
             # The dots a link is dragged out of while a cycle is edited: the
             # radius drawn, and how far off it a press still takes one - a
             # five-pixel target is a target missed.
             "port": 5, "port_reach": 10},
    # The graph runs **down** the page, the way the workflow diagrams it is
    # drawn from are. A cycle of any size is mostly a sequence - a twenty-step
    # chain is an ordinary shape - and twenty nodes across is a line nobody can
    # read, while twenty down is a list, which is a thing people read all day.
    # Where a cycle does fork, the branches spread sideways and the fork is
    # the widest thing on the page, which is what you want to notice.
    #
    # `layer_gap` is the distance between one layer and the next, and is what
    # the edges are drawn in: too little and a branch has further to travel
    # than it has room to turn in, so a pair of links reads as a chevron.
    # `sibling_gap` separates two nodes of the same layer, across the flow.
    "grid": {"flow": "down", "layer_gap": 62, "sibling_gap": 36, "margin": 48},
    "edge": {"width": 1.6, "arrow": 9,
             # How far a line leaves its node before it is allowed to turn.
             # Without it a corner can land right on the node's edge, where it
             # reads as part of the box rather than as the line arriving at it.
             "stub": 18,
             # The head sits this far off the node's edge, so it points at the
             # node rather than touching it.
             "gap": 3,
             # Between one corridor and the next, for the edges that have to
             # travel around the graph rather than through it. Wide enough that
             # two of them read as two lines at the zoom a cycle is opened at.
             "lane_gap": 16,
             # How heavy a line is drawn when its node is selected. Enough to
             # pick out at the size a long cycle opens at, where a difference
             # of half a pixel is no difference at all.
             "lit_width": 3.2,
             # How wide a line is to the pointer - a right-click on it - which
             # is far wider than it is drawn, or nobody would ever hit one.
             "hit": 10,
             # None is a solid line. A dash pattern is in pen units.
             "dash": {"dependency": None, "success": None, "failure": (4, 3),
                      # A link into a step with an `if:` - the step may not
                      # run, and a broken line says so before it is read.
                      "condition": (5, 3)},
             # The condition written on such a link: its text size, and how
             # far its box stands off the words.
             "label": 10, "label_pad": 4},
    "text": {"title": 12, "plugin": 10, "meta": 10},
    # `grid` is the scale a cycle opens at and the scale the ruled paper starts
    # being drawn at - one number on purpose. The paper appearing is what tells
    # a reader they are at the size the canvas is meant to be read at, and a
    # default zoom that drifted away from it would either open onto blank
    # ground or be a size nothing else in the view agrees with. The Fit button
    # ignores it: somebody who pressed it asked to see all of it, however small
    # that has to be.
    "zoom": {"min": 0.25, "max": 2.5, "step": 1.15, "grid": 0.45},
    # The surface the graph sits on: a dot grid, and how much empty room there
    # is around the graph. The room is what makes the canvas draggable when
    # everything already fits - a scene the exact size of its contents has
    # nowhere to scroll to, so the surface would be nailed down precisely when
    # the cycle is small enough to want moving about freely. A whole viewport
    # on each side means any node can be dragged to the middle of the screen.
    # The ground the graph sits on: the ruled paper the splash screen is drawn
    # over, so opening the application and opening a cycle are the same surface.
    # Square and square on. The grid in the artwork runs at an angle because the
    # whole scene there is drawn in projection - the paper itself is not
    # skewed, and copying the angle onto a canvas seen head on would be copying
    # an artefact of the drawing rather than the thing it draws.
    "canvas": {"grid": 14, "line": 1.0,
               # Every so many squares the line is drawn heavier, which is what
               # makes ruled paper readable rather than a flat texture: it is
               # what lets somebody measure a distance across it by eye.
               "major": 5,
               "roam": 1.0, "roam_min": 400},
}

#: Status -> (icon name, colour token name). The mark and the colour say the
#: same thing twice on purpose: colour alone is not something everyone can read.
#: The colour is a *name* looked up in ``theme`` at paint time, never a value.
STATUS_LOOK = {
    "success": ("pass", "OK"),
    "failed": ("fail", "BAD"),
    "timeout": ("fail", "WARN"),
    "running": ("running", "ACCENT"),
    # Live, like running, and deliberately the same family: the run is healthy
    # and this step is still its business. Dimmer, because the difference worth
    # seeing at a glance is working against parked.
    "waiting": ("running", "ACCENT400"),
    "skipped": ("pending", "NEUTRAL500"),
    "cancelled": ("pending", "NEUTRAL500"),
    "pending": ("pending", "NEUTRAL400"),
}

#: What a status is called on a node. Short: the node is 208px wide.
#: Room on a node's plugin line for Network, Files and Limits (or SYSTEM).
RESTRICTION_WIDTH = 76

STATUS_WORD = {
    "success": "passed", "failed": "failed", "timeout": "timed out",
    "running": "running", "waiting": "waiting", "skipped": "skipped",
    "cancelled": "stopped", "pending": "",
}


def family_of(plugin_id):
    """The family a plugin belongs to: the part of its id before the dot.

    ``git.commit`` and ``git.checkout`` are both ``git``. The naming convention
    is already the grouping, so this needs no table of its own and a plugin
    added later is grouped correctly the moment it is named.
    """
    return str(plugin_id or "").split(".", 1)[0]


def tint_of(plugin_id):
    """The wash behind a node, by what kind of step it is.

    Read at paint time like every other colour here, and falling back to the
    ordinary node background for a family nobody has chosen a colour for -
    which is the right answer for a plugin that arrived after this palette.
    """
    found = theme.PLUGIN_TINT.get(family_of(plugin_id))
    return found or ink("NEUTRAL100")


def ink(name):
    """One colour by name, read now rather than when this module was imported.

    ``theme.set_dark_mode`` rewrites the module's globals in place, so a colour
    captured at import time is the light one for the life of the process.
    """
    if name.startswith("NEUTRAL"):
        return theme.NEUTRAL[int(name[len("NEUTRAL"):])]
    if name.startswith("ACCENT") and name[len("ACCENT"):].isdigit():
        # "ACCENT400", the same spelling the neutrals use. Needed for the
        # states that are live but quiet: the same family as running, dimmer,
        # so a glance separates a step that is working from one that is parked.
        return theme.ACCENT_RAMP[int(name[len("ACCENT"):])]
    return getattr(theme, name)


# -- conditions, in words ------------------------------------------------------
#: A step's output or status as a condition reads it.
_READ = re.compile(r"\$\{\s*steps\.([A-Za-z0-9_-]+)\.(status|outputs\.([A-Za-z0-9_.-]+))\s*\}")
#: ``<reference> == 'value'`` or ``!=``, or the reference alone - the grammar
#: cycle/conditions.py accepts. Quotes around the reference are allowed.
_COMPARED = re.compile(
    r"^\s*[\"']?(\$\{[^}]*\})[\"']?\s*(?:(==|!=)\s*([\"'])(.*)\3)?\s*$")

#: Outputs whose plain truth reads better as words than as their name.
_SAID = {"changed": "if files changed", "count": "if any found",
         "approved": "if approved", "passed": "if passed", "ok": "if ok",
         "found": "if found", "blocking_count": "if problems found",
         "fixable_count": "if something to fix",
         "issue_count": "if issues found"}

#: The longest a condition is written on a link. The rest is in its tooltip.
LABEL_CHARS = 28


def condition_reads(condition):
    """The step ids a condition reads, in order."""
    found = []
    for step_id, _what, _output in _READ.findall(str(condition or "")):
        if step_id not in found:
            found.append(step_id)
    return found


def describe_condition(condition, plugin=""):
    """A step's ``if:`` as the few words written on the link into it.

    Said from the side of the step it reads - the link comes out of that one -
    so ``${steps.fix.outputs.changed}`` is "if files changed" and
    ``${steps.gate.status} != 'failed'`` is "unless it failed", or "unless
    rejected" when the step is an approval, whose failing is somebody saying
    no. Anything it does not recognise is written as it is, shortened.
    """
    text = str(condition or "").strip()
    if not text:
        return ""
    matched = _COMPARED.match(text)
    read = _READ.fullmatch(matched.group(1)) if matched else None
    if read is None:
        alone = matched.group(1) if matched and matched.group(2) is None else ""
        if re.fullmatch(r"\$\{\s*run\.[A-Za-z0-9_.]+\s*\}", alone):
            # A run always has an id: this is how a step says it runs whatever
            # became of the step before it.
            return "either way"
        variable = re.fullmatch(r"\$\{\s*vars\.([A-Za-z0-9_-]+)\s*\}", alone)
        if variable:
            return "if %s is set" % variable.group(1)
        short = text if len(text) <= LABEL_CHARS - 3 else text[:LABEL_CHARS - 4] + "..."
        return "if " + short
    what, output = read.group(2), read.group(3) or ""
    operator, value = matched.group(2), matched.group(4)
    gate = plugin.startswith("approval")
    if what == "status":
        if operator is None:
            return "if it ran"
        wording = {("==", "success"): "if approved" if gate else "if it succeeded",
                   ("!=", "success"): "unless approved" if gate else "unless it succeeded",
                   ("==", "failed"): "if rejected" if gate else "if it failed",
                   ("!=", "failed"): "unless rejected" if gate else "unless it failed",
                   ("==", "skipped"): "if it was skipped",
                   ("!=", "skipped"): "if it ran"}
        return wording.get((operator, value),
                           "if %s %s" % ("status is" if operator == "==" else
                                         "status is not", value))
    name = output.split(".")[-1]
    if operator is None:
        return _SAID.get(name, "if " + name.replace("_", " "))
    said = "if %s is %s%s" % (name.replace("_", " "),
                              "" if operator == "==" else "not ", value)
    return said if len(said) <= LABEL_CHARS else said[:LABEL_CHARS - 3] + "..."


# -- layout -------------------------------------------------------------------
def layout_graph(nodes, spec=None):
    """``{node id: (x, y)}`` for every node. Deterministic, and free of Qt.

    ``layer`` is how far along the flow a node is and ``row`` is its place
    among the others of that layer - both computed by the core, so the
    arrangement is the same wherever it is drawn. Which direction the flow runs
    is ``spec["grid"]["flow"]``: ``down`` the page by default, ``right`` across
    it. Everything below is written in those terms rather than in x and y, so
    the two directions are one piece of arithmetic rather than two that can
    disagree.

    Each layer is centred across the widest one, so a fork looks like a fork
    rather than like everything hanging off one side.

    Pure on purpose: this is the part worth being careful about, and it can be
    tested without a QApplication, a widget, or a screen.
    """
    spec = spec or SPEC
    down = spec["grid"].get("flow", "down") == "down"
    # How much room one node takes along the flow, and across it.
    along = spec["node"]["h"] if down else spec["node"]["w"]
    across = spec["node"]["w"] if down else spec["node"]["h"]
    layer_gap = spec["grid"]["layer_gap"]
    sibling_gap = spec["grid"]["sibling_gap"]
    margin = spec["grid"]["margin"]

    layers_ = {}
    for node in nodes:
        layers_.setdefault(int(node.get("layer", 0)), []).append(node)
    if not layers_:
        return {}

    widest = max(len(one) for one in layers_.values())
    full = widest * across + (widest - 1) * sibling_gap

    places = {}
    for layer, members in layers_.items():
        ordered = sorted(members, key=lambda one: int(one.get("row", 0)))
        span = len(ordered) * across + (len(ordered) - 1) * sibling_gap
        start = margin + (full - span) / 2.0
        for index, node in enumerate(ordered):
            here = margin + layer * (along + layer_gap)
            beside = start + index * (across + sibling_gap)
            places[node["id"]] = (beside, here) if down else (here, beside)
    return places


def place_beside(waits_for, placed, spec=None):
    """Where a step nobody has placed goes, in a cycle somebody arranged.

    The computed layout is about the whole graph, and a cycle whose boxes were
    dragged about is not that graph any more: a new step put where the layout
    says landed wherever its layer happened to be, away from the step it was
    added after and often off the screen - and moved again with every step
    added after it, since a step never dragged has no place of its own.

    So it goes one row on from the first step it waits for that has a place -
    under it when the flow runs down - and along the row past whatever is
    already there. A step that waits for nothing goes one row on from the
    furthest along. ``placed`` is ``{step id: (x, y)}``; pure, like
    :func:`layout_graph`.
    """
    spec = spec or SPEC
    down = spec["grid"].get("flow", "down") == "down"
    width, height = spec["node"]["w"], spec["node"]["h"]
    along = height if down else width
    across = width if down else height
    layer_gap, sibling_gap = spec["grid"]["layer_gap"], spec["grid"]["sibling_gap"]
    if not placed:
        margin = spec["grid"]["margin"]
        return (margin, margin)

    def split(point):
        x, y = point
        return (y, x) if down else (x, y)          # (along, across)

    anchor = next((placed[one] for one in waits_for or () if one in placed), None)
    if anchor is None:
        anchor = max(placed.values(), key=lambda one: split(one)[0])
    first, side = split(anchor)
    first += along + layer_gap

    def free(at_along, at_across):
        for other in placed.values():
            o_along, o_across = split(other)
            if (abs(o_along - at_along) < along + layer_gap / 2.0
                    and abs(o_across - at_across) < across + sibling_gap / 2.0):
                return False
        return True

    while not free(first, side):
        side += across + sibling_gap
    return (side, first) if down else (first, side)


def scene_size(places, spec=None):
    """How big the scene has to be to hold ``places``, including the margin."""
    spec = spec or SPEC
    if not places:
        return (0.0, 0.0)
    margin = spec["grid"]["margin"]
    right = max(x for x, _y in places.values()) + spec["node"]["w"] + margin
    bottom = max(y for _x, y in places.values()) + spec["node"]["h"] + margin
    return (right, bottom)


# -- phases -------------------------------------------------------------------
# A long cycle is mostly a sequence, and twenty-eight boxes down the page is a
# list nobody can take in at once. A step may name the phase it belongs to
# (`group:` in the file); the canvas frames each phase's steps together and can
# fold a phase into one box. Folding is a way of looking at the graph and
# nothing else: the core's layers are for the whole graph, so a folded view is
# arranged here, by the same longest-path rule the core uses.

#: A folded phase's node id. A prefix no step id can carry - a colon is not
#: allowed in one - so the two can never collide.
GROUP_PREFIX = "group:"

#: What a folded phase's box says it is doing, from what its steps are doing.
#: Earlier entries win: a phase with one step running and one failed is
#: failed, because that is the thing somebody watching has to act on.
_PHASE_ORDER = ("failed", "timeout", "running", "waiting", "cancelled")


def groups_of(nodes):
    """``{phase: [step ids]}`` in the order the steps are written."""
    found = {}
    for node in nodes or ():
        name = (node.get("group") or "").strip()
        if name:
            found.setdefault(name, []).append(node["id"])
    return found


def phase_status(statuses):
    """One status for a phase, from its steps' statuses."""
    statuses = [one or "pending" for one in statuses]
    if not statuses:
        return "pending"
    for one in _PHASE_ORDER:
        if one in statuses:
            return one
    if all(one == "success" for one in statuses):
        return "success"
    if all(one in ("skipped", "cancelled") for one in statuses):
        return "skipped"
    if "success" in statuses:
        return "running" if "pending" in statuses else "success"
    return "pending"


def fold_graph(graph, folded):
    """``(graph, refused)``: ``graph`` with each phase in ``folded`` as one node.

    Pure, like :func:`layout_graph`. A phase is folded only if drawing it as
    one box keeps the graph a graph: when a path leaves the phase and comes
    back into it, one box would have to wait for itself, and that phase is
    left open and named in ``refused`` instead.
    """
    nodes = list((graph or {}).get("nodes") or [])
    phases = groups_of(nodes)
    owner = {}
    refused = []
    for name in [one for one in phases if one in set(folded or ())]:
        trial = dict(owner, **{step: GROUP_PREFIX + name for step in phases[name]})
        if _acyclic(nodes, trial):
            owner = trial
        else:
            refused.append(name)
    if not owner:
        return graph, refused

    order, merged = [], {}
    for node in nodes:
        into = owner.get(node["id"])
        if into is None:
            order.append(dict(node, needs=_mapped(node.get("needs"), owner, node["id"])))
            continue
        if into not in merged:
            members = [one["id"] for one in nodes if owner.get(one["id"]) == into]
            merged[into] = {
                "id": into, "label": into[len(GROUP_PREFIX):], "plugin": "",
                "group": into[len(GROUP_PREFIX):], "members": members,
                "needs": [], "disabled": all(one.get("disabled") for one in nodes
                                             if one["id"] in members),
                "condition": "", "timeout": None, "retry": 1,
                "on_failure": "stop"}
            order.append(merged[into])
        merged[into]["needs"] = _mapped(
            list(merged[into]["needs"]) + list(node.get("needs") or []), owner, into)

    depth = {}

    def walk(node_id, seen=()):
        if node_id in depth:
            return depth[node_id]
        node = next(one for one in order if one["id"] == node_id)
        needs = [walk(need, seen + (node_id,)) for need in node["needs"]
                 if need not in seen and any(one["id"] == need for one in order)]
        depth[node_id] = 1 + max(needs) if needs else 0
        return depth[node_id]

    rows = {}
    for node in order:
        node["layer"] = walk(node["id"])
        node["row"] = rows.get(node["layer"], 0)
        rows[node["layer"]] = node["row"] + 1
    edges = [{"from": need, "to": node["id"], "kind": "dependency"}
             for node in order for need in node["needs"]]
    return dict(graph, nodes=order, edges=edges), refused


def fold_blocker(graph, name):
    """Why phase ``name`` cannot be one box, in a sentence; "" when it can.

    The reason is a path that leaves the phase and comes back into it - the
    one box would have to wait for itself. It is named by the steps' labels,
    because that is what the canvas shows, and the phases it passes through
    are named first, because that is what somebody reading it has to move.
    """
    nodes = _steps_only((graph or {}).get("nodes"))
    members = set(groups_of(nodes).get(name) or ())
    if not members:
        return ""
    by_id = {node["id"]: node for node in nodes}
    after = {}
    for node in nodes:
        for need in node.get("needs") or ():
            after.setdefault(need, []).append(node["id"])
    for node in nodes:
        if node["id"] not in members:
            continue
        # Breadth first from each step's way out of the phase, through steps
        # outside it only, until a way back in: the shortest such detour.
        queue = [[node["id"], out] for out in after.get(node["id"], ())
                 if out not in members]
        seen = set(one[-1] for one in queue)
        while queue:
            path = queue.pop(0)
            for nxt in after.get(path[-1], ()):
                if nxt in members:
                    return _blocker_text(path + [nxt], by_id)
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(path + [nxt])
    return ""


def _blocker_text(path, by_id):
    between = []
    for step in path[1:-1]:
        phase = (by_id[step].get("group") or "").strip()
        word = phase or "steps outside any phase"
        if word not in between:
            between.append(word)
    labels = [by_id[step].get("label") or step for step in path]
    if len(labels) > 5:
        labels = labels[:2] + ["..."] + labels[-2:]
    return ("%s %s between its steps (%s), so as one box it would wait for "
            "itself. To fold it, put the steps after %s in a phase of their own."
            % (", ".join(between), "runs" if len(between) == 1 else "run",
               " -> ".join(labels), " and ".join(between)))


def _steps_only(nodes):
    """The nodes that are steps - not a folded phase standing in for some."""
    return [node for node in nodes or () if not str(node.get("id", "")).startswith(GROUP_PREFIX)]


def _mapped(needs, owner, itself):
    """``needs`` with every folded step replaced by its phase, once each."""
    found = []
    for need in needs or ():
        need = owner.get(need, need)
        if need != itself and need not in found:
            found.append(need)
    return found


def _acyclic(nodes, owner):
    """Whether the graph, with ``owner``'s steps merged, has no loop."""
    after = {}
    for node in nodes:
        into = owner.get(node["id"], node["id"])
        for need in node.get("needs") or ():
            need = owner.get(need, need)
            if need != into:
                after.setdefault(need, set()).add(into)
    state = {}

    def visit(one):
        state[one] = 1
        for nxt in after.get(one, ()):
            if state.get(nxt) == 1 or (state.get(nxt) is None and not visit(nxt)):
                return False
        state[one] = 2
        return True

    return all(state.get(one) == 2 or visit(one) for one in list(after))


def problems_by_step(problems, step_ids):
    """``({step id: [messages]}, [the rest])`` - the core says ``id: what``."""
    known = set(step_ids or ())
    mine, rest = {}, []
    for message in problems or ():
        text = str(message)
        head, _sep, _tail = text.partition(":")
        if head in known:
            mine.setdefault(head, []).append(text)
        else:
            rest.append(text)
    return mine, rest


# -- items --------------------------------------------------------------------
class CycleNodeItem(QGraphicsObject):
    """One step: what it is, what it runs, and how it went.

    Its status is set, never rebuilt. A run touches one node at a time and the
    scene must not be reconstructed to show that - forty nodes flickering on
    every event is the difference between a live view and an unusable one.
    """

    def __init__(self, node, spec=None):
        QGraphicsObject.__init__(self)
        self._spec = spec or SPEC
        self.node = dict(node)
        self.step_id = node["id"]
        self._status = "pending"
        self._duration_ms = None
        self._attempts = 0
        #: What the cycle's restrictions hold this step to, as the page worked
        #: it out: {"network": (shown, state), "files": (shown, state),
        #: "limits": text, "system": bool, "pending": set}. None draws nothing -
        #: a cycle without a project folder looks exactly as it always did.
        self.restriction = None
        #: What the core says is wrong with this step, as its own lines.
        self._problems = []
        self.edges = []              # every edge with this node at either end
        #: What to call when this node moves. The canvas sets it, because a
        #: move changes the routing of edges that are nothing to do with this
        #: node and only the canvas can see those.
        self.on_moved = None
        self._pressed_at = QPointF()
        self._dragging = False
        #: Whether links can be dragged out of this step - a cycle being
        #: edited. The dots show while the pointer is over it or it is selected.
        self._ports = False
        self._hover = False
        #: While a link is dragged over this step: "ok" if it can be dropped
        #: here, "no" if it would make a loop, "" otherwise.
        self._drop = ""
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        # Movable, because a computed layout is a starting point rather than an
        # answer: on a real cycle there is always one node somebody wants a
        # little out of the way to read the rest. Arrange puts them all back.
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setCursor(Qt.OpenHandCursor)
        self.setAcceptHoverEvents(True)
        self.setToolTip(self._tooltip())

    def set_node(self, node):
        """Take a fresh payload for the same step. Its status is not touched.

        What a node says can change without the picture changing shape - a
        condition added, a step disabled - and the canvas takes those without
        rebuilding, which is what keeps a live run's statuses on screen.
        """
        self.node = dict(node)
        self.setToolTip(self._tooltip())
        self.update()

    def itemChange(self, change, value):
        """Take this node's edges with it as it moves.

        Through the canvas when it is there, because moving one node changes
        what the *other* edges have to go around: drag a step into the middle
        of the column and every line that used to pass through empty space now
        has an obstacle. Its own edges only, when it is not - an item built
        without a canvas still has to draw.
        """
        if change == QGraphicsItem.ItemPositionHasChanged:
            if self.on_moved is not None:
                self.on_moved()
            else:
                for edge in self.edges:
                    edge.reroute()
        return QGraphicsObject.itemChange(self, change, value)

    # -- moving it ------------------------------------------------------------
    # Qt starts moving a movable item on the first pixel of mouse movement, so
    # a click with a hand tremor in it shifts the node - and a few clicks turn a
    # laid-out graph into a scattered one for no reason anybody can see. Moving
    # therefore waits until the pointer has travelled the distance the platform
    # itself calls a drag, which is the same threshold every other dragging
    # widget uses.

    def mousePressEvent(self, event):
        self._pressed_at = event.screenPos()
        self._dragging = False
        self.setCursor(Qt.ClosedHandCursor)
        QGraphicsObject.mousePressEvent(self, event)

    def mouseMoveEvent(self, event):
        if not self._dragging:
            travelled = (event.screenPos() - self._pressed_at).manhattanLength()
            if travelled < QApplication.startDragDistance():
                event.accept()
                return                    # still a click, not yet a drag
            self._dragging = True
        QGraphicsObject.mouseMoveEvent(self, event)

    def mouseReleaseEvent(self, event):
        self.setCursor(Qt.OpenHandCursor)
        self._dragging = False
        QGraphicsObject.mouseReleaseEvent(self, event)

    def moved_by_hand(self):
        """True while this press has turned into an actual drag."""
        return self._dragging

    # -- state ----------------------------------------------------------------
    def set_status(self, status, duration_ms=None, attempts=0):
        """O(1), and one repaint. Called for every event of a live run."""
        self._status = status or "pending"
        if duration_ms is not None:
            self._duration_ms = duration_ms
        if attempts:
            self._attempts = attempts
        self.setToolTip(self._tooltip())
        self.update()

    def clear_status(self):
        self._status = "pending"
        self._duration_ms = None
        self._attempts = 0
        self.setToolTip(self._tooltip())
        self.update()

    @property
    def status(self):
        return self._status

    @property
    def is_phase(self):
        """True for a folded phase standing in for its steps."""
        return self.step_id.startswith(GROUP_PREFIX)

    def set_problems(self, problems):
        """What is wrong with this step. Drawn as a mark; read in the tooltip."""
        self._problems = list(problems or ())
        self.setToolTip(self._tooltip())
        self.update()

    # -- linking --------------------------------------------------------------
    # Two dots, one on the side a step's links leave by and one on the side
    # they arrive at. Dragging the bottom one onto another step makes that step
    # wait for this one; dragging the top one onto another step makes this one
    # wait for that. The canvas does the dragging - see CycleCanvas.
    def set_ports(self, on):
        on = bool(on) and not self.is_phase
        if on == self._ports:
            return
        self.prepareGeometryChange()          # the shape takes the dots in
        self._ports = on
        self.setToolTip(self._tooltip())
        self.update()

    @property
    def has_ports(self):
        return self._ports

    def ports_shown(self):
        return self._ports and (self._hover or self.isSelected() or bool(self._drop))

    def port_point(self, which):
        """The middle of the ``"out"`` (bottom) or ``"in"`` (top) dot."""
        node = self._spec["node"]
        return QPointF(node["w"] / 2.0, node["h"] if which == "out" else 0.0)

    def port_at(self, point):
        """Which dot ``point`` (in this item's terms) takes, or ""."""
        if not self._ports:
            return ""
        reach = self._spec["node"]["port_reach"]
        for which in ("out", "in"):
            centre = self.port_point(which)
            if (abs(point.x() - centre.x()) <= reach
                    and abs(point.y() - centre.y()) <= reach):
                return which
        return ""

    def set_drop(self, drop):
        if drop != self._drop:
            self._drop = drop
            self.update()

    def hoverEnterEvent(self, event):
        self._hover = True
        self.update()
        QGraphicsObject.hoverEnterEvent(self, event)

    def hoverMoveEvent(self, event):
        # The dots say what dragging them does; the rest of the step says what
        # the step is. One tooltip per item, so it is swapped as the pointer
        # moves between them.
        port = self.port_at(event.pos())
        tip = (PORT_TIP[port] if port else self._tooltip())
        if tip != self.toolTip():
            self.setToolTip(tip)
        QGraphicsObject.hoverMoveEvent(self, event)

    def hoverLeaveEvent(self, event):
        self._hover = False
        self.setToolTip(self._tooltip())
        self.update()
        QGraphicsObject.hoverLeaveEvent(self, event)

    # -- painting -------------------------------------------------------------
    def boundingRect(self):
        node = self._spec["node"]
        # Half the border on each side, so a selected node's thicker outline is
        # not clipped by its own rectangle - and a folded phase's second card,
        # which sits a few pixels off the first - and the link dots, which sit
        # across the top and bottom edges.
        bleed = max(node["border_selected"] + (STACK if self.is_phase else 0),
                    node["port_reach"])
        return QRectF(-bleed, -bleed, node["w"] + bleed * 2,
                      node["h"] + bleed * 2)

    def shape(self):
        """The card, and the dots while links can be drawn from them.

        Not the bounding rect: that is padded for the dots, and a press just
        under a step that is not being edited belongs to the ground.
        """
        node = self._spec["node"]
        bleed = node["border_selected"] + (STACK if self.is_phase else 0)
        path = QPainterPath()
        # Winding, so where a dot overlaps the card is still inside rather
        # than a hole - the default rule makes overlaps holes.
        path.setFillRule(Qt.WindingFill)
        path.addRect(QRectF(-bleed / 2.0, -bleed / 2.0, node["w"] + bleed,
                            node["h"] + bleed))
        if self._ports:
            reach = node["port_reach"]
            for which in ("out", "in"):
                centre = self.port_point(which)
                path.addRect(QRectF(centre.x() - reach, centre.y() - reach,
                                    reach * 2, reach * 2))
        return path

    def paint(self, painter, option, widget=None):
        spec = self._spec["node"]
        text = self._spec["text"]
        painter.setRenderHint(QPainter.Antialiasing, True)

        body = QRectF(0, 0, spec["w"], spec["h"])
        disabled = bool(self.node.get("disabled"))
        # A disabled step is not going to run, so what it *would* have been is
        # the least interesting thing about it: it gets the flat grey and says
        # "disabled" where the status word goes.
        fill = QColor(ink("NEUTRAL200") if disabled
                      else tint_of(self.node.get("plugin", "")))
        # A link dragged over it says first whether it can land here.
        edge = QColor(ink("ACCENT" if self._drop == "ok"
                          else "BAD" if self._drop == "no"
                          else "ACCENT" if self.isSelected()
                          else "BAD" if self._problems else "DIVIDER"))
        heavy = bool(self._drop) or self.isSelected() or bool(self._problems)

        path = QPainterPath()
        path.addRoundedRect(body, spec["radius"], spec["radius"])
        if self.is_phase:
            # A card behind the card: several steps under one box, which is
            # what folding a phase is, said without a word.
            behind = QPainterPath()
            behind.addRoundedRect(body.translated(STACK, STACK), spec["radius"],
                                  spec["radius"])
            painter.setBrush(QBrush(QColor(ink("NEUTRAL200"))))
            painter.setPen(QPen(QColor(ink("DIVIDER"))))
            painter.drawPath(behind)
        painter.setBrush(QBrush(fill))
        pen = QPen(edge)
        pen.setWidthF(spec["border_selected"] if heavy else spec["border"])
        painter.setPen(pen)
        painter.drawPath(path)

        self._paint_status_bar(painter, body, path)
        self._paint_text(painter, spec, text, disabled)
        self._paint_badges(painter, spec)
        if self.restriction is not None and not self.is_phase:
            self._paint_restriction(painter)
        if self.ports_shown():
            self._paint_ports(painter, spec)

    def _paint_ports(self, painter, spec):
        """The two dots a link is dragged out of: hollow in, filled out."""
        pen = QPen(QColor(ink("ACCENT")))
        pen.setWidthF(1.5)
        painter.setPen(pen)
        radius = spec["port"]
        for which in ("in", "out"):
            painter.setBrush(QBrush(QColor(ink("ACCENT" if which == "out"
                                               else "CANVAS_BG"))))
            painter.drawEllipse(self.port_point(which), radius, radius)

    def _paint_status_bar(self, painter, body, path):
        """The stripe down the left edge, clipped to the rounded corner."""
        _mark, colour = STATUS_LOOK.get(self._status, STATUS_LOOK["pending"])
        if self._status == "pending":
            return
        painter.save()
        painter.setClipPath(path)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(ink(colour))))
        painter.drawRect(QRectF(body.left(), body.top(),
                                self._spec["node"]["status_bar"],
                                body.height()))
        painter.restore()

    def _paint_text(self, painter, spec, text, disabled):
        left = spec["status_bar"] + spec["pad_x"]
        width = spec["w"] - left - spec["pad_x"]

        title = QFont(theme.FONT_BODY[0])
        title.setPixelSize(text["title"])
        title.setBold(True)
        painter.setFont(title)
        painter.setPen(QPen(QColor(ink("NEUTRAL500" if disabled else "TEXT"))))
        painter.drawText(QRectF(left, spec["pad_y"], width, text["title"] + 4),
                         Qt.AlignLeft | Qt.AlignVCenter,
                         _elide(painter, ("! " if self.node.get("important") else "") + (self.node.get("label") or self.step_id),
                                width))

        plugin = QFont(theme.FONT_MONO[0])
        plugin.setPixelSize(text["plugin"])
        painter.setFont(plugin)
        # A step deeper than the rest of the muted text in the application,
        # because this line sits on the node's own tint rather than on the page
        # and it is small. At NEUTRAL600 it measured 3.9 against the plain
        # background and 3.6 against the washes - under the 4.5 that small text
        # wants; this clears it at 5.5. It also matters more than most muted
        # text: it is the channel somebody reads when they cannot tell two of
        # the family colours apart.
        painter.setPen(QPen(QColor(ink("NEUTRAL700"))))
        members = self.node.get("members") or []
        second = (self.node.get("plugin") or "" if not self.is_phase
                  else self.node.get("cause_summary") if self.node.get("cause_summary")
                  else "phase - %d step%s" % (len(members),
                                              "" if len(members) == 1 else "s"))
        painter.drawText(
            QRectF(left, spec["pad_y"] + text["title"] + 6, width,
                   text["plugin"] + 4),
            Qt.AlignLeft | Qt.AlignVCenter, _elide(painter, second, width - (RESTRICTION_WIDTH if self.restriction is not None and not self.is_phase else 0)))

        painter.setFont(QFont(theme.FONT_BODY[0], -1))
        meta = QFont(theme.FONT_BODY[0])
        meta.setPixelSize(text["meta"])
        painter.setFont(meta)
        word = STATUS_WORD.get(self._status, "")
        if disabled and self._status == "pending":
            word = "disabled"
        if word:
            _mark, colour = STATUS_LOOK.get(self._status, STATUS_LOOK["pending"])
            painter.setPen(QPen(QColor(ink(colour))))
            painter.drawText(
                QRectF(left, spec["h"] - spec["pad_y"] - text["meta"] - 2,
                       width, text["meta"] + 4),
                Qt.AlignLeft | Qt.AlignVCenter, word)
        if self._duration_ms:
            painter.setPen(QPen(QColor(ink("NEUTRAL600"))))
            painter.drawText(
                QRectF(left, spec["h"] - spec["pad_y"] - text["meta"] - 2,
                       width, text["meta"] + 4),
                Qt.AlignRight | Qt.AlignVCenter, _duration(self._duration_ms))

    def _paint_badges(self, painter, spec):
        """Small marks along the top right: trouble, retry, a condition."""
        size = spec["badge"]
        x = spec["w"] - spec["pad_x"] - size
        y = spec["pad_y"]
        for name, colour in self._badges():
            # As lines, not a pixmap: the canvas is scaled, and a pixmap
            # resampled with it comes out soft.
            icons.paint(painter, name, QRectF(x, y, size, size), ink(colour))
            x -= size + 4

    def _badges(self):
        found = []
        if self._problems:
            found.append(("warn", "BAD"))
        if self.is_phase:
            found.append(("unfold", "NEUTRAL500"))
            return found
        if self.node.get("condition"):
            found.append(("group", "NEUTRAL500"))
        if int(self.node.get("retry") or 1) > 1 or self._attempts > 1:
            found.append(("refresh", "NEUTRAL500"))
        return found

    def _tooltip(self):
        """Everything that will not fit on the node itself."""
        if self.is_phase:
            rows = ["Phase %s: %s" % (self.node.get("label") or "",
                                      ", ".join(self.node.get("members") or [])),
                    "Double-click to open it back out."]
            if self._status != "pending":
                rows.append("status: %s" % self._status)
            if self.node.get("cause_summary"):
                rows.extend([self.node["cause_summary"], self.node.get("cause_detail", "")])
            return "\n".join(rows + self._problems)
        rows = ["%s  (%s)" % (self.node.get("label") or self.step_id,
                              self.node.get("plugin") or "")]
        if self.restriction is not None:
            if self.restriction.get("system"):
                rows.append("System step: runs without the cycle's restrictions.")
            else:
                rows.append("Restrictions: " + self.restriction.get("summary", ""))
                rows.append("Network and Files on the node switch that access off, or back on to what was set; "
                            "Limits opens the numbers." if self.restriction.get("editable") else
                            "Restrictions are read-only here.")
                if self.restriction.get("pending"):
                    rows.append("Not saved yet: Save applies it to the next run.")
        if self.node.get("important"):
            rows.append("Important: " + (self.node.get("importance_reason") or "Approval required in Important mode"))
        if self.node.get("group"):
            rows.append("phase: %s" % self.node["group"])
        if self.node.get("needs"):
            rows.append("after: %s" % ", ".join(self.node["needs"]))
        if self.node.get("condition"):
            rows.append("if: %s" % self.node["condition"])
        if self.node.get("timeout"):
            rows.append("timeout: %ss" % _clean(self.node["timeout"]))
        if int(self.node.get("retry") or 1) > 1:
            rows.append("tries: %s" % self.node["retry"])
        if self.node.get("on_failure") == "continue":
            rows.append("on failure: carry on")
        if self.node.get("disabled"):
            rows.append("disabled")
        if self._status != "pending":
            rows.append("status: %s" % self._status)
        if self._ports:
            rows.append("Drag a dot on its edge to another step to link them.")
        return "\n".join(rows + self._problems)

    def restriction_rect(self, kind):
        """Where one indicator sits: right of the plugin line, in a fixed order."""
        spec, text = self._spec["node"], self._spec["text"]
        left = spec["w"] - spec["pad_x"] - RESTRICTION_WIDTH
        offsets = {"network": (0, 20), "files": (22, 32), "limits": (56, 20), "system": (0, RESTRICTION_WIDTH)}
        start, width = offsets[kind]
        return QRectF(left + start, spec["pad_y"] + text["title"] + 5, width, 20)

    def restriction_at(self, point):
        if self.restriction is None or self.is_phase or self.restriction.get("system"):
            return ""
        return next((kind for kind in ("network", "files", "limits")
                     if self.restriction_rect(kind).contains(point)), "")

    def _paint_restriction(self, painter):
        """Network, Files and Limits as they are now; a tag on a system step.

        State is a mark and a word, never a colour alone: a slashed globe for
        no network, the folder's access in letters, the gauge drawn in the
        accent only when there is a limit to read. A change not saved yet is
        underlined, on that indicator only.
        """
        state = self.restriction
        font = QFont(theme.FONT_BODY[0])
        font.setPixelSize(10)
        font.setBold(True)
        painter.setFont(font)
        if state.get("system"):
            rect = self.restriction_rect("system")
            painter.setPen(QPen(QColor(ink("NEUTRAL500")), 1, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(rect.adjusted(0.5, 2.5, -0.5, -2.5))
            painter.setPen(QPen(QColor(ink("NEUTRAL600"))))
            painter.drawText(rect, Qt.AlignCenter, "SYSTEM")
            return
        pending = state.get("pending") or set()
        shown, mode = state["network"]
        rect = self.restriction_rect("network")
        colour = ink("WARN" if mode == "off" else "NEUTRAL500" if mode == "locked" else "NEUTRAL700")
        icons.paint(painter, {"Off": "network-off", "Isolated": "network-isolated"}.get(shown, "network"),
                    rect.adjusted(2, 2, -2, -2), colour)
        shown, mode = state["files"]
        rect = self.restriction_rect("files")
        colour = ink("WARN" if mode == "off" else "NEUTRAL500" if mode == "locked" else "NEUTRAL700")
        icons.paint(painter, "folder-off" if shown == "Off" else "folder", QRectF(rect.left() + 1, rect.top() + 3, 14, 14), colour)
        painter.setPen(QPen(QColor(colour)))
        painter.drawText(QRectF(rect.left() + 16, rect.top(), rect.width() - 16, rect.height()),
                         Qt.AlignLeft | Qt.AlignVCenter, shown if shown != "All" else "")
        rect = self.restriction_rect("limits")
        icons.paint(painter, "limits", rect.adjusted(2, 2, -2, -2),
                    ink("ACCENT" if state.get("limits") else "NEUTRAL500"))
        if pending:
            painter.setPen(QPen(QColor(ink("ACCENT")), 1, Qt.DashLine))
            for kind in pending:
                if kind in ("network", "files", "limits"):
                    rect = self.restriction_rect(kind)
                    painter.drawLine(QPointF(rect.left() + 1, rect.bottom()), QPointF(rect.right() - 1, rect.bottom()))


class CycleEdgeItem(QGraphicsPathItem):
    """One dependency: a line out of one node and into the next, with a head.

    An edge is a meaningful object rather than decoration - ``kind`` is what it
    means, and the dash pattern is how that reads without opening the YAML.
    Today there is one kind; the mechanism is here so the second costs nothing.

    It holds the two **items**, not their positions, so a dragged node takes its
    edges with it: :meth:`reroute` asks them where they are now.

    The head is kept apart from the line and filled separately. Putting it in
    the same path meant the pen stroked the triangle as well as filling it,
    which thickened it into a blob at the very point somebody is trying to read
    the direction from.
    """

    def __init__(self, source_item, target_item, kind="dependency", spec=None):
        QGraphicsPathItem.__init__(self)
        self._spec = spec or SPEC
        self.source_item = source_item
        self.target_item = target_item
        self.source = getattr(source_item, "step_id", "")
        self.target = getattr(target_item, "step_id", "")
        self.kind = kind
        self._head = QPolygonF()
        #: How this edge is drawn among the ones beside it, decided by the
        #: canvas - which is the only thing that can see them all. Where it
        #: attaches at each end, and the corridor to go around in, if it needs
        #: one. Left alone, an edge draws the way it always did.
        self._out_slot = (0, 1)
        self._into_slot = (0, 1)
        self._lane = None
        #: Drawn heavier, because the node it belongs to is selected.
        self._lit = False
        #: Where the line starts and where its head points - the two ends a
        #: selected link is re-pointed by.
        self._start = QPointF()
        self._tip = QPointF()
        #: The target's condition in words, written on the line, when the
        #: target has one and this is the link it reads - see set_condition.
        self.condition = ""
        self._condition_full = ""
        self.setZValue(EDGE_Z)      # under the nodes, always
        if not self.is_phase_link:
            self.setToolTip("%s waits for %s" % (self.target, self.source))
        self.reroute()

    @property
    def is_phase_link(self):
        """True for a line to or from a folded phase - several links at once."""
        return (self.source.startswith(GROUP_PREFIX)
                or self.target.startswith(GROUP_PREFIX))

    def set_editable(self, on):
        """Whether a click selects it, to be re-pointed or deleted."""
        on = bool(on) and not self.is_phase_link
        if not on and self.isSelected():
            self.setSelected(False)
        self.setFlag(QGraphicsItem.ItemIsSelectable, on)
        if not self.is_phase_link:
            said = "%s waits for %s" % (self.target, self.source)
            self.setToolTip(said + ("\nClick it, then drag an end to another "
                                    "step, or press Delete." if on else ""))

    def set_condition(self, label, condition=""):
        """Draw this link broken, with ``label`` on it: the step it leads to
        runs only when ``condition`` holds."""
        self.prepareGeometryChange()
        self.condition, self._condition_full = label or "", condition or ""
        if self.condition and not self.is_phase_link:
            self.setToolTip("%s runs only %s.\nif: %s\n%s waits for %s" % (
                self.target, self.condition, self._condition_full,
                self.target, self.source))
        self.update()

    def _label_font(self):
        font = QFont(theme.FONT_BODY[0])
        font.setPixelSize(self._spec["edge"].get("label", 10))
        return font

    def label_rect(self):
        """Where the condition's box is drawn: on the middle of the line."""
        if not self.condition or self.path().isEmpty():
            return QRectF()
        pad = self._spec["edge"].get("label_pad", 4)
        metrics = QFontMetricsF(self._label_font())
        width = metrics.horizontalAdvance(self.condition) + pad * 2
        height = metrics.height() + pad
        middle = self.path().pointAtPercent(0.5)
        return QRectF(middle.x() - width / 2.0, middle.y() - height / 2.0,
                      width, height)

    def end_at(self, point):
        """Which end of a selected link ``point`` takes - "from", "to" or ""."""
        if not self.isSelected():
            return ""
        reach = self._spec["node"]["port_reach"]
        for which, centre in (("to", self._tip), ("from", self._start)):
            if (abs(point.x() - centre.x()) <= reach
                    and abs(point.y() - centre.y()) <= reach):
                return which
        return ""

    def shape(self):
        """The line as wide as a pointer needs, its head, and - selected - the
        two ends it is dragged by."""
        stroker = QPainterPathStroker()
        stroker.setWidth(self._spec["edge"]["hit"])
        found = stroker.createStroke(self.path())
        found.addPolygon(self._head)
        if self.condition:
            found.addRect(self.label_rect())
        if self.isSelected():
            reach = self._spec["node"]["port_reach"]
            for centre in (self._start, self._tip):
                found.addRect(QRectF(centre.x() - reach, centre.y() - reach,
                                     reach * 2, reach * 2))
        found.setFillRule(Qt.WindingFill)
        return found

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemSelectedChange:
            self.prepareGeometryChange()        # the ends join its shape
        elif change == QGraphicsItem.ItemSelectedHasChanged:
            self._restack()
        return QGraphicsPathItem.itemChange(self, change, value)

    def _restack(self):
        # A selected link is lifted over the nodes as well, so the ends it is
        # dragged by are on top where they overlap a step's own dots.
        self.setZValue(EDGE_SELECTED_Z if self.isSelected()
                       else EDGE_LIT_Z if self._lit else EDGE_Z)

    def set_lit(self, lit):
        """Draw this one heavier, or stop. Cheap: one repaint, no re-routing.

        A lit edge is also lifted above the other edges - still under every
        node, but over the lines it crosses. Without that, following a link
        across a busy graph means losing it under the next one.
        """
        lit = bool(lit)
        if lit == self._lit:
            return
        self._lit = lit
        self._restack()
        self.update()

    def set_routing(self, out_slot=(0, 1), into_slot=(0, 1), lane=None):
        """Take the canvas's decision about where this one goes, and redraw."""
        self._out_slot, self._into_slot, self._lane = out_slot, into_slot, lane
        self.reroute()

    def sides(self):
        """Which side it leaves by and arrives at, from where the nodes are."""
        if self.source_item is None or self.target_item is None:
            return SIDES["bottom"], SIDES["top"]
        return _sides(self.source_item.pos(), self.target_item.pos(),
                      self._spec["node"])

    def reroute(self):
        """Rebuild the line from where the two nodes are now.

        Which sides it uses is decided here rather than fixed, because a node
        can be dragged anywhere. An edge that always left to the right and
        arrived from the left drew a loop out and back the moment somebody
        stacked two steps vertically - which is a layout people reach for
        immediately, and it made the graph unreadable exactly then.
        """
        node = self._spec["node"]
        edge = self._spec["edge"]
        if self.source_item is None or self.target_item is None:
            self.setPath(QPainterPath())
            return

        out, into = self.sides()
        start = _anchor(self.source_item.pos(), node, out, *self._out_slot)
        # The head sits off the node's edge, pointing along the way in.
        tip = _anchor(self.target_item.pos(), node, into, *self._into_slot)
        tip = QPointF(tip.x() + into[0] * edge["gap"],
                      tip.y() + into[1] * edge["gap"])
        # The line stops short of the tip so the head is drawn on clear ground
        # rather than on top of the line it belongs to.
        back = edge["arrow"] * 0.8
        end = QPointF(tip.x() + into[0] * back, tip.y() + into[1] * back)

        if self._lane is None:
            points = _elbow(start, end, out, edge["stub"])
        else:
            points = _detour(start, end, out, edge["stub"], self._lane)
        path = QPainterPath(start)
        for point in points:
            path.lineTo(point)
        self.prepareGeometryChange()
        self.setPath(path)
        self._head = _arrow_head(tip, edge["arrow"], into)
        self._start, self._tip = start, tip

    def boundingRect(self):
        # The head lives outside the path, so the rect has to cover both or the
        # tip is clipped away as the view scrolls. The margin is half the
        # heaviest pen this edge can be drawn with, plus one: a path's own rect
        # is the line through its middle and knows nothing about how thick it
        # is, so a fixed margin would clip a lit edge along its length. Or the
        # ends a selected link is dragged by, if those reach further.
        edge = self._spec["edge"]
        margin = max(max(edge["width"], edge["lit_width"]) / 2.0 + 1.0,
                     self._spec["node"]["port_reach"] + 1.0)
        return QGraphicsPathItem.boundingRect(self).united(
            self._head.boundingRect()).united(self.label_rect()).adjusted(
                -margin, -margin, margin, margin)

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing, True)
        edge = self._spec["edge"]
        lit = self._lit or self.isSelected()
        colour = QColor(ink("ACCENT" if lit else "NEUTRAL500"))
        pen = QPen(colour)
        pen.setWidthF(edge["lit_width"] if lit else edge["width"])
        pen.setCapStyle(Qt.RoundCap)
        dash = self._spec["edge"]["dash"].get(
            "condition" if self.condition else self.kind)
        if dash:
            pen.setDashPattern(list(dash))

        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(self.path())

        # Filled, never stroked: an outlined triangle at this size reads as a
        # smudge rather than as a direction.
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(colour))
        painter.drawPolygon(self._head)

        if self.condition:
            # On the paper's own ground, so the line stops at the words rather
            # than running through them, and in the line's colour, so the two
            # read as one thing.
            box = self.label_rect()
            border = QPen(colour)
            border.setWidthF(1.0)
            painter.setPen(border)
            painter.setBrush(QBrush(QColor(ink("CANVAS_BG"))))
            painter.drawRect(box)
            painter.setFont(self._label_font())
            painter.setPen(QColor(ink("ACCENT" if lit else "NEUTRAL700")))
            painter.drawText(box, Qt.AlignCenter, self.condition)

        if self.isSelected():
            # The two ends it can be dragged by: hollow, like a step's own
            # dots, so what can be picked up looks the same everywhere.
            ring = QPen(colour)
            ring.setWidthF(1.5)
            painter.setPen(ring)
            painter.setBrush(QBrush(QColor(ink("CANVAS_BG"))))
            radius = self._spec["node"]["port"]
            for centre in (self._start, self._tip):
                painter.drawEllipse(centre, radius, radius)


#: The four sides of a node, as the outward direction of each. An edge leaves
#: and arrives along one of these, so there is no diagonal case to reason about
#: and a line always meets a node square on.
SIDES = {"right": (1.0, 0.0), "left": (-1.0, 0.0),
         "bottom": (0.0, 1.0), "top": (0.0, -1.0)}

#: Where edges sit against the nodes. Both are below zero, which is where a
#: node sits, because a line over a node's writing is a line in the way. A lit
#: edge is lifted above the other *edges* only - enough to follow it across a
#: busy graph without losing it under the next one.
EDGE_Z = -2.0
EDGE_LIT_Z = -1.0
#: The one link somebody has clicked, while a cycle is edited: over the nodes,
#: because its two ends are handles and a handle under a step cannot be taken.
EDGE_SELECTED_Z = 1.0
#: A phase's frame is the ground its steps and their links stand on.
FRAME_Z = -3.0

#: How far a folded phase's second card sits off the first.
STACK = 4

#: What each link dot does, said where the pointer is.
PORT_TIP = {
    "out": "Drag onto another step: that step will wait for this one.",
    "in": "Drag onto another step: this step will wait for that one.",
}
#: A link being drawn: over the nodes, under nothing.
LINK_Z = 5.0

#: A phase frame: the band its name sits in, and the room round its steps.
FRAME = {"header": 22, "pad": 12, "radius": 10, "label": 11}


def frame_islands(rects, pad, header, gap):
    """The ground a phase is drawn on: ``[(band rects, heading rect)]``.

    Pure, like the layout. ``rects`` are the phase's steps, ``(x, y, w, h)``.
    Steps side by side make a band; bands one layer apart are joined where
    they overlap across; bands further apart - a phase with one step early on
    and the rest at the end - are separate islands, each with its own heading.
    A bounding box would have been one line of code and would have drawn one
    phase over three others, and over any step of another phase that happened
    to share one of its layers.
    """
    bands = {}
    for x, y, w, h in rects:
        key = round(y)
        left, top, right, bottom = bands.get(key, (x, y, x + w, y + h))
        bands[key] = (min(left, x), min(top, y), max(right, x + w), max(bottom, y + h))
    islands, current, last = [], [], None
    for key in sorted(bands):
        left, top, right, bottom = bands[key]
        joined = (last is not None and top - last[3] <= gap * 1.5
                  and min(right, last[2]) > max(left, last[0]))
        if current and not joined:
            islands.append(current)
            current = []
        if joined:
            # The link between two bands: as wide as they both are.
            current.append((max(left, last[0]) - pad, last[3], min(right, last[2]) + pad,
                            top))
        current.append((left - pad, top - pad, right + pad, bottom + pad))
        last = (left, top, right, bottom)
    if current:
        islands.append(current)
    found = []
    for island in islands:
        first = island[0]
        heading = (first[0], first[1] - header, first[2], first[1])
        found.append((island + [heading], heading))
    return found


class CycleGroupFrame(QGraphicsObject):
    """One phase's steps, framed together, its name in a band across the top.

    Only the band takes a click - it folds the phase - so a press anywhere
    else inside the frame still reaches the canvas and pans it, the way empty
    ground does everywhere else.
    """

    def __init__(self, name, spec=None):
        QGraphicsObject.__init__(self)
        self._spec = spec or SPEC
        self.name = name
        self._path = QPainterPath()
        self._headings = []
        self._count = 0
        self._state = "pending"
        self._current = False
        #: Why the phase cannot fold into one box - see fold_blocker - or "".
        self._blocked = ""
        #: Called with the phase's name when its band is clicked.
        self.on_fold = None
        self.setZValue(FRAME_Z)
        self.setAcceptedMouseButtons(Qt.LeftButton)
        self.setCursor(Qt.PointingHandCursor)

    def place(self, islands, count):
        """Take the ground :func:`frame_islands` worked out, and redraw."""
        self.prepareGeometryChange()
        path = QPainterPath()
        path.setFillRule(Qt.WindingFill)
        self._headings = []
        for pieces, heading in islands:
            for left, top, right, bottom in pieces:
                path.addRoundedRect(QRectF(left, top, right - left, bottom - top),
                                    FRAME["radius"], FRAME["radius"])
            self._headings.append(QRectF(heading[0], heading[1],
                                         heading[2] - heading[0],
                                         heading[3] - heading[1]))
        self._path = path.simplified()
        self._count = count
        self._say()
        self.update()

    def set_blocked(self, reason):
        """Say the phase cannot fold, and why - or, with "", that it can.

        A band that took the click and did nothing was the whole complaint:
        some phases folded and some did not, with no way to tell which or why.
        """
        reason = reason or ""
        if reason != self._blocked:
            self._blocked = reason
            self.setCursor(Qt.ArrowCursor if reason else Qt.PointingHandCursor)
            self._say()
            self.update()

    def blocked(self):
        return self._blocked

    def _say(self):
        head = "Phase %s - %d step%s." % (self.name, self._count,
                                          "" if self._count == 1 else "s")
        self.setToolTip("%s It does not fold into one box: %s" % (head, self._blocked)
                        if self._blocked else
                        "%s Click the band to fold it into one box." % head)

    def set_state(self, state, current):
        """How the phase is going, and whether the run is in it now."""
        if (state, current) != (self._state, self._current):
            self._state, self._current = state, current
            self.update()

    def band(self):
        """The first heading: where the phase's name is read from."""
        return self._headings[0] if self._headings else QRectF()

    def boundingRect(self):
        return self._path.boundingRect().adjusted(-2, -2, 2, 2)

    def shape(self):
        path = QPainterPath()
        for heading in self._headings:
            path.addRect(heading)
        return path

    def paint(self, painter, option, widget=None):
        if self._path.isEmpty():
            return
        painter.setRenderHint(QPainter.Antialiasing, True)
        edge = QColor(ink("ACCENT" if self._current else "NEUTRAL400"))
        pen = QPen(edge)
        pen.setWidthF(2.0 if self._current else 1.2)
        painter.setPen(pen)
        # Lighter than the paper, so a phase reads as a sheet laid on it -
        # and a little see-through, so the ruling still shows it is paper.
        ground = QColor(ink("NEUTRAL100"))
        ground.setAlpha(210)
        painter.setBrush(QBrush(ground))
        painter.drawPath(self._path)

        font = QFont(theme.FONT_BODY[0])
        font.setPixelSize(FRAME["label"])
        font.setBold(True)
        painter.setFont(font)
        word = STATUS_WORD.get(self._state, "")
        label = "%s  -  %d step%s%s" % (self.name.upper(), self._count,
                                        "" if self._count == 1 else "s",
                                        ("  -  " + word) if word else "")
        for heading in self._headings:
            band = heading.adjusted(10, 0, -10, 0)
            painter.setPen(QPen(QColor(ink("ACCENT" if self._current
                                           else "NEUTRAL700"))))
            # Half the band at most: the link into the phase's first step
            # comes down the middle, and a label under it would be crossed out.
            room = max(40.0, band.width() / 2.0 + 30)
            painter.drawText(QRectF(band.left(), band.top(), room, band.height()),
                             Qt.AlignLeft | Qt.AlignVCenter,
                             _elide(painter, label, room))
            if self._blocked:
                continue      # no fold mark on a band a click does not fold
            size = 12
            icons.paint(painter, "fold", QRectF(band.right() - size,
                                                band.center().y() - size / 2.0,
                                                size, size), ink("NEUTRAL600"))

    def mousePressEvent(self, event):
        if (any(one.contains(event.pos()) for one in self._headings)
                and self.on_fold is not None and not self._blocked):
            event.accept()
            self.on_fold(self.name)
            return
        event.ignore()


def _sides(source, target, node):
    """Which side the edge leaves by, and which side it arrives at.

    The axis the two nodes are furthest apart on wins. That is what keeps an
    edge short and straight however somebody has arranged them: side by side it
    goes right to left, stacked it goes bottom to top, and it never crosses the
    node it is leaving.
    """
    dx = (target.x() - source.x())
    dy = (target.y() - source.y())
    if abs(dx) * node["h"] >= abs(dy) * node["w"]:
        # Scaled by the node's own proportions: a node is much wider than it is
        # tall, so "further apart across than down" is not the same question as
        # "a bigger number".
        return (SIDES["right"], SIDES["left"]) if dx >= 0 else \
               (SIDES["left"], SIDES["right"])
    return (SIDES["bottom"], SIDES["top"]) if dy >= 0 else \
           (SIDES["top"], SIDES["bottom"])


def _elbow(start, end, out, stub):
    """The points from ``start`` to ``end``, turning only at right angles.

    Three segments at most: out of the node, across, and in. The turn happens
    halfway between the two stubs, so a fork's branches bend on the same line
    and read as one splitting rather than as several unrelated lines.

    Straight through when the two are already lined up: the middle points
    collapse onto the ends and ``lineTo`` a point it is already at draws
    nothing, so the degenerate case needs no branch of its own.
    """
    first = QPointF(start.x() + out[0] * stub, start.y() + out[1] * stub)
    last = QPointF(end.x() - out[0] * stub, end.y() - out[1] * stub)
    if out[0]:                                  # leaving by a side: turn on x
        middle = (first.x() + last.x()) / 2.0
        return [first, QPointF(middle, first.y()), QPointF(middle, last.y()),
                last, end]
    middle = (first.y() + last.y()) / 2.0       # leaving by top or bottom
    return [first, QPointF(first.x(), middle), QPointF(last.x(), middle),
            last, end]


def _anchor(pos, node, side, slot=0, of=1):
    """The point on a node where an edge meets it, for one side.

    ``slot`` of ``of`` spreads the edges that share a side across it instead of
    stacking them all on its middle. Three lines leaving one node from the
    same pixel are one line as far as a reader is concerned; spread, they are
    visibly three. ``of`` being 1 puts the only edge back on the middle, which
    is what a node with one link should look like.
    """
    across = (slot + 1.0) / (of + 1.0)
    if side[0]:                          # left or right: spread down the side
        return QPointF(pos.x() + node["w"] * (0.5 + side[0] / 2.0),
                       pos.y() + node["h"] * across)
    return QPointF(pos.x() + node["w"] * across,
                   pos.y() + node["h"] * (0.5 + side[1] / 2.0))


def _detour(start, end, out, stub, lane):
    """A route that goes out to ``lane``, along it, and back in.

    For an edge whose direct line would pass through the nodes between its two
    ends - which in a column layout is every edge that skips a layer, and there
    are usually more of those than there are short ones. Drawn straight, they
    run down the same pixels as the short links and as each other, and the
    whole column reads as one thick line.

    ``lane`` is the coordinate of the corridor to travel in: an x when the edge
    leaves by the top or the bottom, a y when it leaves by a side.
    """
    if out[1]:                           # leaving top or bottom: lane is an x
        first = QPointF(start.x(), start.y() + out[1] * stub)
        last = QPointF(end.x(), end.y() - out[1] * stub)
        return [first, QPointF(lane, first.y()), QPointF(lane, last.y()),
                last, end]
    first = QPointF(start.x() + out[0] * stub, start.y())
    last = QPointF(end.x() - out[0] * stub, end.y())
    return [first, QPointF(first.x(), lane), QPointF(last.x(), lane),
            last, end]


def _points_of(path):
    """A QPainterPath back as the points it turns at.

    As QPointF rather than as the path's own elements, whose x and y are plain
    attributes - so everything downstream reads one kind of point.
    """
    return [QPointF(path.elementAt(index).x, path.elementAt(index).y)
            for index in range(path.elementCount())]


def _crosses(points, rects):
    """Whether an axis-aligned polyline passes through any of ``rects``.

    Only the segments are tested, not the ends: an edge always touches the two
    nodes it joins, and those are not obstacles to it.
    """
    for index in range(len(points) - 1):
        one, two = points[index], points[index + 1]
        low = QPointF(min(one.x(), two.x()), min(one.y(), two.y()))
        high = QPointF(max(one.x(), two.x()), max(one.y(), two.y()))
        for rect in rects:
            if (low.x() <= rect.right() and high.x() >= rect.left()
                    and low.y() <= rect.bottom() and high.y() >= rect.top()):
                return True
    return False


def _arrow_head(tip, size, into=(-1.0, 0.0)):
    """A triangle at ``tip``, pointing against ``into``. Drawn, never typed."""
    base = QPointF(tip.x() + into[0] * size, tip.y() + into[1] * size)
    wing = (into[1] * size * 0.5, -into[0] * size * 0.5)
    return QPolygonF([QPointF(tip.x(), tip.y()),
                      QPointF(base.x() + wing[0], base.y() + wing[1]),
                      QPointF(base.x() - wing[0], base.y() - wing[1])])


def _rule(painter, rect, canvas, colour, major_colour):
    """Rule ``rect`` into squares, every ``major``-th line drawn heavier.

    Lines are numbered from the scene's own origin rather than from a corner of
    the view, so a given line keeps its number however far the canvas has been
    panned - which is what stops the heavy ones sliding about as you scroll.
    """
    step = canvas["grid"]
    pen, major_pen = QPen(colour), QPen(major_colour)
    for one in (pen, major_pen):
        one.setWidthF(canvas["line"])
        one.setCosmetic(True)         # one pixel at every zoom, never thicker

    for horizontal in (False, True):
        low, high = ((rect.top(), rect.bottom()) if horizontal
                     else (rect.left(), rect.right()))
        first = int(math.floor(low / step))
        last = int(math.ceil(high / step))
        if last - first > 4000:       # zoomed so far out the lines would merge
            continue
        for index in range(first, last + 1):
            at = index * step
            painter.setPen(major_pen if index % canvas["major"] == 0 else pen)
            if horizontal:
                painter.drawLine(QPointF(rect.left(), at),
                                 QPointF(rect.right(), at))
            else:
                painter.drawLine(QPointF(at, rect.top()),
                                 QPointF(at, rect.bottom()))


# -- the canvas ---------------------------------------------------------------
class CycleCanvas(QGraphicsView):
    """The whole cycle at once: every node, every connection, live status.

    One scene per cycle, built once by :meth:`set_graph`. After that a run only
    ever calls :meth:`set_status`, which repaints a single item.
    """

    node_selected = Signal(str)          # step id, or "" when nothing is
    node_opened = Signal(str)            # double-clicked: open it for editing
    #: A drag finished, or Arrange put everything back. Carries nothing: the
    #: page asks for :meth:`places` when it wants them, so a listener that only
    #: enables a button does not pay for a dictionary it will not read.
    nodes_moved = Signal()
    #: The zoom or the scroll moved. Unlike ``nodes_moved`` this fires while a
    #: hand is still moving - a wheel emits several a second - so whatever
    #: listens must wait for it to stop before writing anything down.
    view_changed = Signal()
    #: Fit put the view back, however it was asked for - the button, a
    #: double-click on empty ground, Arrange. Whatever remembered where
    #: somebody had been looking should forget it: Fit is the way back, and a
    #: remembered view surviving it would undo it on the next open.
    view_reset = Signal()
    #: A phase was folded or opened; ask :meth:`folded` which are folded now.
    folds_changed = Signal()
    #: Right-click on a step: (step id, where on screen). The page decides
    #: what can be done to it - the canvas draws, it does not edit.
    node_menu = Signal(str, QPoint)
    #: A click on a step's Network, Files or Limits indicator: (step id,
    #: "network" | "files" | "limits", where on screen).
    restriction_clicked = Signal(str, str, QPoint)
    #: Right-click on empty ground: where on screen.
    canvas_menu = Signal(QPoint)
    #: Right-click on a link: (the step waited for, the step waiting, where).
    edge_menu = Signal(str, str, QPoint)
    #: A link was drawn by dragging a dot: (the step waited for, the step
    #: that should wait for it). Only ever one :meth:`set_linking` allowed.
    link_made = Signal(str, str)
    #: One end of a selected link was dragged to another step: (old waited
    #: for, old waiting, new waited for, new waiting).
    link_moved = Signal(str, str, str, str)
    #: Delete on a selected link: (waited for, waiting).
    link_removed = Signal(str, str)

    def __init__(self, parent=None, spec=None):
        QGraphicsView.__init__(self, parent)
        self._spec = spec or SPEC
        self._nodes = {}
        self._edges = []
        self._graph = None
        #: The graph as drawn: ``_graph`` with the folded phases folded.
        self._drawn = None
        #: Which phases are folded into one box each - see fold_graph.
        self._folded = set()
        #: phase name -> its frame, for the phases that are open.
        self._frames = {}
        #: Where nodes were put by hand, kept across folding and opening.
        self._places = {}
        #: step id -> (status, duration, attempts). Kept here as well as on
        #: the items, because folding rebuilds the items and a run's colours
        #: must survive somebody tidying the view.
        self._statuses = {}
        self._run_evidence = {}
        #: step id -> its restriction indicators (see set_restrictions).
        self._restrictions = {}
        #: step id -> what the core says is wrong with it.
        self._problems = {}
        #: The phases and which one each step is in - read once per graph,
        #: because a run asks on every event it sends.
        self._phases, self._phase_of = {}, {}
        #: True while many nodes are moved at once, so the lines are worked
        #: out once at the end rather than once per node.
        self._batch = False
        #: ``why_not(waited_for, waiting)`` while links may be drawn - "" when
        #: one may - or None. The page owns the answer: the canvas draws.
        self._linking = None
        #: The link being dragged, while one is: see _start_link.
        self._link = None
        self._fitted = False
        self._floor = SPEC["zoom"]["min"]
        #: The view this cycle was opened with, kept until there is a viewport
        #: to honour it against. A cycle can be opened while the page it sits
        #: on has never been shown, and a view applied then would be applied to
        #: nothing.
        self._wanted_view = None
        #: True while the canvas itself is moving the view. Placing a graph
        #: scrolls, and a scroll the canvas made is not somebody looking
        #: somewhere - taken as one, opening a cycle would write down the view
        #: it had just restored.
        self._placing = False
        #: Which placement a resize should repeat while nobody has taken the
        #: view over: how a cycle opens, or Fit.
        self._auto = "default"
        #: The view as the canvas last placed it, so the scroll bars finishing
        #: that placement a turn of the event loop later is recognised for what
        #: it is rather than written down as somebody looking somewhere.
        self._placed = None
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.Antialiasing, True)
        # Dragging empty space pans, which is what a canvas is expected to do.
        # Qt only offers one drag mode at a time, so it is switched per press:
        # on a node, NoDrag lets the item move itself; on empty ground,
        # ScrollHandDrag moves the view. See mousePressEvent.
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setFrameShape(QGraphicsView.NoFrame)
        # True once somebody has zoomed, panned or moved something. After that
        # the view stops re-fitting itself when the panel is resized: a canvas
        # that snaps back to Fit every time the splitter moves is unusable.
        self._touched = False
        self._moved = False
        # Anything outside the scene rect is painted with this rather than with
        # drawBackground, so without it the canvas ends in a visible seam the
        # moment you pan past its edge.
        self.setBackgroundBrush(QColor(ink("CANVAS_BG")))
        self.scene().selectionChanged.connect(self._selection_changed)
        # Panning is the scroll bars moving, whichever gesture moved them -
        # the hand, the wheel, a keypress - so this is the one place that sees
        # all of it. It fires per pixel; the listener is what waits.
        for bar in (self.horizontalScrollBar(), self.verticalScrollBar()):
            bar.valueChanged.connect(self._scrolled)

    # -- content --------------------------------------------------------------
    def set_graph(self, graph, places=None, view=None, folded=()):
        """Build the scene from a ``cycle.model.to_graph`` payload.

        The only thing that rebuilds. Everything a run does afterwards goes
        through :meth:`set_status`.

        ``places`` is where somebody put the nodes last time, by step id. A
        step it does not mention goes where the layout computes - which is what
        makes a remembered arrangement safe to keep after the cycle has grown a
        step, rather than something that has to be thrown away whenever the
        file changes.

        ``view`` is where they were last looking at it from. Without one the
        cycle opens the way every cycle opens - see :meth:`reset_view`.

        ``folded`` names the phases to show folded, each as one box.
        """
        self._folded = set(folded or ())
        self._places = dict(places or {})
        self._statuses, self._problems = {}, {}
        self._run_evidence = {}
        # Building a scene scrolls: items appearing move the scene rect under
        # the view. Held as a placement from end to end so none of that reads
        # as somebody choosing to look somewhere.
        with self._placing_view():
            self._build(graph, self._places, view)

    def _build(self, graph, places, view):
        """The scene itself. Split out only so the placement above wraps it."""
        # Emptied **before** the scene is cleared, not after. Clearing deletes
        # the items, and deleting a selected one emits selectionChanged - which
        # lands in a handler that walks this dictionary. Done the other way
        # round it walks items whose C++ half has just gone, which is a
        # RuntimeError out of a paint path rather than anything a reader of
        # this method would expect.
        self._nodes, self._edges, self._frames = {}, [], {}
        self._link = None                 # its line goes with the scene
        self.scene().clear()
        self._graph = graph or None
        self._fitted = False
        self._placed = None
        self._wanted_view = view if _view_of(view)[0] is not None else None
        if not graph:
            self._drawn = None
            self._phases, self._phase_of = {}, {}
            self.scene().setSceneRect(QRectF(0, 0, 1, 1))
            return

        self._phases = groups_of(graph.get("nodes") or [])
        self._phase_of = {step: name for name, steps in self._phases.items()
                          for step in steps}
        self._drawn, refused = fold_graph(graph, self._folded)
        self._folded -= set(refused)
        nodes = self._drawn.get("nodes") or []
        computed = layout_graph(nodes, self._spec)
        # A folded view is laid out afresh: places somebody dragged steps to
        # are about the open graph, and half of them would land on a phase.
        remembered = {} if self._folded else dict(places or {})

        # In a cycle somebody arranged, a step without a place of its own goes
        # beside the step it waits for (see place_beside), not where the
        # computed layout - which knows nothing of the arrangement - would put
        # it. In layer order, so a step added after a new one lands beside it.
        waits = {}
        for edge in self._drawn.get("edges") or []:
            if edge.get("kind", "dependency") == "dependency":
                waits.setdefault(edge.get("to"), []).append(edge.get("from"))
        positions = dict(remembered)
        self._unplaced = []
        if remembered:
            for node in sorted(nodes, key=lambda one: int(one.get("layer", 0))):
                if node["id"] not in positions:
                    positions[node["id"]] = place_beside(
                        waits.get(node["id"]), positions, self._spec)
                    self._unplaced.append(node["id"])

        for node in nodes:
            item = CycleNodeItem(node, self._spec)
            item.restriction = self._restrictions.get(node.get("id"))
            x, y = positions.get(node["id"],
                                 computed.get(node["id"], (0.0, 0.0)))
            item.setPos(x, y)
            item.on_moved = self._moved_one
            # Folded, the view is for reading; moving boxes about is for the
            # open graph, where where they go can be remembered.
            item.setFlag(QGraphicsItem.ItemIsMovable, not self._folded)
            item.set_ports(self._linking is not None)
            self.scene().addItem(item)
            self._nodes[node["id"]] = item

        for edge in self._drawn.get("edges") or []:
            source = self._nodes.get(edge.get("from"))
            target = self._nodes.get(edge.get("to"))
            if source is None or target is None:
                continue          # an edge to a step that is not in the graph
            item = CycleEdgeItem(source, target,
                                 edge.get("kind", "dependency"), self._spec)
            item.set_editable(self._linking is not None)
            condition = (getattr(target, "node", None) or {}).get("condition")
            if condition:
                # On the link from the step the condition reads - or, when it
                # reads none of the steps this one waits for, on every link
                # in, since then any of them is the way it may not be taken.
                reads = condition_reads(condition)
                incoming = waits.get(edge.get("to")) or []
                if edge.get("from") in reads or not set(reads) & set(incoming):
                    item.set_condition(describe_condition(
                        condition, (getattr(source, "node", None) or {})
                        .get("plugin", "")), condition)
            source.edges.append(item)
            target.edges.append(item)
            self.scene().addItem(item)
            self._edges.append(item)

        for name in groups_of(_steps_only(nodes)):
            frame = CycleGroupFrame(name, self._spec)
            frame.on_fold = self._fold_later
            if fold_graph(graph, self._folded | {name})[1]:
                frame.set_blocked(fold_blocker(graph, name) or (
                    "with the phases folded now, as one box it would wait for "
                    "itself. Open them first."))
            self.scene().addItem(frame)
            self._frames[name] = frame

        self._repaint_statuses()
        self._repaint_problems()
        self.route()
        self._resize_scene()
        # A cycle opened with a remembered arrangement is already "moved", so
        # Arrange offers itself straight away - which is the only way back to
        # the computed layout once one has been kept.
        self._moved = bool(remembered)
        if not self.apply_view(self._wanted_view):
            self.reset_view()

    # -- phases -----------------------------------------------------------------
    def phases(self):
        """``{phase: [step ids]}`` for the whole graph, folded or not."""
        return dict(self._phases)

    def folded(self):
        return sorted(self._folded)

    def set_folded(self, names, anchor=""):
        """Fold exactly these phases. Statuses and problems are carried over.

        The zoom stays what it was. Rebuilding with no view used to reset it,
        so folding one phase fitted the whole cycle into the window - a jump
        away from what somebody had just clicked. ``anchor`` names the phase
        that was clicked: it is kept where it was on the screen, folded or
        opened, so the eye does not have to find it again.
        """
        names = set(names or ()) & set(self.phases())
        if names == self._folded:
            return
        if not self._folded and self._moved:
            # What somebody arranged by hand, so opening the phases again
            # puts the steps back where they had them.
            self._places = self.places()
        statuses, problems = dict(self._statuses), dict(self._problems)
        view = self.view() if self._nodes else None
        before = self._phase_point(anchor)
        on_screen = self.mapFromScene(before) if before is not None else None
        self._folded = names
        with self._placing_view():
            self._build(self._graph, self._places, view)
            after = self._phase_point(anchor)
            if on_screen is not None and after is not None:
                # Move the view by however far the phase moved in the scene,
                # so it lands back under the pointer that clicked it.
                shift = after - self.mapToScene(on_screen)
                centre = self.mapToScene(self.viewport().rect().center())
                self.centerOn(centre + shift)
        self._statuses, self._problems = statuses, problems
        self._repaint_statuses()
        self._repaint_problems()
        self.folds_changed.emit()

    def _phase_point(self, name):
        """Where a phase is read from in the scene: its band, or its box when
        folded. None for no phase, or one not drawn."""
        if not name:
            return None
        frame = self._frames.get(name)
        if frame is not None and not frame.band().isEmpty():
            return frame.mapToScene(frame.band().topLeft())
        box = self._nodes.get(GROUP_PREFIX + name)
        if box is not None:
            return box.pos()
        return None

    def fold(self, name):
        self.set_folded(self._folded | {name}, anchor=name)

    def unfold(self, name):
        self.set_folded(self._folded - {name}, anchor=name)

    @contextlib.contextmanager
    def _moving_many(self):
        """Hold the lines still while several nodes move; route once after.

        Every move of a node re-routes every line, which is right for a hand
        dragging one node and wasteful for Arrange putting back twenty-eight
        - twenty-eight full routings for one picture.
        """
        was, self._batch = self._batch, True
        try:
            yield
        finally:
            self._batch = was

    def _moved_one(self):
        if not self._batch:
            self.route()

    def _fold_later(self, name):
        """Fold from inside an item's own mouse handler, once it has returned.

        Folding rebuilds the scene, and deleting the item whose event is still
        being delivered is how a Qt application falls over.
        """
        QTimer.singleShot(0, lambda: self.fold(name))

    def current_phases(self):
        """The phases the run is in now: a step of theirs running or waiting."""
        found = []
        for name, members in self._phases.items():
            if any((self._statuses.get(one) or ("",))[0] in ("running", "waiting")
                   for one in members):
                found.append(name)
        return found

    def _place_frames(self):
        """Fit each open phase's frame round its steps, wherever they are now."""
        if not self._frames:
            return
        pad, header = FRAME["pad"], FRAME["header"]
        width, height = self._spec["node"]["w"], self._spec["node"]["h"]
        members = groups_of(_steps_only((self._drawn or {}).get("nodes") or []))
        for name, frame in self._frames.items():
            rects = [(item.pos().x(), item.pos().y(), width, height)
                     for item in (self._nodes.get(one) for one in members.get(name) or [])
                     if item is not None]
            if rects:
                frame.place(frame_islands(rects, pad, header,
                                          self._spec["grid"]["layer_gap"]),
                            len(members.get(name) or []))
        self._repaint_frames()

    def _repaint_frames(self, names=None):
        for name in (names if names is not None else list(self._frames)):
            frame = self._frames.get(name)
            if frame is None:
                continue
            states = [(self._statuses.get(one) or ("",))[0]
                      for one in self._phases.get(name, [])]
            frame.set_state(phase_status(states),
                            any(one in ("running", "waiting") for one in states))
            summary, detail = self._phase_causes(name)
            frame._say()
            if summary:
                frame.setToolTip(frame.toolTip() + "\n" + summary + "\n" + detail)

    def set_run_evidence(self, steps):
        """Causal evidence accompanies statuses, without changing graph data."""
        self._run_evidence = steps
        for name in self._folded:
            self._repaint_phase(name)
        self._repaint_frames()

    def set_restrictions(self, states):
        """step id -> what the restrictions hold it to (see CycleNodeItem.restriction).

        Kept here as well as on the items, because folding rebuilds the items
        and the indicators must survive somebody tidying the view.
        """
        self._restrictions = dict(states or {})
        for step_id, item in self._nodes.items():
            item.restriction = self._restrictions.get(step_id)
            item.setToolTip(item._tooltip())
            item.update()

    def _phase_causes(self, name):
        from .cycleactivity import causes
        steps = getattr(self, "_run_evidence", {})
        members = self._phases.get(name, [])
        problems = [one for one in members if steps.get(one, {}).get("status") in ("failed", "timeout")
                    or steps.get(one, {}).get("blocked_by")]
        roots = {causes({"steps": steps}, one)[-1][0] for one in problems if causes({"steps": steps}, one)}
        if not roots:
            return "", ""
        summary = "%d root failure(s) · %d blocked" % (len(roots), sum(bool(steps[one].get("blocked_by")) for one in problems))
        labels = {node["id"]: node.get("label") or node["id"] for node in (self._graph or {}).get("nodes", [])}
        return summary, "\n".join(labels.get(root, root) + ": " + steps[root].get("message", "") for root in sorted(roots))

    # -- problems ---------------------------------------------------------------
    def set_problems(self, problems):
        """Mark the steps the core has something to say about. Returns the rest.

        The core names a step's problem ``id: what is wrong``; those land on
        the step's own box. What names no step - a loop, the subject - is
        handed back for the caller to say somewhere else.
        """
        step_ids = [node.get("id") for node in (self._graph or {}).get("nodes") or []]
        self._problems, rest = problems_by_step(problems, step_ids)
        self._repaint_problems()
        return rest

    def _repaint_problems(self):
        owner = {step: GROUP_PREFIX + name for name, steps in self.phases().items()
                 if name in self._folded for step in steps}
        found = {}
        for step_id, messages in self._problems.items():
            found.setdefault(owner.get(step_id, step_id), []).extend(messages)
        for step_id, item in self._nodes.items():
            item.set_problems(found.get(step_id, []))

    def _repaint_statuses(self):
        for step_id, item in self._nodes.items():
            if item.is_phase:
                continue
            status, duration, attempts = self._statuses.get(step_id,
                                                            ("pending", None, 0))
            item.set_status(status, duration, attempts)
        for name in self._folded:
            self._repaint_phase(name)

    def _repaint_phase(self, name):
        item = self._nodes.get(GROUP_PREFIX + name)
        if item is None:
            return
        summary, detail = self._phase_causes(name)
        item.node["cause_summary"] = summary
        item.node["cause_detail"] = detail
        members = self._phases.get(name) or []
        item.set_status(phase_status([(self._statuses.get(one) or ("",))[0]
                                      for one in members]),
                        sum((self._statuses.get(one) or (None, 0, 0))[1] or 0
                            for one in members) or None)
        item.setToolTip(item._tooltip())

    def update_graph(self, graph):
        """Take a fresh payload of the graph that is already drawn.

        For the case :func:`pages.cycles._same_shape` recognises: the same
        nodes in the same places, read again from the file. What a node says
        can still have moved - a condition added, a step disabled - so each
        item takes the new payload, but the *items* are kept, and with them the
        statuses of a live run, the selection, and wherever somebody had
        dragged and zoomed the canvas to.
        """
        if not graph:
            return
        self._graph = graph
        self._drawn, _refused = fold_graph(graph, self._folded)
        for node in self._drawn.get("nodes") or []:
            item = self._nodes.get(node.get("id"))
            if item is not None:
                item.set_node(node)

    # -- where the lines go ---------------------------------------------------
    def route(self):
        """Decide how every edge is drawn. The canvas's job, not the edge's.

        Two things an edge cannot work out alone. **Where to attach**: several
        edges sharing one side of a node all leave from its middle unless
        somebody counts them, and three lines from one pixel are one line as
        far as a reader is concerned. **Whether to go around**: a line is only
        in the way of a node it does not belong to, and the edge cannot see
        those.

        Cheap enough to run on every pixel of a drag: a cycle has tens of
        nodes, not thousands, and the alternative is edges that settle into
        place only after the mouse is released.
        """
        self._place_frames()
        if not self._edges:
            return
        node = self._spec["node"]
        rects = {step_id: QRectF(item.pos().x(), item.pos().y(),
                                 node["w"], node["h"])
                 for step_id, item in self._nodes.items()}

        slots = self._slots()
        detours = []
        for edge in self._edges:
            out, into = edge.sides()
            edge.set_routing(slots[(edge.source, out, id(edge))],
                             slots[(edge.target, into, id(edge))])
            others = [rect for step_id, rect in rects.items()
                      if step_id not in (edge.source, edge.target)]
            if _crosses(_points_of(edge.path()), others):
                detours.append(edge)

        self._lane(detours, rects)

    def _slots(self):
        """``(node, side, edge) -> (index, of)`` for every end of every edge.

        Ordered by where the other end of each edge is, so the lines fan out
        in the order they arrive rather than crossing each other on the way to
        their own attachment points.
        """
        sharing = {}
        for edge in self._edges:
            out, into = edge.sides()
            for step_id, side, other in ((edge.source, out, edge.target_item),
                                         (edge.target, into, edge.source_item)):
                sharing.setdefault((step_id, side), []).append(
                    (other.pos().y() if side[0] else other.pos().x(),
                     edge.source, edge.target, edge))

        slots = {}
        for (step_id, side), members in sharing.items():
            members.sort(key=lambda one: (one[0], one[1], one[2]))
            for index, (_where, _source, _target, edge) in enumerate(members):
                slots[(step_id, side, id(edge))] = (index, len(members))
        return slots

    def _lane(self, detours, rects):
        """Send each edge that has to go around into a corridor of its own.

        Longest first and alternating sides, so the far-reaching edges sit
        outermost and the short ones nearest the nodes - the arrangement a
        transit map uses, and for the same reason: lines that never need to
        cross each other should not.
        """
        if not detours:
            return
        everything = QRectF()
        for rect in rects.values():
            everything = rect if everything.isEmpty() else everything.united(rect)
        gap = self._spec["edge"]["lane_gap"]
        stub = self._spec["edge"]["stub"]

        detours.sort(key=lambda one: (-abs(one.target_item.pos().y()
                                           - one.source_item.pos().y()),
                                      one.source, one.target))
        left = right = 0
        for index, edge in enumerate(detours):
            out, _into = edge.sides()
            if out[1]:                   # travelling down: the lane is an x
                if index % 2:
                    edge._lane = everything.right() + stub + right * gap
                    right += 1
                else:
                    edge._lane = everything.left() - stub - left * gap
                    left += 1
            else:                        # travelling across: the lane is a y
                if index % 2:
                    edge._lane = everything.bottom() + stub + right * gap
                    right += 1
                else:
                    edge._lane = everything.top() - stub - left * gap
                    left += 1
            edge.reroute()

    def places(self):
        """Where every node sits now, by step id, as plain floats.

        Plain rather than QPointF so whatever writes them down needs no Qt, and
        so the answer can be compared and stored as it is.
        """
        return {step_id: (item.pos().x(), item.pos().y())
                for step_id, item in self._nodes.items() if not item.is_phase}

    def _resize_scene(self):
        """Give the scene the graph plus room to roam around it.

        Two jobs. A node dragged past the old edge has to end up somewhere the
        view can still scroll to. And the surface has to stay draggable even
        when the whole cycle already fits on screen: a scene sized exactly to
        its contents has nowhere to scroll, so panning would quietly stop
        working on precisely the small cycles where moving things about is
        easiest. A viewport's worth of empty ground on every side fixes both,
        and is what a canvas is expected to feel like.

        Held as a placement throughout: growing the scene can shift the scroll
        bars, and the canvas resizing its own ground is not somebody choosing
        to look somewhere else.
        """
        with self._placing_view():
            if not self._nodes:
                self.scene().setSceneRect(QRectF(0, 0, 1, 1))
                return
            canvas = self._spec["canvas"]
            scale = self.transform().m11() or 1.0
            viewport = self.viewport().rect()
            roam_x = max(canvas["roam_min"],
                         viewport.width() / scale * canvas["roam"])
            roam_y = max(canvas["roam_min"],
                         viewport.height() / scale * canvas["roam"])
            rect = self.scene().itemsBoundingRect()
            self.scene().setSceneRect(rect.adjusted(-roam_x, -roam_y,
                                                    roam_x, roam_y))

    def content_rect(self):
        """Where the graph itself is, without the empty ground around it.

        What Fit fits to - fitting the scene rect would frame the roaming room
        as well and leave the cycle as a postage stamp in the middle.
        """
        margin = self._spec["grid"]["margin"]
        if not self._nodes:
            return QRectF()
        rect = QRectF()
        # The phases' frames too: a cycle opens at its top, and the top of a
        # framed cycle is the band naming its first phase, not its first box.
        for item in list(self._nodes.values()) + list(self._frames.values()):
            rect = rect.united(item.sceneBoundingRect())
        return rect.adjusted(-margin, -margin, margin, margin)

    # -- the surface ----------------------------------------------------------
    def drawBackground(self, painter, rect):
        """The canvas's own ground: the ruled paper the splash is drawn over.

        The grid earns its place twice over: it gives the graph a surface to sit
        on rather than floating on the page, and it is the only thing that shows
        the canvas moving when an empty part of it is dragged. Without it,
        panning past the last node looks like nothing happening.

        Ruled paper, like the artwork on the splash screen - so starting the
        application and opening a cycle land on the same surface rather than on
        two unrelated ones.
        """
        painter.fillRect(rect, QColor(ink("CANVAS_BG")))

        canvas = self._spec["canvas"]
        # Ruled in scene coordinates, so the paper moves with the content -
        # which is the point - but skipped when zoomed further out than a cycle
        # opens at, where the lines would collapse into a grey wash. The hair
        # of slack is because that is also the scale a cycle opens at: the
        # paper has to be there at it, and a transform that came back from a
        # file rather than from a multiplication can be a bit under.
        if self.transform().m11() < self._spec["zoom"]["grid"] - 1e-6:
            return

        painter.save()
        _rule(painter, rect, canvas, QColor(ink("CANVAS_GRID")),
              QColor(ink("CANVAS_GRID_MAJOR")))
        painter.restore()

    def arrange(self):
        """Put every node back where the computed layout says it belongs.

        The way out of a canvas somebody has rearranged into a mess, and the
        reason dragging is safe to offer: nothing about moving a node is
        permanent, and one button undoes all of it.
        """
        if not self._graph:
            return
        places = layout_graph((self._drawn or self._graph).get("nodes") or [],
                              self._spec)
        with self._moving_many():
            for step_id, item in self._nodes.items():
                if step_id in places:
                    item.setPos(*places[step_id])
        self.route()
        self._resize_scene()
        self._moved = False
        self._places = {}
        # So whatever remembered the old arrangement forgets it: Arrange is how
        # somebody says they want the computed layout back, and a file that
        # kept the mess would give it to them again on the next open.
        self.nodes_moved.emit()
        self.fit()

    def graph(self):
        return self._graph

    def node(self, step_id):
        return self._nodes.get(step_id)

    def node_ids(self):
        """Every step of the cycle, whether it has a box of its own or not."""
        return [node.get("id") for node in (self._graph or {}).get("nodes") or []]

    # -- live state -----------------------------------------------------------
    def set_status(self, step_id, status, duration_ms=None, attempts=0):
        """One node, one repaint. An id that is not here is simply ignored.

        Ignored rather than raising: a run may be of a cycle that has since been
        edited, and a stale event is not worth an exception in a paint path.
        """
        was = self._statuses.get(step_id) or (None, None, 0)
        self._statuses[step_id] = (status, duration_ms if duration_ms is not None
                                   else was[1], attempts or was[2])
        item = self._nodes.get(step_id)
        if item is not None:
            item.set_status(status, duration_ms, attempts)
        phase = self._phase_of.get(step_id)
        if phase is not None:
            if phase in self._folded:
                self._repaint_phase(phase)
            self._repaint_frames((phase,))

    def status_of(self, step_id):
        """What the canvas last heard about a step, whether it is drawn or not."""
        return (self._statuses.get(step_id) or ("pending",))[0]

    def clear_status(self):
        self._statuses = {}
        self._run_evidence = {}
        for item in self._nodes.values():
            item.clear_status()
        for name in self._folded:
            self._repaint_phase(name)
        self._repaint_frames()

    # -- looking at it --------------------------------------------------------
    @contextlib.contextmanager
    def _placing_view(self):
        """While this is held, the view moving is the canvas, not a reader."""
        was, self._placing = self._placing, True
        try:
            yield
        finally:
            self._placing = was

    def _scrolled(self, _value):
        """A scroll bar moved, and that was not the canvas moving it.

        The one place that sees every way of panning - the hand, the wheel, a
        key, the scroll bar itself - so it is also where taking the view over
        is noticed. What it must not mistake for a gesture is the canvas
        placing a graph: that scrolls too, and Qt finishes some of it a turn of
        the event loop later, after the flag has been put down, which is why a
        view identical to the one just placed is not a gesture either.
        """
        if self._placing or _same_view(self.view(), self._placed):
            return
        self._touched = True
        self.view_changed.emit()

    def _placed_here(self):
        """Remember the view just placed, so its late echoes are recognised."""
        self._placed = self.view()

    def reset_view(self):
        """How a cycle opens: the zoom the paper appears at, from the top.

        Not a fit. Fitting answers "how big is this cycle", which is a question
        nobody opening one has - and it answers it at whatever scale the window
        happens to be, so the same cycle looked different in a tall window, a
        short one and a dragged splitter. One scale for every cycle, the one
        the ruled paper starts at, with the first node at the top: a cycle runs
        downwards and a list is read from its beginning.
        """
        rect = self.content_rect()
        viewport = self.viewport().rect()
        # Nothing to place the view against yet - the page was built but never
        # shown, or the splitter has not laid out. showEvent comes back to this.
        if rect.isEmpty() or viewport.width() <= 1 or viewport.height() <= 1:
            return
        scale = self._spec["zoom"]["grid"]
        with self._placing_view():
            self.resetTransform()
            self.scale(scale, scale)
            self._floor = min(self._spec["zoom"]["min"], scale)
            # centerOn takes the middle of the viewport, so the top of the
            # graph is half a viewport below the point asked for. In scene
            # units, because that is what a scaled view measures in.
            self.centerOn(rect.center().x(),
                          rect.top() + viewport.height() / (2.0 * scale))
            self._resize_scene()
        self._placed_here()
        self._touched = False
        self._auto = "default"

    def fit(self, at_least=None):
        """Show all of it. The way back after zooming or panning.

        ``at_least`` is a scale it will not shrink below. Nothing passes one
        any more - a cycle opens through :meth:`reset_view` - but it is what
        makes this usable as "show all of it, but not smaller than X" and the
        Fit button passes nothing, because somebody pressing it has asked to
        see all of it however small that has to be.
        """
        self._touched = False
        self._auto = "fit"
        # Said whether or not there is anything to fit to yet: what it means is
        # "the view is back to being managed", and that is true of a canvas
        # with nothing on it too.
        self.view_reset.emit()
        # The graph, not the scene: the scene now carries a viewport's worth of
        # empty ground on every side so the canvas can be dragged, and fitting
        # that would leave the cycle as a postage stamp in the middle of it.
        rect = self.content_rect()
        if rect.isEmpty():
            return
        # A viewport with no area yet - the page was built but never shown, or
        # the splitter has not laid out - makes fitInView produce a transform
        # scaled to zero, and everything drawn afterwards is invisible at any
        # zoom. There is nothing to fit to yet, so leave the scale alone.
        viewport = self.viewport().rect()
        if viewport.width() <= 1 or viewport.height() <= 1:
            return
        with self._placing_view():
            self.fitInView(rect, Qt.KeepAspectRatio)
            # Never magnify on fit: a three-node cycle blown up to fill the
            # window looks like a mistake.
            if self.transform().m11() > 1.0:
                self.resetTransform()
            if at_least and self.transform().m11() < at_least:
                # Too big to show whole and still be read. Show it at a size
                # the labels survive, from the top of the graph - a long cycle
                # is a list, and a list is read from its beginning.
                self.resetTransform()
                self.scale(at_least, at_least)
                self.centerOn(rect.center().x(), rect.top())
                self._floor = min(self._spec["zoom"]["min"], at_least)
                self._placed_here()
                return
            # Remembered as the floor for zooming out. A large cycle has to be
            # allowed to fit below the ordinary minimum - otherwise Fit could
            # not show it - but once it has, zooming out further only shrinks
            # it into the middle of an empty canvas.
            self._floor = min(self._spec["zoom"]["min"], self.transform().m11())
            # The room to roam is in scene units, so a change of scale changes
            # how much of it a viewport covers.
            self._resize_scene()
        self._placed_here()

    def showEvent(self, event):
        """Place the view once there is a viewport to place it against.

        A graph built before the page was shown could not be (see
        :meth:`reset_view`), so the first showing is when it finally can be -
        onto the remembered view if the cycle was opened with one, because that
        first attempt could not honour it either.
        """
        QGraphicsView.showEvent(self, event)
        if not self._fitted and self._nodes:
            self._fitted = True
            if not self.apply_view(self._wanted_view):
                self.reset_view()

    # -- where somebody was looking -------------------------------------------
    def view(self):
        """``{"zoom", "center"}`` - what it would take to come back here."""
        centre = self.mapToScene(self.viewport().rect().center())
        return {"zoom": self.transform().m11(),
                "center": [centre.x(), centre.y()]}

    def apply_view(self, view):
        """Look at what ``view`` describes. True when there was one to apply.

        Clamped to the zoom this canvas allows, because the file it comes from
        is editable and a scale of zero is a view with nothing in it.
        """
        zoom, centre = _view_of(view)
        if zoom is None:
            return False
        viewport = self.viewport().rect()
        if viewport.width() <= 1 or viewport.height() <= 1:
            return False              # nothing laid out yet; showEvent retries
        zoom = min(max(zoom, self._spec["zoom"]["min"]),
                   self._spec["zoom"]["max"])
        with self._placing_view():
            self.resetTransform()
            self.scale(zoom, zoom)
            self.centerOn(*centre)
            self._resize_scene()
        self._placed_here()
        # Taken as "somebody is looking at this", so the view is not pulled
        # back to the default the next time the splitter moves.
        self._touched = True
        return True

    def zoom_in(self):
        self._zoom(self._spec["zoom"]["step"])

    def zoom_out(self):
        self._zoom(1.0 / self._spec["zoom"]["step"])

    def _zoom(self, by):
        """One step, unless it would leave the range zooming is allowed in.

        The floor is whatever Fit last needed (see :meth:`fit`), never more than
        the spec's minimum - so a large cycle that had to be fitted small can
        still be zoomed back in, which a fixed minimum would have prevented
        entirely.
        """
        current = self.transform().m11() or 1.0
        scale = current * by
        if self._floor <= scale <= self._spec["zoom"]["max"]:
            self._touched = True
            self.scale(by, by)
            self.view_changed.emit()

    def placed_now(self):
        """The steps the last build had to find a place for beside others.

        The page writes them down, so a new step keeps the place it was given
        rather than being placed afresh - and moving - every time the graph is
        drawn again.
        """
        return list(getattr(self, "_unplaced", []))

    def reveal(self, step_id, margin=48):
        """Scroll just enough to show one step, and not at all if it shows.

        Not :meth:`center_on`: centring moves the whole picture to wherever
        the step is, which after adding one read as the screen jumping away
        from what somebody was working on.
        """
        item = self._nodes.get(step_id)
        if item is not None:
            self.ensureVisible(item, margin, margin)

    def center_on(self, step_id):
        item = self._nodes.get(step_id)
        if item is not None:
            self.centerOn(item)

    def select(self, step_id, center=True):
        if step_id not in self._nodes:
            for name, members in self.phases().items():
                if step_id in members and name in self._folded:
                    self.unfold(name)
        self.scene().clearSelection()
        item = self._nodes.get(step_id)
        if item is not None:
            item.setSelected(True)
            if center:
                self.center_on(step_id)

    def selected(self):
        """Which step is selected, or "".

        Guarded against an item whose C++ half has gone: the scene can drop
        items from paths this class does not own - a clear, a parent going
        away - and asking one of those whether it is selected raises from
        inside a signal handler, where there is nothing useful to do with it.
        """
        for step_id, item in self._nodes.items():
            if _alive(item) and item.isSelected() and not item.is_phase:
                return step_id
        return ""

    def restyle(self):
        """Repaint everything after the palette moved under us."""
        self.setBackgroundBrush(QColor(ink("CANVAS_BG")))
        for item in list(self._nodes.values()) + list(self._edges):
            item.update()
        self.viewport().update()

    # -- drawing links --------------------------------------------------------
    def set_linking(self, why_not):
        """Let links be drawn between steps, or stop.

        ``why_not(waited_for, waiting)`` is asked of every step a link is
        dragged over and answers "" when it may land there, or the reason it
        may not - a loop, a link that is already there. None: no dots at all,
        which is every cycle not open for editing.
        """
        if self._link is not None and why_not is None:
            self._end_link(drop=False)
        self._linking = why_not
        for item in self._nodes.values():
            item.set_ports(why_not is not None)
        for edge in self._edges:
            edge.set_editable(why_not is not None)

    def linking(self):
        return self._linking is not None

    def selected_link(self):
        """``(waited for, waiting)`` of the selected link, or None."""
        for edge in self._edges:
            if _alive(edge) and edge.isSelected():
                return edge.source, edge.target
        return None

    def select_link(self, waited_for, waiting):
        """Select one link, if it is drawn and links can be edited."""
        edge = next((one for one in self._edges
                     if (one.source, one.target) == (waited_for, waiting)), None)
        if edge is None or not (edge.flags() & QGraphicsItem.ItemIsSelectable):
            return False
        self.scene().clearSelection()
        edge.setSelected(True)
        return True

    def _start_relink(self, edge, end, at):
        """Pick up one end of a selected link; the other stays where it is.

        Dragging its head moves which step waits; dragging where it starts
        moves which step is waited for. Either way it is the same drag as a
        new link out of the end that stays - one that replaces this one.
        """
        if end == "to":
            self._start_link(edge.source_item, "out", at, edge)
        else:
            self._start_link(edge.target_item, "in", at, edge)

    def _start_link(self, item, port, at, edge=None):
        """A link out of ``item``'s ``port`` dot, following the pointer.

        ``edge`` is the link being re-pointed, if this is one: hidden while
        its replacement is dragged, and back as it was if that is let go of
        nowhere.
        """
        pen = QPen(QColor(ink("ACCENT")))
        pen.setWidthF(self._spec["edge"]["lit_width"])
        pen.setCapStyle(Qt.RoundCap)
        pen.setDashPattern([3, 2])
        line = QGraphicsPathItem()
        line.setPen(pen)
        line.setZValue(LINK_Z)
        head = QGraphicsPolygonItem()
        head.setPen(QPen(Qt.NoPen))
        head.setBrush(QBrush(QColor(ink("ACCENT"))))
        head.setZValue(LINK_Z)
        self.scene().addItem(line)
        self.scene().addItem(head)
        self._link = {"from": item, "port": port, "line": line, "head": head,
                      "over": None, "why": "", "edge": edge,
                      "replacing": (edge.source, edge.target) if edge else None}
        if edge is not None:
            edge.setVisible(False)
        item.set_drop("ok")               # keeps its dots up while dragging
        self._drag_link(at, None)

    def _link_ends(self, over):
        """(waited for, waiting) if the link were dropped on ``over``."""
        mine = self._link["from"].step_id
        return (mine, over.step_id) if self._link["port"] == "out" \
            else (over.step_id, mine)

    def _drag_link(self, at, global_at):
        link = self._link
        here = self.mapToScene(at)
        over = next((one for one in self.scene().items(here)
                     if isinstance(one, CycleNodeItem) and not one.is_phase
                     and one is not link["from"]), None)
        if over is not link["over"]:
            if link["over"] is not None:
                link["over"].set_drop("")
            link["over"] = over
            link["why"] = ""
            if over is not None:
                link["why"] = self._linking(*self._link_ends(over),
                                            replacing=link["replacing"]) or ""
                over.set_drop("no" if link["why"] else "ok")
            if global_at is not None:
                self._say_link(global_at)
        source = link["from"]
        mine = source.mapToScene(source.port_point(link["port"]))
        # Snapped to the dot it would join when it can land, so what is on
        # screen is the link that letting go would make.
        other = here
        if over is not None and not link["why"]:
            other = over.mapToScene(over.port_point(
                "in" if link["port"] == "out" else "out"))
        start, end = (mine, other) if link["port"] == "out" else (other, mine)
        path = QPainterPath(start)
        path.lineTo(end)
        link["line"].setPath(path)
        length = math.hypot(end.x() - start.x(), end.y() - start.y())
        if length > 1:
            into = ((start.x() - end.x()) / length, (start.y() - end.y()) / length)
            link["head"].setPolygon(_arrow_head(end, self._spec["edge"]["arrow"],
                                                into))

    def _say_link(self, global_at):
        """What letting go here would do, next to the pointer."""
        link = self._link
        if link["over"] is None:
            QToolTip.hideText()
            return
        waited_for, waiting = self._link_ends(link["over"])
        QToolTip.showText(global_at, link["why"] or "%s will wait for %s"
                          % (waiting, waited_for), self.viewport())

    def _end_link(self, drop):
        link = self._link
        ends = self._link_ends(link["over"]) if link["over"] is not None else ()
        self._link = None
        QToolTip.hideText()
        for item in (link["from"], link["over"]):
            if item is not None and _alive(item):
                item.set_drop("")
        for item in (link["line"], link["head"]):
            if _alive(item):
                self.scene().removeItem(item)
        if link["edge"] is not None and _alive(link["edge"]):
            link["edge"].setVisible(True)
        if not (drop and ends and not link["why"]):
            return
        # Emitted last: whatever listens rebuilds the scene these items are in.
        if link["replacing"] is None:
            self.link_made.emit(*ends)
        elif tuple(ends) != link["replacing"]:
            self.link_moved.emit(*(link["replacing"] + tuple(ends)))

    # -- input ----------------------------------------------------------------
    def resizeEvent(self, event):
        """Keep the graph placed while nobody has taken control of the view.

        The panel it sits in is a splitter pane, so its size changes whenever
        anything else on the page does. Placing it again until the first zoom,
        pan or drag is what stops a cycle sitting half off the edge because the
        canvas was measured before the layout settled - and it places it where
        a cycle opens rather than fitting it, which is why the same cycle no
        longer looks different in a tall window and a short one.
        """
        QGraphicsView.resizeEvent(self, event)
        # How far the canvas can be dragged is measured from the viewport, so
        # it is recomputed whenever the viewport changes size.
        self._resize_scene()
        if not self._touched and self._nodes:
            # Whichever of the two the view was last placed by: a resize after
            # Fit means Fit, not the opening view, or pressing Fit and then
            # dragging the splitter would undo what was just asked for.
            self.fit() if self._auto == "fit" else self.reset_view()

    def wheelEvent(self, event):
        """Ctrl+wheel zooms about the cursor; a plain wheel scrolls.

        The same bargain every other scrollable view in this application makes,
        so the gesture does not have to be learned twice.
        """
        if event.modifiers() & Qt.ControlModifier:
            self._zoom(self._spec["zoom"]["step"]
                       if event.angleDelta().y() > 0
                       else 1.0 / self._spec["zoom"]["step"])
            event.accept()
            return
        self._touched = True
        QGraphicsView.wheelEvent(self, event)

    def mousePressEvent(self, event):
        """Decide, per press, whether this drag moves a node or the view.

        Or draws a link: a press on one of a step's dots while the cycle is
        being edited starts one, and the step itself stays where it is.
        """
        item = self.itemAt(event.position().toPoint())
        if event.button() == Qt.LeftButton and isinstance(item, CycleNodeItem):
            kind = item.restriction_at(item.mapFromScene(self.mapToScene(event.position().toPoint())))
            if kind:
                self.scene().clearSelection()
                item.setSelected(True)
                self.restriction_clicked.emit(item.step_id, kind, event.globalPosition().toPoint())
                event.accept()
                return
        if (event.button() == Qt.LeftButton and self._linking is not None
                and isinstance(item, CycleEdgeItem)):
            end = item.end_at(self.mapToScene(event.position().toPoint()))
            if end:
                self._touched = True
                self._start_relink(item, end, event.position().toPoint())
                event.accept()
                return
        if (event.button() == Qt.LeftButton and self._linking is not None
                and isinstance(item, CycleNodeItem) and item.has_ports):
            port = item.port_at(item.mapFromScene(self.mapToScene(
                event.position().toPoint())))
            if port:
                self._touched = True
                self._start_link(item, port, event.position().toPoint())
                event.accept()
                return
        on_a_node = isinstance(item, CycleNodeItem)
        if event.button() == Qt.MiddleButton:
            self.setDragMode(QGraphicsView.ScrollHandDrag)
        elif event.button() == Qt.LeftButton:
            self.setDragMode(QGraphicsView.NoDrag if on_a_node
                             else QGraphicsView.ScrollHandDrag)
        self._touched = True
        QGraphicsView.mousePressEvent(self, event)

    def mouseMoveEvent(self, event):
        if self._link is not None:
            self._drag_link(event.position().toPoint(),
                            event.globalPosition().toPoint())
            event.accept()
            return
        QGraphicsView.mouseMoveEvent(self, event)

    def keyPressEvent(self, event):
        if self._link is not None and event.key() == Qt.Key_Escape:
            self._end_link(drop=False)
            event.accept()
            return
        chosen = self.selected_link() if self._linking is not None else None
        if chosen and event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            event.accept()
            self.link_removed.emit(*chosen)
            return
        QGraphicsView.keyPressEvent(self, event)

    def mouseReleaseEvent(self, event):
        if self._link is not None:
            self._end_link(drop=event.button() == Qt.LeftButton)
            event.accept()
            return
        # Asked of the items rather than assumed from the press: a click that
        # selected a node did not move anything, and Arrange offering itself
        # after one would be a button for a thing that never happened.
        moved = any(item.moved_by_hand() for item in self._nodes.values())
        QGraphicsView.mouseReleaseEvent(self, event)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self._moved = self._moved or moved
        if moved:
            # A node may have been pulled past the old edge of the scene, and
            # the view can only scroll to what the scene says exists.
            self._resize_scene()
            # Once the drag has finished, not while it is going on: writing a
            # file on every pixel of movement would be a file written a hundred
            # times to record one decision.
            self.nodes_moved.emit()

    def mouseDoubleClickEvent(self, event):
        """Double-clicking a node opens it. Anything else fits the view.

        The two gestures a canvas is expected to have, and neither of them is
        something a panel down the side could offer.
        """
        item = self.itemAt(event.position().toPoint())
        if isinstance(item, CycleNodeItem) and item.is_phase:
            # Later, not now: opening it rebuilds the scene this very item
            # is in, while its event is still being delivered.
            name = item.step_id[len(GROUP_PREFIX):]
            QTimer.singleShot(0, lambda: self.unfold(name))
            event.accept()
            return
        if isinstance(item, CycleNodeItem):
            self.node_opened.emit(item.step_id)
            event.accept()
            return
        if isinstance(item, (CycleGroupFrame, CycleEdgeItem)):
            # Near a line is not empty ground: it is a click that missed
            # selecting the line by a hair, and fitting the view would throw
            # away where somebody had got to.
            event.accept()
            return
        self.fit()
        QGraphicsView.mouseDoubleClickEvent(self, event)

    def contextMenuEvent(self, event):
        """Right-click: say which step, or that it was the ground, and where."""
        item = self.itemAt(event.pos())
        if isinstance(item, CycleNodeItem) and not item.is_phase:
            self.scene().clearSelection()
            item.setSelected(True)
            self.node_menu.emit(item.step_id, event.globalPos())
        elif isinstance(item, CycleEdgeItem):
            # A link between two phases stands for several; there is no one
            # of them to offer.
            if not (item.source.startswith(GROUP_PREFIX)
                    or item.target.startswith(GROUP_PREFIX)):
                self.edge_menu.emit(item.source, item.target, event.globalPos())
        elif not isinstance(item, (CycleNodeItem, CycleGroupFrame)):
            self.canvas_menu.emit(event.globalPos())
        event.accept()

    def moved(self):
        """True once a node has been dragged - what enables Arrange."""
        return self._moved

    def _selection_changed(self):
        self._light_edges(self.selected())
        self.node_selected.emit(self.selected())

    def _light_edges(self, step_id):
        """Draw the lines touching one step heavier than the rest.

        What a click on a node is actually asking: *what is this connected
        to*. On a long cycle that is a real question - a step's links run off
        in both directions past a dozen other boxes, and following one by eye
        means tracing a grey line through every other grey line.

        Both directions, not only the ones leaving it. "What does this wait
        for" and "what waits for this" are one question when you are reading a
        graph, and answering half of it would leave somebody tracing the other
        half by hand.
        """
        wanted = self._nodes.get(step_id)
        lit = set()
        if wanted is not None:
            lit = {id(edge) for edge in wanted.edges}
        for edge in self._edges:
            edge.set_lit(id(edge) in lit)



class CanvasSkeleton(widgets.Skeleton):
    """The canvas while a cycle loads: its own paper, and nodes breathing on it.

    Not a generic grey graph: the ruled paper is drawn by the same code and at
    the same scale the canvas opens a cycle at, and the placeholder nodes have
    the real nodes' size, spacing and links. So what is on the way looks like
    what arrives, and the moment it arrives is the boxes filling in rather than
    one picture being swapped for another.
    """

    #: How many nodes each layer of the placeholder has, top to bottom, over
    #: and over: mostly a sequence with the odd fork, like a real cycle.
    PATTERN = (1, 1, 2, 1, 1, 1, 3, 1)

    def __init__(self, canvas, spec=None):
        widgets.Skeleton.__init__(self, canvas.viewport(), "graph", ground=None)
        self._spec = spec or SPEC

    def zoom(self):
        """The scale a cycle opens at - see CycleCanvas.reset_view."""
        return self._spec["zoom"]["grid"]

    def layers(self):
        zoom = self.zoom()
        node, grid = self._spec["node"], self._spec["grid"]
        width, height = node["w"] * zoom, node["h"] * zoom
        pitch = (node["h"] + grid["layer_gap"]) * zoom
        beside = grid["sibling_gap"] * zoom
        found, y, index = [], grid["margin"] * zoom, 0
        while y < self.height():
            count = self.PATTERN[index % len(self.PATTERN)]
            span = count * width + (count - 1) * beside
            left = (self.width() - span) / 2.0
            found.append([QRectF(left + one * (width + beside), y, width, height)
                          for one in range(count)])
            y += pitch
            index += 1
        return found

    def paint_ground(self, painter):
        painter.fillRect(self.rect(), QColor(ink("CANVAS_BG")))
        zoom = self.zoom()
        painter.save()
        painter.scale(zoom, zoom)
        _rule(painter, QRectF(0, 0, self.width() / zoom, self.height() / zoom),
              self._spec["canvas"], QColor(ink("CANVAS_GRID")),
              QColor(ink("CANVAS_GRID_MAJOR")))
        painter.restore()

    def paint_shapes(self, painter):
        zoom = self.zoom()
        node, edge = self._spec["node"], self._spec["edge"]
        layers = self.layers()
        ink_line = QColor(ink("NEUTRAL500"))
        pen = QPen(ink_line)
        pen.setWidthF(max(1.0, edge["width"] * zoom))
        # The links first, under the nodes, bent the way the canvas bends them.
        for above, below in zip(layers, layers[1:]):
            for one in above:
                for two in below:
                    start = QPointF(one.center().x(), one.bottom())
                    tip = QPointF(two.center().x(), two.top() - edge["gap"] * zoom)
                    end = QPointF(tip.x(), tip.y() - edge["arrow"] * zoom * 0.8)
                    painter.setPen(pen)
                    painter.setBrush(Qt.NoBrush)
                    path = QPainterPath(start)
                    for point in _elbow(start, end, SIDES["bottom"], edge["stub"] * zoom):
                        path.lineTo(point)
                    painter.drawPath(path)
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(ink_line)
                    painter.drawPolygon(_arrow_head(tip, edge["arrow"] * zoom, SIDES["top"]))
        border = QPen(QColor(ink("DIVIDER")))
        border.setWidthF(1.0)
        for row, layer in enumerate(layers):
            body = self.colour(row, ink("NEUTRAL300"), ink("NEUTRAL100"))
            text = self.colour(row, ink("NEUTRAL400"), ink("NEUTRAL300"))
            for box in layer:
                painter.setPen(border)
                painter.setBrush(body)
                painter.drawRoundedRect(box, node["radius"] * zoom, node["radius"] * zoom)
                # Where the title and the plugin will be written, and the
                # status stripe down the left edge.
                painter.setPen(Qt.NoPen)
                painter.setBrush(text)
                left = box.left() + (node["status_bar"] + node["pad_x"]) * zoom
                inner = box.width() - (node["status_bar"] + node["pad_x"] * 2) * zoom
                top = box.top() + node["pad_y"] * zoom
                painter.drawRoundedRect(QRectF(left, top, inner * 0.62, 12 * zoom), 2, 2)
                painter.drawRoundedRect(QRectF(left, top + 20 * zoom, inner * 0.38,
                                               10 * zoom), 2, 2)

# -- shared helpers -----------------------------------------------------------
def _same_view(one, other):
    """Whether two views are the same place, give or take a rounding.

    A scroll bar is whole pixels, so a view placed and then read back - or
    re-centred by Qt as the viewport changes size - can be a pixel out. A
    pixel is one scene unit at full size and more zoomed out: 1 / zoom of
    them, so that is the allowance. Half a unit, as it was, was less than a
    pixel below full size, and a resize after Fit rounding by one read as
    somebody panning - which stopped the view following the panel.
    """
    if not one or not other:
        return False
    pixel = 1.0 / max(one["zoom"], 1e-6)
    return (abs(one["zoom"] - other["zoom"]) < 1e-6
            and all(abs(a - b) <= max(0.5, pixel)
                    for a, b in zip(one["center"], other["center"])))


def _view_of(view):
    """``(zoom, (x, y))`` from a remembered view, or ``(None, None)``.

    Forgiving on purpose. This comes out of a file somebody can edit and a
    build that wrote it differently, and a view that cannot be read is a cycle
    opened the ordinary way - never a cycle that will not open.
    """
    if not isinstance(view, dict):
        return None, None
    try:
        zoom = float(view.get("zoom"))
        x, y = (float(one) for one in (view.get("center") or ()))
    except (TypeError, ValueError):
        return None, None
    if any(one != one for one in (zoom, x, y)) or zoom <= 0:
        return None, None                 # NaN, or a view with nothing in it
    return zoom, (x, y)


def _alive(item):
    """Whether a Qt object's C++ half is still there.

    Python keeps the wrapper long after Qt has deleted what it wrapped, and
    touching one of those raises rather than returning something useless -
    which, inside a signal handler, means a traceback out of a repaint.
    """
    try:
        import shiboken6
    except ImportError:                   # not a PySide build that ships it
        return True
    return shiboken6.isValid(item)


def _elide(painter, text, width):
    """``text`` shortened to fit, with an ellipsis Qt chooses for the font."""
    from PySide6.QtGui import QFontMetricsF
    return QFontMetricsF(painter.font()).elidedText(str(text), Qt.ElideRight,
                                                    width)


def _duration(milliseconds):
    """A duration in the unit that fits - the same rule the Run page follows."""
    if not milliseconds:
        return ""
    seconds = milliseconds / 1000.0
    if seconds < 1:
        return "%dms" % milliseconds
    if seconds < 60:
        return "%.1fs" % seconds
    minutes, seconds = divmod(int(seconds), 60)
    return "%dm %02ds" % (minutes, seconds)


def _clean(number):
    """30 rather than 30.0, for a tooltip."""
    try:
        value = float(number)
    except (TypeError, ValueError):
        return number
    return int(value) if value.is_integer() else value
