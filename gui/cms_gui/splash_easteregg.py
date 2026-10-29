"""The failed-scenario wink on the original splash artwork.

The JPG stays the startup image. Only its verification row changes after a
failed scenario; the separate parametric SVG prototype is not used at startup.
"""

import os

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap, QTransform

from . import projectsfile, store


def last_scenario_failed(directory):
    """Read the most recent scenario result, ignoring launches without tests.

    New runs save the last flow.end result immediately, even while browser
    windows remain open. Older records have only the completed run's totals.
    """
    entries = store.JsonStore(os.path.join(directory, "history.json"), default=[]).load()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        result = entry.get("last_scenario_status")
        if result in ("pass", "fail", "error"):
            return result != "pass"
        if entry.get("kind") not in ("launch", "command"):
            continue
        status, total, passed = entry.get("status"), entry.get("total"), entry.get("passed")
        if (status not in ("ok", "failed") or not isinstance(total, (int, float))
                or not isinstance(passed, (int, float)) or total <= 0):
            continue
        return status == "failed" or passed < total
    return False


def history_directory(settings):
    """Use the same project that the main window will restore."""
    try:
        project = projectsfile.find(projectsfile.load(), settings.current_project)
    except projectsfile.ProjectsFileError:
        project = None
    return project.folder if project and project.folder else settings.data_dir()


def failed_verification(artwork):
    """Copy the original artwork and replace only Step 3's icon and caption."""
    if artwork.isNull():
        return QPixmap(artwork)
    result = QPixmap(artwork)
    painter = QPainter(result)
    try:
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)
        painter.scale(result.width() / 1024, result.height() / 1024)
        # The central tile's coordinates in the original 1024px drawing.
        painter.setTransform(QTransform(.83, -.49, .83, .49, 309, 532), True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#f9f7ef"))
        painter.drawRect(QRectF(20, 190, 224, 61))

        painter.setPen(QPen(QColor("#40474c"), 1.6))
        painter.setBrush(QColor("#d4d5d2"))
        painter.drawRoundedRect(QRectF(26, 207, 39, 39), 3, 3)
        painter.setBrush(QColor("#fbfaf5"))
        painter.drawRoundedRect(QRectF(32, 201, 39, 39), 3, 3)

        # In the tile's own plane, corner to corner of the box's face - which
        # on screen reads as an upright cross on the tilted box (chosen over a
        # flat "x" and over one along the tile's axes).
        red = QColor("#c83e40")
        painter.setPen(QPen(red, 3.4, Qt.SolidLine, Qt.RoundCap))
        centre = QPointF(51.5, 220.5)
        for one, other in ((QPointF(-8, -8), QPointF(8, 8)),
                           (QPointF(-8, 8), QPointF(8, -8))):
            painter.drawLine(centre + one, centre + other)
        font = QFont("DejaVu Sans")
        font.setPixelSize(17)
        painter.setFont(font)
        painter.setPen(QColor("#40474c"))
        # On the original caption's line: lower along the tile than the first
        # attempt, which read as floating above it.
        painter.drawText(QPointF(90, 223), "Step 3: Not verify")
    finally:
        painter.end()
    return result


def pixmap(path, settings):
    """Keep the normal image untouched unless the easter egg applies."""
    artwork = QPixmap(path)
    if (os.path.basename(path) == "splash.jpg"
            and last_scenario_failed(history_directory(settings))):
        return failed_verification(artwork)
    return artwork
