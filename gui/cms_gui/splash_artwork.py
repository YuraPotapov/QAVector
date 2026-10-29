"""Editable vector recreation of splash.jpg; no Qt needed to generate the SVG.

Run from gui/: python -m cms_gui.splash_artwork --output /tmp/splash.svg
The JSON beside the artwork supplies defaults; partial JSON overrides work too.
This module does not change the artwork selected by the application at startup.
"""

import argparse
from copy import deepcopy
import json
import math
from pathlib import Path
import re
from xml.sax.saxutils import escape, quoteattr


ASSETS = Path(__file__).resolve().parent / "assets"
PARAMETERS = ASSETS / "splash.parameters.json"
PLANE = "matrix(.83 -.49 .83 .49 0 0)"
INVERSE = "matrix(.60240964 .60240964 -1.02040816 1.02040816 0 0)"


def _merge(base, changes, prefix=""):
    if not isinstance(changes, dict):
        raise ValueError("%s must be an object" % (prefix or "parameters"))
    for key, value in changes.items():
        name = "%s.%s" % (prefix, key) if prefix else key
        if key not in base:
            raise ValueError("Unknown parameter: %s" % name)
        if isinstance(base[key], dict):
            _merge(base[key], value, name)
        else:
            base[key] = deepcopy(value)


def _number(value, name, low, high):
    if (isinstance(value, bool) or not isinstance(value, (float, int))
            or not math.isfinite(value) or not low <= value <= high):
        raise ValueError("%s must be between %s and %s" % (name, low, high))


def _validated(overrides):
    params = json.loads(PARAMETERS.read_text(encoding="utf-8"))
    _merge(params, {} if overrides is None else overrides)
    for key in ("width", "height"):
        _number(params[key], key, 1, 16384)
    _number(params["grid_opacity"], "grid_opacity", 0, 1)
    _number(params["pass_rate"], "pass_rate", 0, 100)
    if type(params["active_step"]) is not int or not 1 <= params["active_step"] <= 3:
        raise ValueError("active_step must be 1, 2 or 3")
    for key in ("title", "subtitle"):
        if not isinstance(params[key], str):
            raise ValueError("%s must be text" % key)
    for name, color in params["palette"].items():
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise ValueError("palette.%s must be a #RRGGBB color" % name)
    label_counts = {"environments": 3, "logs": 2, "scenarios": 4,
                    "sessions": 2, "reports": 2, "services": 2}
    for name, card in params["cards"].items():
        for key in ("x", "y"):
            _number(card[key], "%s.%s" % (name, key), -2048, 2048)
        _number(card["size"], name + ".size", 40, 600)
        _number(card["scale"], name + ".scale", .1, 4)
        if not isinstance(card["title"], str):
            raise ValueError(name + ".title must be text")
        labels = card["labels"]
        if (not isinstance(labels, list) or len(labels) != label_counts[name]
                or not all(isinstance(label, str) for label in labels)):
            raise ValueError("%s.labels must contain %d texts" % (name, label_counts[name]))
    return params


def _text(x, y, value, size=18, bold=False, anchor="start", fit=None):
    # Fit longer translations into the same area without turning text into paths.
    if fit:
        size = min(size, fit / max(1, len(value)) / .58)
    return ('<text x="%s" y="%s" font-size="%s" font-weight="%s" '
            'stroke="none" text-anchor=%s>%s</text>'
            % (x, y, size, 600 if bold else 400, quoteattr(anchor), escape(value)))


def _use(name, x, y, scale=1, standing=False):
    transform = "translate(%s %s) " % (x, y)
    if standing:
        transform += INVERSE + " "
    transform += "scale(%s)" % scale
    return '<g transform="%s"><use xlink:href="#%s"/></g>' % (transform, name)


