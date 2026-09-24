"""The application icon, rendered from assets/qavector-mark.svg.

Three steps converging on a checked verdict - scenarios, services and agents
run as one graph that ends in "verified", which is what QAVector does. (The
three offset windows in assets/qavector-icon.svg were the mark of the old
chrome-multi-session and are kept only as artwork.) The marks are files the
designer owns, so changing the icon is replacing a file, not editing
coordinates here.

Every size is rendered from the vector at its own size rather than scaled down
from one bitmap. Below SMALL_BELOW the three steps would run together into a
smudge, so a simpler drawing is used: one step and the verdict.

``python -m cms_gui.icon <directory>`` writes the PNG and ICO files a packaged
build needs.
"""

import os
import struct
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
# Imported by name so a frozen build carries the SVG module and its plugin.
from PySide6.QtSvg import QSvgRenderer

#: The artwork, in the assets folder the packaged GUI already carries.
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
SOURCE = os.path.join(ASSETS, "qavector-mark.svg")
SMALL_SOURCE = os.path.join(ASSETS, "qavector-mark-small.svg")
#: Sizes under this use SMALL_SOURCE.
SMALL_BELOW = 24

# Rendered sizes: the platform picks; 16/32 are the ones that have to survive.
SIZES = (16, 20, 24, 32, 48, 64, 128, 256)

_cache = {}


def pixmap(size):
    """The icon at one size, rendered for that size.

    Blank when the artwork is missing or unreadable - a window without an icon
    is a smaller problem than a window that will not open.
    """
    if size in _cache:
        return _cache[size]
    image = QPixmap(size, size)
    image.fill(Qt.transparent)
    renderer = QSvgRenderer(SMALL_SOURCE if size < SMALL_BELOW else SOURCE)
    if renderer.isValid():
        painter = QPainter(image)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            renderer.render(painter)
        finally:
            painter.end()
    _cache[size] = image
    return image


def app_icon():
    """A QIcon carrying every size, for the window and the task switcher."""
    icon = QIcon()
    for size in SIZES:
        icon.addPixmap(pixmap(size))
    return icon


# -- files, for packaging -----------------------------------------------------
def write_png(path, size=256):
    return pixmap(size).save(path, "PNG")


def write_ico(path, sizes=(16, 24, 32, 48, 64, 128, 256)):
    """A genuinely multi-size .ico - every size painted at its own size.

    Assembled here rather than handed to Qt's ICO writer, which takes a single
    pixmap: that wrote one 256px image and left Windows to scale it down, which
    is exactly what the 16px taskbar icon must not be. The container is the
    PNG-in-ICO form every Windows since Vista reads.
    """
    from PySide6.QtCore import QBuffer, QIODevice

    encoded = []
    for size in sorted(sizes):
        # The buffer owns its bytes: handing QBuffer a temporary QByteArray
        # leaves Qt writing into something Python has already freed.
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        if not pixmap(size).save(buffer, "PNG"):
            return False
        buffer.close()
        encoded.append((size, bytes(buffer.data())))

    header = struct.pack("<HHH", 0, 1, len(encoded))     # reserved, type=icon, count
    offset = len(header) + 16 * len(encoded)
    directory, payload = b"", b""
    for size, data in encoded:
        directory += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,      # 0 means 256 in this field
            0 if size >= 256 else size,
            0, 0,                            # palette, reserved
            1, 32,                           # planes, bits per pixel
            len(data), offset)
        payload += data
        offset += len(data)
    try:
        with open(path, "wb") as handle:
            handle.write(header + directory + payload)
    except OSError:
        return False
    return True


def main(argv=None):
    from PySide6.QtGui import QGuiApplication

    argv = list(sys.argv if argv is None else argv)
    directory = argv[1] if len(argv) > 1 else os.getcwd()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QGuiApplication.instance() or QGuiApplication([])
    os.makedirs(directory, exist_ok=True)
    written = []
    for size in SIZES:
        path = os.path.join(directory, "icon-%d.png" % size)
        if write_png(path, size):
            written.append(path)
    ico = os.path.join(directory, "icon.ico")
    if write_ico(ico):
        written.append(ico)
    for path in written:
        print(path)
    del app
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
