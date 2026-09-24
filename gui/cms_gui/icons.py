"""The interface's icons, painted rather than typed.

Every mark here used to be a character - a play triangle, a gear, a half-filled
square - chosen at runtime from a list of candidates by asking the body font
whether it could draw it
(see the glyph table this replaced). That works where the body font happens to
carry the symbol and fails everywhere else, and Windows is everywhere else: the
font resolved there carries almost none of them, so the whole interface fell
back to its ASCII stand-ins and the rail read "= • > +" while the toolbar
offered "% Developer mode" and "* Settings". Nothing was broken; the font simply
did not have the characters, which is not something a program can fix by
choosing a different character.

So these are drawn. Two 1px strokes on a 24-unit grid, square caps and square
joins, the same wireframe rule the stylesheet follows - and no dependency on
what is installed, which is the point. Each size is painted at its own size
rather than scaled from one bitmap, so 16px stays legible.

Colour is a parameter, not a property of the icon: the palette moves when dark
mode is set, and status marks are drawn in the colour of the status they report.
``clear_cache()`` exists for the moment the palette changes underneath.
"""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QIcon, QPainter, QPainterPath, QPen,
                           QPixmap, QPolygonF)

from . import theme

#: Everything below is drawn on this grid and scaled to the size asked for.
GRID = 24.0

#: Rendered sizes. 16 is the one that has to survive: it is the button icon.
SIZES = (16, 20, 24, 32, 48)

#: Stroke width, on the grid. Matches the hairline the stylesheet draws.
STROKE = 2.0


# --- the drawings -----------------------------------------------------------
# Each takes a painter already scaled to GRID, with the pen and brush set to the
# icon's colour. Fill and stroke are both used: a filled mark reads as an action
# (run, stop), an outlined one as a place or a thing (environments, artifacts).

def _stroke(painter, ink):
    """Outline mode: a pen of the icon's colour, and nothing filled."""
    pen = QPen(QColor(ink))
    pen.setWidthF(STROKE)
    pen.setCapStyle(Qt.SquareCap)
    pen.setJoinStyle(Qt.MiterJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)


def _fill(painter, ink):
    """Solid mode: no outline, brush of the icon's colour.

    Both halves are set every time on purpose. Setting only the pen left the
    brush from whatever ran before it, and a shape drawn with NoPen after an
    outline shape had cleared the brush was drawn with neither - which is how
    the step markers vanished from the scenarios mark and the second window
    from the launch one.
    """
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(ink))


def _run(painter, ink):
    """A play triangle: the one mark nobody has to be taught."""
    _fill(painter, ink)
    painter.drawPolygon(QPolygonF([QPointF(6.75, 4.5), QPointF(17.25, 12),
                                   QPointF(6.75, 19.5)]))


def _run_step(painter, ink):
    """One chevron: run this step and nothing else.

    A chevron rather than the play triangle, because it is not the same act -
    the triangle starts the cycle, and these two start a part of it. Outlined
    rather than filled, for the same reason: the weight says which of them is
    the ordinary thing to press.
    """
    _stroke(painter, ink)
    painter.drawPolyline(QPolygonF([QPointF(9, 5), QPointF(16, 12),
                                    QPointF(9, 19)]))


def _run_from(painter, ink):
    """Two chevrons: this step and everything that waits on it."""
    _stroke(painter, ink)
    painter.drawPolyline(QPolygonF([QPointF(5, 5), QPointF(12, 12),
                                    QPointF(5, 19)]))
    painter.drawPolyline(QPolygonF([QPointF(12, 5), QPointF(19, 12),
                                    QPointF(12, 19)]))


def _stop(painter, ink):
    _fill(painter, ink)
    painter.drawRect(QRectF(6, 6, 12, 12))


def _copy(painter, ink):
    """Two sheets, offset - the shape every clipboard in every toolbar uses."""
    _stroke(painter, ink)
    painter.drawRect(QRectF(8, 3.5, 12, 14))
    painter.drawRect(QRectF(4, 7.5, 12, 14))