def _defs(c):
    return f'''<defs>
      <g id="cloud" fill="{c['side']}" stroke-width="1.35">
        <path d="M-20 0 C-34-3-31-23-20-23 C-18-36-5-39 3-28 C17-35 28-22 26-12 C38 1 22 7 14 3 Z"/>
        <path d="M-24-4 C-33-11-28-26-17-23 C-13-37 2-31 5-20 C17-25 23-11 20-4 L14 3 Z" fill="{c['surface']}"/>
      </g>
      <g id="server" stroke-width="1.3" stroke-linejoin="round">
        <path d="M-25-42 3-56 27-42 0-27Z" fill="{c['surface']}"/>
        <path d="M-25-42 0-27 0 7-25-8Z" fill="{c['side']}"/>
        <path d="M0-27 27-42 27-8 0 7Z" fill="{c['muted']}" fill-opacity=".3"/>
        <path d="M-25-31 0-16 27-31 M-25-20 0-5 27-20" fill="none"/>
        <path d="M-20-34-12-29 M-20-23-12-18 M-20-12-12-7 M6-24 20-32 M6-13 20-21 M6-2 20-10" fill="none"/>
        <path d="M-6-26-4-25 M-6-15-4-14 M-6-4-4-3" fill="none" stroke-width="2"/>
      </g>
      <g id="folder" fill="{c['side']}" stroke-width="1.5">
        <path d="M-18-12 H-5 L0-5 H19 V12 H-18Z"/>
      </g>
      <g id="browser" stroke-width="1.4" stroke-linejoin="round">
        <path d="M-27-56 Q-27-61-22-58 L26-31 Q29-29 29-24 V17 L-27-15Z" fill="{c['side']}"/>
        <path d="M-23-52 25-25 V12 L-23-15Z" fill="{c['surface']}"/>
        <path d="M-23-42 25-15" fill="none"/>
        <path d="M-18-48-17-47 M-12-45-11-44 M-6-41-5-40" stroke-width="2"/>
      </g>
      <g id="user" stroke-width="1.4">
        <ellipse cy="4" rx="20" ry="10" fill="{c['side']}"/>
        <ellipse rx="20" ry="10" fill="{c['surface']}"/>
        <path d="M-13-2 Q-15-16-6-20 H6 Q15-16 13-2 Q0 8-13-2Z" fill="{c['accent']}"/>
        <path d="M-9-32 Q-9-43 0-43 Q12-42 10-30 L6-17 Q0-12-6-19Z" fill="{c['surface']}"/>
        <path d="M-9-29 Q-13-43-2-45 Q14-48 12-29 L6-30 6-36 Q-1-30-6-35 L-6-27Z" fill="{c['muted']}"/>
      </g>
      <g id="database" stroke-width="1.4" fill="{c['side']}">
        <path d="M-19-30 V3 C-19 17 19 17 19 3 V-30Z"/>
        <path d="M-19-19 C-19-5 19-5 19-19 M-19-8 C-19 6 19 6 19-8" fill="none"/>
        <ellipse cy="-30" rx="19" ry="11" fill="{c['surface']}"/>
        <path d="M11-13 13-14 M11-2 13-3 M11 9 13 8" stroke-width="2"/>
      </g>
      <g id="check" fill="none" stroke-width="4" stroke-linecap="round" stroke-linejoin="round">
        <path d="M-9 0-2 7 11-9"/>
      </g>
      <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto">
        <path d="M1 1 9 5 1 9 3 5Z" fill="{c['accent']}" stroke="{c['muted']}" stroke-width=".6"/>
      </marker>
    </defs>'''


def _gear(x, y, radius):
    points = []
    for index in range(48):
        angle = math.tau * index / 48
        r = radius * (1 if index % 4 in (1, 2) else .81)
        points.append("%.2f,%.2f" % (x + r * math.cos(angle), y + r * math.sin(angle)))
    return ('<polygon points="%s"/><ellipse cx="%s" cy="%s" rx="%s" ry="%s" fill="none"/>'
            % (" ".join(points), x, y, radius * .48, radius * .48))


