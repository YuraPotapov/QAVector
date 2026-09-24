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

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QPainter, QPainterPath, QPen,
                           QPolygonF)
from PySide6.QtWidgets import (QApplication, QGraphicsItem, QGraphicsObject,
                               QGraphicsPathItem, QGraphicsScene, QGraphicsView)

from . import icons, theme

#: Everything the canvas looks like, in one place. A redesign changes values
#: here; it does not change layout_graph, the items, or the page. Colours are
#: not here - they are read from ``theme`` at paint time, because the palette
#: moves when dark mode is set.
SPEC = {
    "node": {"w": 208, "h": 76, "radius": 8, "border": 1, "border_selected": 2,
             "status_bar": 3, "pad_x": 12, "pad_y": 10, "badge": 13},
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
             # None is a solid line. A dash pattern is in pen units.
             "dash": {"dependency": None, "success": None, "failure": (4, 3)}},
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


def scene_size(places, spec=None):
    """How big the scene has to be to hold ``places``, including the margin."""
    spec = spec or SPEC
    if not places:
        return (0.0, 0.0)
    margin = spec["grid"]["margin"]
    right = max(x for x, _y in places.values()) + spec["node"]["w"] + margin
    bottom = max(y for _x, y in places.values()) + spec["node"]["h"] + margin
    return (right, bottom)


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
        self.edges = []              # every edge with this node at either end
        #: What to call when this node moves. The canvas sets it, because a
        #: move changes the routing of edges that are nothing to do with this
        #: node and only the canvas can see those.
        self.on_moved = None
        self._pressed_at = QPointF()
        self._dragging = False
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

    # -- painting -------------------------------------------------------------
    def boundingRect(self):
        node = self._spec["node"]
        # Half the border on each side, so a selected node's thicker outline is
        # not clipped by its own rectangle.
        bleed = node["border_selected"]
        return QRectF(-bleed, -bleed, node["w"] + bleed * 2,
                      node["h"] + bleed * 2)

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
        edge = QColor(ink("ACCENT" if self.isSelected() else "DIVIDER"))

        path = QPainterPath()
        path.addRoundedRect(body, spec["radius"], spec["radius"])
        painter.setBrush(QBrush(fill))
        pen = QPen(edge)
        pen.setWidthF(spec["border_selected"] if self.isSelected()
                      else spec["border"])
        painter.setPen(pen)
        painter.drawPath(path)

        self._paint_status_bar(painter, body, path)
        self._paint_text(painter, spec, text, disabled)
        self._paint_badges(painter, spec)

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
                         _elide(painter, self.node.get("label") or self.step_id,
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
        painter.drawText(
            QRectF(left, spec["pad_y"] + text["title"] + 6, width,
                   text["plugin"] + 4),
            Qt.AlignLeft | Qt.AlignVCenter,
            _elide(painter, self.node.get("plugin") or "", width))

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
        """Small marks along the top right: retry, a condition, a long-runner."""
        size = spec["badge"]
        x = spec["w"] - spec["pad_x"] - size
        y = spec["pad_y"]
        for name in self._badges():
            painter.drawPixmap(QRectF(x, y, size, size).toRect(),
                               icons.pixmap(name, size, ink("NEUTRAL500")))
            x -= size + 4

    def _badges(self):
        found = []
        if self.node.get("condition"):
            found.append("group")
        if int(self.node.get("retry") or 1) > 1 or self._attempts > 1:
            found.append("refresh")
        return found

    def _tooltip(self):
        """Everything that will not fit on the node itself."""
        rows = ["%s  (%s)" % (self.node.get("label") or self.step_id,
                              self.node.get("plugin") or "")]
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
        return "\n".join(rows)


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
        self.setZValue(EDGE_Z)      # under the nodes, always
        self.reroute()

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
        self.setZValue(EDGE_LIT_Z if lit else EDGE_Z)
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
        self.setPath(path)
        self._head = _arrow_head(tip, edge["arrow"], into)
        self.prepareGeometryChange()

    def boundingRect(self):
        # The head lives outside the path, so the rect has to cover both or the
        # tip is clipped away as the view scrolls. The margin is half the
        # heaviest pen this edge can be drawn with, plus one: a path's own rect
        # is the line through its middle and knows nothing about how thick it
        # is, so a fixed margin would clip a lit edge along its length.
        edge = self._spec["edge"]
        margin = max(edge["width"], edge["lit_width"]) / 2.0 + 1.0
        return QGraphicsPathItem.boundingRect(self).united(
            self._head.boundingRect()).adjusted(-margin, -margin,
                                                margin, margin)

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing, True)
        edge = self._spec["edge"]
        colour = QColor(ink("ACCENT" if self._lit else "NEUTRAL500"))
        pen = QPen(colour)
        pen.setWidthF(edge["lit_width"] if self._lit else edge["width"])
        pen.setCapStyle(Qt.RoundCap)
        dash = self._spec["edge"]["dash"].get(self.kind)
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

    def __init__(self, parent=None, spec=None):
        QGraphicsView.__init__(self, parent)
        self._spec = spec or SPEC
        self._nodes = {}
        self._edges = []
        self._graph = None
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
    def set_graph(self, graph, places=None, view=None):
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
        """
        # Building a scene scrolls: items appearing move the scene rect under
        # the view. Held as a placement from end to end so none of that reads
        # as somebody choosing to look somewhere.
        with self._placing_view():
            self._build(graph, places, view)

    def _build(self, graph, places, view):
        """The scene itself. Split out only so the placement above wraps it."""
        # Emptied **before** the scene is cleared, not after. Clearing deletes
        # the items, and deleting a selected one emits selectionChanged - which
        # lands in a handler that walks this dictionary. Done the other way
        # round it walks items whose C++ half has just gone, which is a
        # RuntimeError out of a paint path rather than anything a reader of
        # this method would expect.
        self._nodes, self._edges = {}, []
        self.scene().clear()
        self._graph = graph or None
        self._fitted = False
        self._placed = None
        self._wanted_view = view if _view_of(view)[0] is not None else None
        if not graph:
            self.scene().setSceneRect(QRectF(0, 0, 1, 1))
            return

        nodes = graph.get("nodes") or []
        computed = layout_graph(nodes, self._spec)
        remembered = dict(places or {})

        for node in nodes:
            item = CycleNodeItem(node, self._spec)
            x, y = remembered.get(node["id"],
                                  computed.get(node["id"], (0.0, 0.0)))
            item.setPos(x, y)
            item.on_moved = self.route
            self.scene().addItem(item)
            self._nodes[node["id"]] = item

        for edge in graph.get("edges") or []:
            source = self._nodes.get(edge.get("from"))
            target = self._nodes.get(edge.get("to"))
            if source is None or target is None:
                continue          # an edge to a step that is not in the graph
            item = CycleEdgeItem(source, target,
                                 edge.get("kind", "dependency"), self._spec)
            source.edges.append(item)
            target.edges.append(item)
            self.scene().addItem(item)
            self._edges.append(item)

        self.route()
        self._resize_scene()
        # A cycle opened with a remembered arrangement is already "moved", so
        # Arrange offers itself straight away - which is the only way back to
        # the computed layout once one has been kept.
        self._moved = bool(remembered)
        if not self.apply_view(self._wanted_view):
            self.reset_view()

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
        for node in graph.get("nodes") or []:
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
                for step_id, item in self._nodes.items()}

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
        for item in self._nodes.values():
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
        places = layout_graph(self._graph.get("nodes") or [], self._spec)
        for step_id, item in self._nodes.items():
            if step_id in places:
                item.setPos(*places[step_id])
        self.route()
        self._resize_scene()
        self._moved = False
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
        return list(self._nodes)

    # -- live state -----------------------------------------------------------
    def set_status(self, step_id, status, duration_ms=None, attempts=0):
        """One node, one repaint. An id that is not here is simply ignored.

        Ignored rather than raising: a run may be of a cycle that has since been
        edited, and a stale event is not worth an exception in a paint path.
        """
        item = self._nodes.get(step_id)
        if item is not None:
            item.set_status(status, duration_ms, attempts)

    def clear_status(self):
        for item in self._nodes.values():
            item.clear_status()

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

    def center_on(self, step_id):
        item = self._nodes.get(step_id)
        if item is not None:
            self.centerOn(item)

    def select(self, step_id):
        self.scene().clearSelection()
        item = self._nodes.get(step_id)
        if item is not None:
            item.setSelected(True)
            self.center_on(step_id)

    def selected(self):
        """Which step is selected, or "".

        Guarded against an item whose C++ half has gone: the scene can drop
        items from paths this class does not own - a clear, a parent going
        away - and asking one of those whether it is selected raises from
        inside a signal handler, where there is nothing useful to do with it.
        """
        for step_id, item in self._nodes.items():
            if _alive(item) and item.isSelected():
                return step_id
        return ""

    def restyle(self):
        """Repaint everything after the palette moved under us."""
        self.setBackgroundBrush(QColor(ink("CANVAS_BG")))
        for item in list(self._nodes.values()) + list(self._edges):
            item.update()
        self.viewport().update()

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
        """Decide, per press, whether this drag moves a node or the view."""
        on_a_node = isinstance(self.itemAt(event.position().toPoint()),
                               CycleNodeItem)
        if event.button() == Qt.MiddleButton:
            self.setDragMode(QGraphicsView.ScrollHandDrag)
        elif event.button() == Qt.LeftButton:
            self.setDragMode(QGraphicsView.NoDrag if on_a_node
                             else QGraphicsView.ScrollHandDrag)
        self._touched = True
        QGraphicsView.mousePressEvent(self, event)

    def mouseReleaseEvent(self, event):
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
        if isinstance(item, CycleNodeItem):
            self.node_opened.emit(item.step_id)
            event.accept()
            return
        self.fit()
        QGraphicsView.mouseDoubleClickEvent(self, event)

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


# -- shared helpers -----------------------------------------------------------
def _same_view(one, other):
    """Whether two views are the same place, give or take a rounding.

    A scroll bar is whole pixels, so a view placed and then read back differs
    in the last decimal of a scene coordinate. Half a scene unit is well inside
    "nobody moved anything" at every zoom this canvas allows.
    """
    if not one or not other:
        return False
    return (abs(one["zoom"] - other["zoom"]) < 1e-6
            and all(abs(a - b) < 0.5
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