def _refresh(painter, ink):
    """An open circle with a head on it: a cycle, not a full ring."""
    _stroke(painter, ink)
    path = QPainterPath()
    path.arcMoveTo(QRectF(4, 4, 16, 16), 60)
    path.arcTo(QRectF(4, 4, 16, 16), 60, 280)
    painter.drawPath(path)
    _fill(painter, ink)
    painter.drawPolygon(QPolygonF([QPointF(11.5, 3), QPointF(16.5, 7),
                                   QPointF(11.5, 11)]))


def _settings(painter, ink):
    """A gear: a ring, a hub, and eight teeth around it."""
    painter.save()
    painter.translate(12, 12)
    _fill(painter, ink)
    for step in range(8):
        painter.save()
        painter.rotate(step * 45)
        painter.drawRect(QRectF(-1.6, -11, 3.2, 4.5))
        painter.restore()
    painter.restore()
    _stroke(painter, ink)
    painter.drawEllipse(QRectF(4.5, 4.5, 15, 15))
    painter.drawEllipse(QRectF(9.5, 9.5, 5, 5))


def _environments(painter, ink):
    """Stacked rows: a table of environments, which is what the page is."""
    _stroke(painter, ink)
    painter.drawRect(QRectF(3.5, 4.5, 17, 15))
    painter.drawLine(QPointF(3.5, 9.5), QPointF(20.5, 9.5))
    painter.drawLine(QPointF(3.5, 14.5), QPointF(20.5, 14.5))


def _credentials(painter, ink):
    """A key: the page holds logins and passwords, and says so."""
    _stroke(painter, ink)
    painter.drawEllipse(QRectF(3.5, 8.5, 8, 8))
    painter.drawLine(QPointF(11, 12.5), QPointF(20.5, 12.5))
    painter.drawLine(QPointF(17, 12.5), QPointF(17, 16.5))
    painter.drawLine(QPointF(20, 12.5), QPointF(20, 15.5))


def _command(painter, ink):
    """A prompt: a chevron and a caret. The page is the command line."""
    _stroke(painter, ink)
    painter.drawRect(QRectF(3.5, 4.5, 17, 15))
    path = QPainterPath()
    path.moveTo(7, 9)
    path.lineTo(10.5, 12)
    path.lineTo(7, 15)
    painter.drawPath(path)
    painter.drawLine(QPointF(12.5, 15.5), QPointF(17, 15.5))


def _launch(painter, ink):
    """Two offset windows - the application's own mark, and its whole subject."""
    _stroke(painter, ink)
    painter.drawRect(QRectF(3.5, 4.5, 12, 10))
    _fill(painter, ink)
    painter.drawRect(QRectF(8.5, 9.5, 12, 10))


def _scenarios(painter, ink):
    """A list with a step marker: named things, run in order."""
    _stroke(painter, ink)
    painter.drawLine(QPointF(10.5, 6), QPointF(20.5, 6))
    painter.drawLine(QPointF(10.5, 12), QPointF(20.5, 12))
    painter.drawLine(QPointF(10.5, 18), QPointF(20.5, 18))
    _fill(painter, ink)
    for y in (6.0, 12.0, 18.0):
        painter.drawPolygon(QPolygonF([QPointF(3.5, y - 2.75), QPointF(7.5, y),
                                       QPointF(3.5, y + 2.75)]))


def _cycles(painter, ink):
    """Three nodes and the branch between them: a cycle is a graph, not a list.

    Deliberately not a circular arrow, which is what "cycle" suggests and what
    this is not - nothing here repeats. It is one node that leads to two, which
    is the smallest picture of the thing the page actually draws, and it reads
    as a different idea to the list-with-markers that Scenarios wears.
    """
    _stroke(painter, ink)
    painter.drawLine(QPointF(8.5, 12), QPointF(13, 12))
    painter.drawLine(QPointF(13, 12), QPointF(13, 6.5))
    painter.drawLine(QPointF(13, 12), QPointF(13, 17.5))
    painter.drawLine(QPointF(13, 6.5), QPointF(15.5, 6.5))
    painter.drawLine(QPointF(13, 17.5), QPointF(15.5, 17.5))
    _fill(painter, ink)
    painter.drawRect(QRectF(3.5, 9.5, 5, 5))
    painter.drawRect(QRectF(15.5, 4, 5, 5))
    painter.drawRect(QRectF(15.5, 15, 5, 5))