def _content(name, card, p):
    c, labels = p["palette"], card["labels"]
    title = _text(17, 29, card["title"], 25, True, fit=card["size"] - 28)
    if name == "environments":
        return title + ''.join([
            _use("server", 50, 66, .85, True),
            _use("server", 50, 111, .85, True),
            _use("cloud", 145, 83, .9, True),
            _use("cloud", 145, 158, .9, True),
            _text(115, 112, labels[0], 14, fit=75),
            _text(112, 184, labels[1], 14, fit=80),
            _use("folder", 47, 153, .65),
            _text(25, 184, labels[2], 14, fit=75),
        ])
    if name == "logs":
        lines = []
        for index in range(12):
            y = 54 + index * 8
            lines.append('<path d="M65 %s H%s M%s %s H178"/>'
                         % (y, 96 + (index * 17) % 53, 146 + (index * 7) % 20, y))
        return (title + '<rect x="52" y="39" width="137" height="137" rx="4" fill="none"/>'
                + '<g fill="none" stroke-width="1.1">' + ''.join(lines) + '</g>'
                + _text(65, 166, labels[1], 12, fit=110)
                + _text(17, 192, labels[0], 15, fit=160))
    if name == "scenarios":
        parts = [title, _text(19, 56, labels[0], 18, fit=215)]
        for step in range(1, 4):
            y = 74 + (step - 1) * 56
            active = step == p["active_step"]
            fill = c["accent"] if active else c["surface"]
            parts += [
                '<g id="step-%d">' % step,
                '<rect x="19" y="%s" width="227" height="45" rx="5" fill="%s" fill-opacity=".24" stroke="none"/>'
                % (y - 2, c["accent"] if active else c["surface"]),
                '<rect x="24" y="%s" width="42" height="37" rx="4" fill="%s"/>' % (y + 6, c["side"]),
                '<rect x="24" y="%s" width="42" height="37" rx="4" fill="%s"/>' % (y + 1, fill),
                _use("check", 45, y + 20, .7),
                _text(79, y + 27, labels[step], 18, fit=160), '</g>',
            ]
        return ''.join(parts)
    if name == "sessions":
        return title + ''.join([
            _text(18, 57, labels[0], 16, fit=120),
            _text(18, 79, labels[1], 16, fit=120),
            '<path d="M70 104 H149 M70 176 H149" fill="none" stroke-width="1"/>',
            _use("browser", 146, 98, 1, True),
            _use("browser", 146, 157, 1, True),
            _use("user", 66, 119, .9, True),
            _use("user", 66, 189, .9, True),
        ])
    if name == "reports":
        parts = [title, _text(18, 58, labels[0], 16, fit=110),
                 _text(18, 81, "%s: %g%%" % (labels[1], p["pass_rate"]), 16, fit=110),
                 '<path d="M134 39 V105 H194" fill="none"/>']
        for index, height in enumerate((22, 37, 60)):
            parts.append('<rect x="%s" y="%s" width="13" height="%s" fill="%s"/>'
                         % (142 + index * 18, 98 - height, height,
                            c["accent"] if index == 2 else c["side"]))
        # The large pie tracks pass_rate, including the 0% and 100% endpoints.
        for x, y, fraction in ((46, 165, .12), (111, 165, .28), (176, 165, p["pass_rate"] / 100)):
            parts.append('<circle cx="%s" cy="%s" r="23" fill="%s"/>' % (x, y, c["side"]))
            if fraction == 1:
                parts.append('<circle cx="%s" cy="%s" r="23" fill="%s"/>' % (x, y, c["accent"]))
            elif fraction > 0:
                end = -math.pi / 2 + math.tau * fraction
                parts.append('<path d="M%s %s V%s A23 23 0 %d 1 %.3f %.3fZ" fill="%s"/>'
                             % (x, y, y - 23, fraction > .5,
                                x + 23 * math.cos(end), y + 23 * math.sin(end), c["accent"]))
        return ''.join(parts)
    return title + ''.join([
        '<g fill="%s" stroke-width="1.4">' % c["side"],
        _gear(137, 63, 25), _gear(111, 102, 16), _gear(157, 109, 20), '</g>',
        _text(18, 149, labels[0], 16, fit=170),
        _text(18, 175, labels[1], 16, fit=150),
        _use("server", 161, 172, .9, True),
        _use("database", 110, 190, .9, True),
    ])


