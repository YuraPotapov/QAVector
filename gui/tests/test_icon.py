"""The application icon.

It is rendered from assets/qavector-mark.svg - with a simpler drawing for the
smallest sizes - so the things that can go wrong are the artwork not being
found, which renders nothing, and a small size where the mark stops reading
as a mark. Both are checked by looking at the pixels.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import icon

GROUND = "#1d2d3d"
VERDICT = "#eef6ff"
STEP = "#94bce3"


def _colours(size):
    """Every opaque colour in the icon at ``size``, with its pixel count."""
    image = icon.pixmap(size).toImage()
    counts = {}
    for x in range(image.width()):
        for y in range(image.height()):
            colour = image.pixelColor(x, y)
            if colour.alpha() == 255:
                name = colour.name().lower()
                counts[name] = counts.get(name, 0) + 1
    return counts


def test_the_artwork_ships_with_the_gui():
    for path in (icon.SOURCE, icon.SMALL_SOURCE):
        assert os.path.isfile(path), path
        assert os.path.dirname(path).endswith("assets")


def test_the_old_windows_artwork_is_kept():
    """The chrome-multi-session mark is no longer the icon, but stays as art."""
    assert os.path.isfile(os.path.join(icon.ASSETS, "qavector-icon.svg"))


def test_every_size_renders_something(qapp):
    for size in icon.SIZES:
        pixmap = icon.pixmap(size)
        assert not pixmap.isNull(), size
        assert (pixmap.width(), pixmap.height()) == (size, size)
        assert _colours(size), "size %d came out blank" % size


def test_the_icon_carries_every_size(qapp):
    available = {size.width() for size in icon.app_icon().availableSizes()}
    assert available == set(icon.SIZES)


def test_the_rounded_corners_are_clear(qapp):
    image = icon.pixmap(64).toImage()
    for x, y in ((0, 0), (63, 0), (0, 63), (63, 63)):
        assert image.pixelColor(x, y).alpha() == 0, (x, y)
    assert image.pixelColor(32, 8).alpha() == 255, "the ground is there"


def test_the_steps_and_the_verdict_are_drawn(qapp):
    colours = _colours(256)
    assert colours.get(GROUND, 0) > colours.get(VERDICT, 0) > 1000
    assert colours.get(STEP, 0) > 500


def test_the_verdict_still_reads_at_sixteen_pixels(qapp):
    """The size that actually matters. The small drawing has one step and a
    large verdict, so the light disc keeps pixels of its own there."""
    colours = _colours(16)
    assert colours.get(GROUND, 0) >= 20
    assert colours.get(VERDICT, 0) >= 12


def test_the_smallest_sizes_use_the_simpler_drawing(qapp, monkeypatch):
    used = []
    real = icon.QSvgRenderer
    monkeypatch.setattr(icon, "QSvgRenderer", lambda path: used.append(path) or real(path))
    monkeypatch.setattr(icon, "_cache", {})
    icon.pixmap(16)
    icon.pixmap(24)
    assert used == [icon.SMALL_SOURCE, icon.SOURCE]


def test_missing_artwork_gives_a_blank_icon_not_a_crash(qapp, monkeypatch):
    monkeypatch.setattr(icon, "SOURCE", "/nowhere/qavector-mark.svg")
    monkeypatch.setattr(icon, "_cache", {})
    pixmap = icon.pixmap(32)
    assert (pixmap.width(), pixmap.height()) == (32, 32)


def test_it_can_be_written_out_for_packaging(qapp, tmp_path):
    png = tmp_path / "icon-256.png"
    ico = tmp_path / "icon.ico"
    assert icon.write_png(str(png), 256)
    assert icon.write_ico(str(ico))
    assert png.stat().st_size > 0 and ico.stat().st_size > 0