def _history(painter, ink):
    """A clock: the page is what happened, in the order it happened."""
    _stroke(painter, ink)
    painter.drawEllipse(QRectF(3.5, 3.5, 17, 17))
    painter.drawLine(QPointF(12, 7), QPointF(12, 12))
    painter.drawLine(QPointF(12, 12), QPointF(16, 14.5))


def _developer(painter, ink):
    """A square half filled: a mode that is either on or off."""
    _stroke(painter, ink)
    painter.drawRect(QRectF(3.5, 3.5, 17, 17))
    _fill(painter, ink)
    painter.drawRect(QRectF(4, 4, 8.5, 16))


def _log(painter, ink):
    """Lines of text, one shorter - a log, still being written."""
    _stroke(painter, ink)
    painter.drawLine(QPointF(3.5, 6.5), QPointF(20.5, 6.5))
    painter.drawLine(QPointF(3.5, 12.5), QPointF(20.5, 12.5))
    painter.drawLine(QPointF(3.5, 18.5), QPointF(14.5, 18.5))


def _artifacts(painter, ink):
    """A folder: reports, screenshots and DOM dumps, on disk."""
    _stroke(painter, ink)
    path = QPainterPath()
    path.moveTo(3.5, 19.5)
    path.lineTo(3.5, 5.5)
    path.lineTo(9.5, 5.5)
    path.lineTo(11.5, 8.5)
    path.lineTo(20.5, 8.5)
    path.lineTo(20.5, 19.5)
    path.closeSubpath()
    painter.drawPath(path)


def _pass(painter, ink):
    _stroke(painter, ink)
    path = QPainterPath()
    path.moveTo(4.5, 12.5)
    path.lineTo(9.5, 17.5)
    path.lineTo(19.5, 6.5)
    painter.drawPath(path)


def _fail(painter, ink):
    _stroke(painter, ink)
    painter.drawLine(QPointF(5.5, 5.5), QPointF(18.5, 18.5))
    painter.drawLine(QPointF(18.5, 5.5), QPointF(5.5, 18.5))


def _pending(painter, ink):
    """A hollow dot: a step that exists and has not started."""
    _stroke(painter, ink)
    painter.drawEllipse(QRectF(8.5, 8.5, 7, 7))


def _chevron_right(painter, ink):
    _stroke(painter, ink)
    path = QPainterPath()
    path.moveTo(9, 5.5)
    path.lineTo(16, 12)
    path.lineTo(9, 18.5)
    painter.drawPath(path)


def _chevron_left(painter, ink):
    _stroke(painter, ink)
    path = QPainterPath()
    path.moveTo(15, 5.5)
    path.lineTo(8, 12)
    path.lineTo(15, 18.5)
    painter.drawPath(path)


def _chevron_down(painter, ink):
    _stroke(painter, ink)
    path = QPainterPath()
    path.moveTo(5.5, 9)
    path.lineTo(12, 16)
    path.lineTo(18.5, 9)
    painter.drawPath(path)


def _browse(painter, ink):
    """Three dots: the button that opens a file chooser, everywhere."""
    _fill(painter, ink)
    for x in (5.0, 10.75, 16.5):
        painter.drawRect(QRectF(x, 10.5, 3, 3))


def _more(painter, ink):
    """The same three dots, stood up: a menu of the rest."""
    _fill(painter, ink)
    for y in (5.0, 10.75, 16.5):
        painter.drawRect(QRectF(10.5, y, 3, 3))


def _services(painter, ink):
    """Stacked lines with a play mark: the log page's rows, now startable.

    Deliberately a variation on the log mark rather than a new subject - the page
    is the old one with the other half of the job added, and the rail should say
    so at a glance.
    """
    _stroke(painter, ink)
    painter.drawLine(QPointF(3.5, 6.5), QPointF(20.5, 6.5))
    painter.drawLine(QPointF(3.5, 12.5), QPointF(11.5, 12.5))
    painter.drawLine(QPointF(3.5, 18.5), QPointF(11.5, 18.5))
    _fill(painter, ink)
    painter.drawPolygon(QPolygonF([QPointF(14.5, 11), QPointF(21, 15.5),
                                   QPointF(14.5, 20)]))