def _card(name, p):
    card, c = p["cards"][name], p["palette"]
    # Geometry and contents scale together. Connectors use these same anchors.
    scale = card["scale"] * card["size"] / (260 if name == "scenarios" else 200 if name in ("logs", "environments") else 210)
    size = 260 if name == "scenarios" else 200 if name in ("logs", "environments") else 210
    rect = '<rect width="%s" height="%s" rx="12"/>' % (size, size)
    return (f'<g id="{name}" transform="translate({card["x"]} {card["y"]}) scale({scale})">'
            f'<g transform="translate(0 29) {PLANE}" fill="{c["ink"]}" opacity=".17" stroke="none">{rect}</g>'
            f'<g transform="translate(0 13) {PLANE}" fill="{c["side"]}">{rect}</g>'
            f'<g transform="{PLANE}" fill="{c["surface"]}">{rect}'
            f'<g fill="{c["ink"]}" stroke="none">'
            # Text inherits no stroke; icons restore the outline below.
            f'<g stroke="{c["ink"]}">{_content(name, card, p)}</g></g></g></g>')


def _connectors(p):
    def anchor(name, x, y):
        card = p["cards"][name]
        s = card["size"] * card["scale"]
        return (card["x"] + .83 * (x + y) * s,
                card["y"] + .49 * (y - x) * s + 5)

    connections = [
        ("environments", (0, .78), "sessions", (.72, 0)),
        ("environments", (.3, 1), "scenarios", (.47, 0)),
        ("sessions", (1, .67), "scenarios", (0, .4)),
        ("scenarios", (1, .6), "logs", (0, .43)),
        ("scenarios", (.65, 1), "reports", (.42, 0)),
        ("scenarios", (.1, 1), "services", (.68, 0)),
        ("services", (1, .68), "reports", (0, .42)),
    ]
    paths = []
    for source, start, target, end in connections:
        x1, y1 = anchor(source, *start)
        x2, y2 = anchor(target, *end)
        paths.append('<path d="M%g %g L%g %g"/>' % (x1, y1, x2, y2))
    return ('<g id="connections" fill="none" stroke="%s" stroke-width="1.5" marker-end="url(#arrow)">%s</g>'
            % (p["palette"]["accent"], ''.join(paths)))


def render_svg(overrides=None):
    """Return a standalone SVG. Nested overrides merge into the JSON defaults.

    Example: render_svg({"pass_rate": 88, "palette": {"accent": "#79b8a0"}}).
    IDs on cards, steps and connections also allow direct SVG editing.
    """
    p = _validated(overrides)
    c = p["palette"]
    grid = []
    for offset in range(-1100, 1201, 26):
        grid.append('<path d="M-100 %d l1224 723 M-100 %d l1224-723"/>' % (offset, offset + 723))
    svg = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        'width="%s" height="%s" viewBox="0 0 1024 1024" role="img" aria-labelledby="art-title art-description">'
        % (p["width"], p["height"]),
        '<title id="art-title">%s</title>' % escape(p["title"]),
        '<desc id="art-description">%s</desc>' % escape(p["subtitle"]),
        _defs(c),
        '<rect width="1024" height="1024" fill="%s"/>' % c["background"],
        '<g id="grid" stroke="%s" stroke-width=".6" opacity="%s">%s</g>'
        % (c["grid"], p["grid_opacity"], ''.join(grid)),
        # Clear space behind the heading, without filters unsupported by Qt SVG.
        '<rect width="1024" height="245" fill="%s"/>' % c["background"],
        '<g font-family="DejaVu Sans,Arial,sans-serif" fill="%s">' % c["ink"],
        '<g id="heading">', _text(58, 145, p["title"], 98, fit=900),
        _text(59, 203, p["subtitle"], 41, fit=900), '</g>',
        _connectors(p),
        '<g id="cards" stroke="%s" stroke-width="1.65" stroke-linejoin="round">' % c["ink"],
    ]
    svg.extend(_card(name, p) for name in ("environments", "logs", "sessions", "scenarios", "reports", "services"))
    svg.extend(['</g></g></svg>', ''])
    return '\n'.join(svg)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="JSON with full or partial parameter overrides")
    parser.add_argument("--output", type=Path, required=True, help="Output SVG file")
    args = parser.parse_args(argv)
    try:
        overrides = json.loads(args.config.read_text(encoding="utf-8")) if args.config else None
        svg = render_svg(overrides)
        args.output.write_text(svg, encoding="utf-8")
    except (ValueError, OSError) as error:
        parser.exit(2, "%s\n" % error)
    print(args.output)


if __name__ == "__main__":
    main()
