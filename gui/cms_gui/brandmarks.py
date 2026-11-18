"""The marks of the services QAVector connects to, drawn in their own colours.

On Integrations a provider is recognised by its maker's mark faster than by
its name: Jira's blue diamonds, Teams' purple tile, the agent programs' own
signs. These are drawn as paths, like every icon here (no glyphs, no files to
ship), in the makers' colours rather than the theme's - only where a mark is
black on one ground and white on the other does it follow the theme.

They are simplified drawings that identify the service an integration talks
to; they are not the makers' artwork and are not used to stand for QAVector.
"""

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

from . import theme

GRID = 24.0

JIRA_BLUE = "#1868db"
TEAMS_FRONT = "#4b53bc"
TEAMS_BACK = "#7b83eb"
TEAMS_SMALL = "#5059c9"
CLAUDE = "#d97757"


def _jira(painter):
    """A blue tile, and on it three diamonds stepping down to the left, the
    front one whole and the ones behind it fainter."""
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(JIRA_BLUE))
    painter.drawRoundedRect(QRectF(1, 1, 22, 22), 5, 5)
    for (x, y), half, alpha in (((6.3, 17.7), 3.2, 0.5), ((10.4, 13.6), 4.0, 0.75),
                                 ((15.4, 8.6), 4.8, 1.0)):
        diamond = QPainterPath()
        diamond.moveTo(x, y - half)
        diamond.lineTo(x + half, y)
        diamond.lineTo(x, y + half)
        diamond.lineTo(x - half, y)
        diamond.closeSubpath()
        white = QColor("#ffffff")
        white.setAlphaF(alpha)
        painter.setBrush(white)
        painter.drawPath(diamond)


def _teams(painter):
    """Two people behind a tile with a T: a team, and the place it talks."""
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(TEAMS_SMALL))
    painter.drawEllipse(QPointF(20.0, 7.4), 2.4, 2.4)
    painter.drawRoundedRect(QRectF(17.0, 10.6, 6.0, 8.4), 2.4, 2.4)
    painter.setBrush(QColor(TEAMS_BACK))
    painter.drawEllipse(QPointF(14.2, 5.8), 3.4, 3.4)
    painter.drawRoundedRect(QRectF(9.0, 10.0, 10.5, 11.5), 4.0, 4.0)
    painter.setBrush(QColor(TEAMS_FRONT))
    painter.drawRoundedRect(QRectF(1.0, 6.5, 13.0, 13.0), 2.0, 2.0)
    painter.setBrush(QColor("#ffffff"))
    painter.drawRect(QRectF(3.8, 9.3, 7.4, 2.0))
    painter.drawRect(QRectF(6.5, 9.3, 2.0, 7.6))


def _spark(painter, centre, radius, colour):
    """Claude's mark: rays of two lengths around a centre."""
    pen = QPen(QColor(colour))
    pen.setCapStyle(Qt.RoundCap)
    painter.setBrush(Qt.NoBrush)
    for ray in range(12):
        angle = math.radians(ray * 30 + 8)
        reach = radius if ray % 2 == 0 else radius * 0.72
        pen.setWidthF(radius * (0.24 if ray % 2 == 0 else 0.2))
        painter.setPen(pen)
        inner = 0.0
        painter.drawLine(QPointF(centre.x() + inner * math.cos(angle), centre.y() + inner * math.sin(angle)),
                         QPointF(centre.x() + reach * math.cos(angle), centre.y() + reach * math.sin(angle)))


def _knot(painter, centre, radius, colour):
    """OpenAI's mark: six rounded links, each turned a sixth from the last and
    set off to one side of the centre, so together they wind round a hexagon."""
    pen = QPen(QColor(colour))
    pen.setWidthF(radius * 0.16)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    width = radius * 0.56
    for link in range(6):
        painter.save()
        painter.translate(centre)
        painter.rotate(link * 60)
        painter.drawRoundedRect(QRectF(radius * 0.06, -radius * 0.95, width, radius * 1.2),
                                width / 2, width / 2)
        painter.restore()


def _claude(painter):
    """Claude's spark on the tint of the page's marks."""
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(theme.MARK_TINT))
    painter.drawRoundedRect(QRectF(1, 1, 22, 22), 5, 5)
    _spark(painter, QPointF(12.0, 12.0), 8.6, CLAUDE)


def _ink_pair():
    """OpenAI's mark is black on a light ground and white on a dark one."""
    dark = QColor(theme.BG).lightness() < 128
    return QColor("#ffffff" if dark else "#0d0d0d"), ("#0d0d0d" if dark else "#ffffff")


def _codex(painter):
    """Codex: OpenAI's knot, on its own black (or, in the dark, white) tile."""
    tile, ink = _ink_pair()
    painter.setPen(Qt.NoPen)
    painter.setBrush(tile)
    painter.drawRoundedRect(QRectF(1, 1, 22, 22), 5, 5)
    _knot(painter, QPointF(12.0, 12.0), 7.0, ink)


def _agents(painter):
    """Every agent program QAVector runs, before any has been used: Claude's
    spark, and OpenAI's knot (Codex) on a badge at its corner."""
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(theme.MARK_TINT))
    painter.drawEllipse(QPointF(10.0, 10.0), 9.5, 9.5)
    _spark(painter, QPointF(10.0, 10.0), 7.6, CLAUDE)
    badge, ink = _ink_pair()
    # A ring of the card's own ground keeps the two apart where they overlap.
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(theme.SURFACE))
    painter.drawEllipse(QPointF(17.5, 17.5), 6.6, 6.6)
    painter.setBrush(badge)
    painter.drawEllipse(QPointF(17.5, 17.5), 5.6, 5.6)
    _knot(painter, QPointF(17.5, 17.5), 3.9, ink)


#: Providers by their id; agent programs by their kind (``claude``, ``codex``).
MARKS = {"jira": _jira, "teams": _teams, "agents": _agents, "claude": _claude, "codex": _codex}


def paint(painter, name, rect):
    """Draw the mark of ``name`` into ``rect``. Unknown names draw nothing."""
    draw = MARKS.get(name)
    if draw is None:
        return
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.translate(rect.left(), rect.top())
    painter.scale(rect.width() / GRID, rect.height() / GRID)
    draw(painter)
    painter.restore()