def _dot(painter, ink):
    """A filled disc. The status light beside a service, drawn in its colour."""
    _fill(painter, ink)
    painter.drawEllipse(QRectF(7.5, 7.5, 9, 9))


def _plus(painter, ink):
    _stroke(painter, ink)
    painter.drawLine(QPointF(12, 5.5), QPointF(12, 18.5))
    painter.drawLine(QPointF(5.5, 12), QPointF(18.5, 12))


def _minus(painter, ink):
    _stroke(painter, ink)
    painter.drawLine(QPointF(5.5, 12), QPointF(18.5, 12))


def _delete(painter, ink):
    """A bin: a lid with a handle over an open-topped can."""
    _stroke(painter, ink)
    painter.drawLine(QPointF(4.5, 6.5), QPointF(19.5, 6.5))
    painter.drawLine(QPointF(9.5, 6.5), QPointF(9.5, 3.5))
    painter.drawLine(QPointF(9.5, 3.5), QPointF(14.5, 3.5))
    painter.drawLine(QPointF(14.5, 3.5), QPointF(14.5, 6.5))
    path = QPainterPath()
    path.moveTo(6.5, 6.5)
    path.lineTo(7.5, 20.5)
    path.lineTo(16.5, 20.5)
    path.lineTo(17.5, 6.5)
    painter.drawPath(path)


#: name -> drawing. The names are the ones the interface already asked for, so
#: every call site reads the same as it did before the marks were characters.
DRAWINGS = {
    "run": _run,
    "run-step": _run_step,
    "run-from": _run_from,
    "stop": _stop,
    "copy": _copy,
    "refresh": _refresh,
    "settings": _settings,
    "environments": _environments,
    "credentials": _credentials,
    "command": _command,
    "launch": _launch,
    "scenarios": _scenarios,
    "cycles": _cycles,
    "history": _history,
    "developer": _developer,
    "log": _log,
    "services": _services,
    "dot": _dot,
    "artifacts": _artifacts,
    "pass": _pass,
    "check": _pass,
    "fail": _fail,
    "running": _run,
    "pending": _pending,
    "group": _chevron_right,
    "browse": _browse,
    "more": _more,
    "disclosure_open": _chevron_down,
    "disclosure_closed": _chevron_right,
    # The rail's handle, named for what it does rather than which way it points,
    # so the call site reads as the state it is switching to.
    "collapse": _chevron_left,
    "expand": _chevron_right,
    "plus": _plus,
    "minus": _minus,
    "delete": _delete,
}

_cache = {}


def clear_cache():
    """Forget every painted icon. Called when the palette changes under us."""
    _cache.clear()


def pixmap(name, size=16, color=None):
    """One icon, painted at ``size`` in ``color`` (default: the body text ink)."""
    ink = color or theme.TEXT
    key = (name, int(size), str(ink))
    if key in _cache:
        return _cache[key]
    canvas = QPixmap(int(size), int(size))
    canvas.fill(Qt.transparent)
    draw = DRAWINGS.get(name)
    if draw is not None:
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.scale(size / GRID, size / GRID)
        _stroke(painter, ink)
        draw(painter, ink)
        painter.end()
    _cache[key] = canvas
    return canvas


def icon(name, color=None):
    """A QIcon carrying every size, for a button, an action or a menu."""
    key = ("icon", name, str(color or theme.TEXT))
    if key in _cache:
        return _cache[key]
    built = QIcon()
    for size in SIZES:
        built.addPixmap(pixmap(name, size, color))
    _cache[key] = built
    return built


def button(widget, name, text=None, color=None):
    """Give ``widget`` an icon (and optionally its text), and hand it back.

    The one call every site uses, so "icon plus label" is spelled once rather
    than as string concatenation in thirty places.
    """
    widget.setIcon(icon(name, color))
    if text is not None:
        widget.setText(text)
    return widget
