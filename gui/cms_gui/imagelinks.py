"""Paths to pictures in an agent's tool rows, as links that open them.

Nothing is loaded to show a row: a path becomes a link only when the file is
there (one stat), and the picture is opened - in the desktop's own viewer -
only when the link is clicked.
"""

import html
import os
import re

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices

#: What counts as a picture, by its name.
EXTENSIONS = ("png", "jpg", "jpeg", "gif", "webp", "bmp", "svg")

#: An absolute or home path ending in a picture's extension. Stops at what
#: ends a path in a tool's text: space, quote, bracket, comma.
PATH = re.compile(r"(?<![\w/~.])((?:~|/)[^\s\"'<>|,;()\[\]{}]*\.(?:%s))(?![\w])" % "|".join(EXTENSIONS),
                  re.IGNORECASE)


def found(text):
    """The picture paths in ``text`` whose files are there, in order."""
    seen = []
    for match in PATH.finditer(text or ""):
        path = match.group(1)
        if path not in seen and os.path.isfile(os.path.expanduser(path)):
            seen.append(path)
    return seen


def linked(text):
    """``text`` as rich text with each picture that is there a link, keeping
    its lines - or None when it names none."""
    paths = set(found(text))
    if not paths:
        return None
    made, at = [], 0
    for match in PATH.finditer(text):
        path = match.group(1)
        if path not in paths:
            continue
        made.append(html.escape(text[at:match.start(1)]))
        made.append('<a href="%s">%s</a>' % (
            html.escape(QUrl.fromLocalFile(os.path.expanduser(path)).toString(), quote=True), html.escape(path)))
        at = match.end(1)
    made.append(html.escape(text[at:]))
    # Pre-wrapped: a tool's lines keep their breaks and their indent.
    return '<qt><span style="white-space: pre-wrap">%s</span></qt>' % "".join(made)


def link(label):
    """Make ``label``'s pictures links, when it names any. Returns whether it did."""
    rich = linked(label.text())
    if rich is None:
        return False
    label.setTextFormat(Qt.RichText)
    label.setText(rich)
    label.setTextInteractionFlags(label.textInteractionFlags() | Qt.LinksAccessibleByMouse)
    label.setOpenExternalLinks(False)
    label.linkActivated.connect(show)
    label.setToolTip("Click a picture's path to open it")
    return True


def show(url):
    """Open the picture in the desktop's viewer."""
    QDesktopServices.openUrl(QUrl(url))
