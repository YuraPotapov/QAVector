"""What a step is doing, as stages down the page.

A step that takes half a minute needs to say something while it runs. An agent
step used to say one thing - the JSON blob it finished with, printed into the
output as a single unreadable line - which told a reader nothing about the work
and arrived only once the work was over.

So the core emits a stage per turn (``cycle.step.stage``, see
``cycle/plugins/agent_stream.py``) and this draws them the way they happened,
newest at the bottom::

    o  Thinking
    |
    o  Read main.py
    |
    o  Answered

Two things this deliberately is not. It is **not a log view**: a stage is a row
somebody reads, with a title and an optional body, and the text a process
printed is still in the Output tab beside it. And it is **not specific to
agents**: it renders whatever stages a step emits, so a plugin that later grows
phases of its own gets this for free.

The marker column is painted, never typed. ``test_theme`` walks every module
under ``gui/cms_gui`` and fails on a literal character above U+2100, which is
the right rule: a glyph that depends on the font is not a drawing. The dot and
the line between dots are a ``QPainter`` call each.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QScrollArea, QVBoxLayout, QWidget)

from . import icons, theme

#: Everything the column looks like, in one place - the habit ``cyclegraph.py``
#: keeps, and for the same reason: a redesign changes values here rather than
#: hunting through paint code.
SPEC = {
    "marker": {"width": 26, "dot": 7, "ring": 2, "line": 1},
    "row": {"pad_x": 10, "pad_y": 6, "gap": 2},
    "title": {"size": 12},
    "detail": {"size": 11, "lines": 14},
}

#: Which colour a kind of stage is drawn in, by ``theme`` attribute name so the
#: value is read at paint time - ``theme.set_dark_mode`` rewrites those globals
#: in place, and a colour captured at import is the light one forever.
LOOK = {
    "start": "NEUTRAL500",
    "thinking": "NEUTRAL500",
    "tool": "ACCENT",
    "tool_result": "NEUTRAL400",
    "text": "ACCENT",
    "review": "ACCENT",
    # A gate is a verdict, so its row is coloured like one: green when every
    # check held, and red through the failed status every other row uses.
    "gate": "OK",
    "result": "OK",
}

#: How a review's own verdict is coloured, by the same rule its risk is read.
RISK = {"low": "OK", "medium": "WARN", "high": "BAD", "unknown": "NEUTRAL500"}

#: And an issue's severity, which has no "unknown".
SEVERITY = {"low": "NEUTRAL600", "medium": "WARN", "high": "BAD"}

#: A stage that says it failed is drawn as one whatever kind it is.
FAILED = "BAD"
RUNNING = "ACCENT"

#: How far off the bottom still counts as being at it. A scroll bar rounds, and
#: one pixel short must not read as "the reader has scrolled away to read
#: something".
SLACK = 2

#: How many rows the model may have dropped off the front between two redraws
#: before rebuilding is simpler than working out what moved. The model only
#: trims once it is full (``runner.CYCLE_STAGES``) and the page redraws every
#: turn of the event loop, so in a real run this is nought or one.
MAX_SHIFT = 64

#: What the panel says when there is nothing in it. A sentence rather than a
#: blank, because most steps emit no stages at all and an empty panel has to
#: say which of the two it is.
EMPTY = ("Nothing has reported a stage yet. Steps that work in phases - an "
         "agent review - show them here.")


def ink(name):
    """One colour by name, read now rather than when this module was imported."""
    if name.startswith("NEUTRAL"):
        return theme.NEUTRAL[int(name[len("NEUTRAL"):])]
    return getattr(theme, name)


def colour_of(stage):
    """The colour one stage's marker is drawn in."""
    if stage.get("status") == "failed":
        return ink(FAILED)
    if stage.get("status") == "running":
        return ink(RUNNING)
    if stage.get("kind") == "review":
        # A review's row is its verdict: a high-risk one should be findable
        # without reading a word of it.
        return ink(RISK.get((stage.get("body") or {}).get("risk"), "NEUTRAL500"))
    return ink(LOOK.get(stage.get("kind", ""), "NEUTRAL500"))


class _Marker(QWidget):
    """The dot, and the line joining it to the dots above and below it."""

    def __init__(self, stage, first, last, parent=None):
        super().__init__(parent)
        self._stage = stage
        self._first = first
        self._last = last
        self.setFixedWidth(SPEC["marker"]["width"])

    def set_ends(self, first=None, last=None):
        """Whether the lines above and below this dot are drawn; None keeps one.

        Which row is last is not known when a row is built - a stage is drawn
        the moment it is reported - so the one that *was* last is told it is not
        any more rather than rebuilt for the sake of one line. The same happens
        at the top when the model trims its oldest stages away.
        """
        changed = False
        for name, value in (("_first", first), ("_last", last)):
            if value is not None and getattr(self, name) != bool(value):
                setattr(self, name, bool(value))
                changed = True
        if changed:
            self.update()

    def paintEvent(self, _event):             # noqa: N802 - Qt's own spelling
        spec = SPEC["marker"]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        middle = self.width() / 2.0
        centre = SPEC["row"]["pad_y"] + SPEC["title"]["size"] / 2.0 + 2

        # NEUTRAL400, not 300: the panel's own ground is already a light grey,
        # and a 300 line on it is invisible - which makes the column a set of
        # loose dots rather than a sequence.
        painter.setPen(QColor(ink("NEUTRAL400")))
        if not self._first:
            painter.drawLine(int(middle), 0, int(middle), int(centre))
        if not self._last:
            painter.drawLine(int(middle), int(centre), int(middle), self.height())

        colour = QColor(colour_of(self._stage))
        radius = spec["dot"] / 2.0
        # A running stage is drawn hollow: it is the one that has not happened
        # yet, and a reader should be able to find it without reading a word.
        if self._stage.get("status") == "running":
            painter.setBrush(QColor(theme.BG))
            pen = painter.pen()
            pen.setColor(colour)
            pen.setWidth(spec["ring"])
            painter.setPen(pen)
        else:
            painter.setBrush(colour)
            painter.setPen(Qt.NoPen)
        painter.drawEllipse(int(middle - radius), int(centre - radius),
                            spec["dot"], spec["dot"])
        painter.end()


class StageRow(QWidget):
    """One stage: its marker, its title, and its body when it has one."""

    def __init__(self, stage, first=False, last=False, step_id="", parent=None,
                 label=""):
        super().__init__(parent)
        self.stage = dict(stage)
        self.step_id = step_id
        self.label = label
        row = QVBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)

        across = QHBoxLayout()
        across.setContentsMargins(0, 0, 0, 0)
        across.setSpacing(0)
        self.marker = _Marker(self.stage, first, last)
        across.addWidget(self.marker, 0)

        body = QWidget()
        column = QVBoxLayout(body)
        column.setContentsMargins(SPEC["row"]["pad_x"], SPEC["row"]["pad_y"],
                                  SPEC["row"]["pad_x"], SPEC["row"]["pad_y"])
        column.setSpacing(SPEC["row"]["gap"])

        title = self.stage.get("title", "")
        if step_id:
            # The step's own words when the graph has any: a row headed
            # "Is this ours to do" says which step this is, where "admit" says
            # what the id happens to be spelt like.
            title = "%s  %s" % (label or step_id, title)
        self.title = QLabel(title)
        self.title.setWordWrap(True)
        self.title.setStyleSheet(
            "font-size: %dpx; font-weight: 600; color: %s;"
            % (SPEC["title"]["size"], colour_of(self.stage)))
        column.addWidget(self.title)

        detail = (self.stage.get("detail") or "").strip()
        if self.stage.get("kind") == "gate":
            # Every check on its own line with the mark that says which way it
            # went. The joined text the gate also sends could only say what was
            # compared, which left a reader matching the heading against four
            # identical-looking lines to find the one that refused.
            self.detail = None
            for widget in _gate_widgets(self.stage, detail):
                column.addWidget(widget)
        elif self.stage.get("kind") == "review":
            # The step's actual answer. It arrives as JSON because that is what
            # the step asked for, and printed as it stands it is one long line
            # nobody reads - so it is laid out instead.
            self.detail = None
            for widget in _review_widgets(self.stage, detail):
                column.addWidget(widget)
        elif detail:
            self.detail = QLabel(_trimmed(detail))
            self.detail.setWordWrap(True)
            self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.detail.setStyleSheet(
                "font-family: %s; font-size: %dpx; color: %s;"
                % (theme.MONO_CSS, SPEC["detail"]["size"], ink("NEUTRAL600")))
            column.addWidget(self.detail)
        else:
            self.detail = None

        across.addWidget(body, 1)
        row.addLayout(across)


def _paragraph(text, colour, size=None, mono=False, indent=0):
    """One block of text in the body of a row."""
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    label.setStyleSheet("%sfont-size: %dpx; color: %s;%s"
                        % ("font-family: %s; " % theme.MONO_CSS if mono else "",
                           size or SPEC["detail"]["size"], colour,
                           " margin-left: %dpx;" % indent if indent else ""))
    return label


def _heading(text):
    label = QLabel(text.upper())
    label.setStyleSheet("font-size: 10px; font-weight: 600; letter-spacing: 1px;"
                        " color: %s; margin-top: 4px;" % ink("NEUTRAL500"))
    return label


def _gate_widgets(stage, detail):
    """A gate laid out: one line per check, with the mark that says how it went.

    In a grid rather than as free lines, because the three columns - the mark,
    what the check is called, what was actually compared - only read as columns
    when they line up. A gate with no structured checks (an older core) falls
    back to the text it sent, which is what it used to show.
    """
    checks = [one for one in ((stage.get("body") or {}).get("checks") or [])
              if isinstance(one, dict)]
    if not checks:
        return [_paragraph(detail, ink("NEUTRAL700"),
                           size=SPEC["detail"]["size"] + 1)] if detail else []

    box = QWidget()
    grid = QGridLayout(box)
    grid.setContentsMargins(8, 2, 0, 0)
    grid.setHorizontalSpacing(8)
    grid.setVerticalSpacing(2)
    # The name gets the room it needs before the comparison does: "within its
    # budget" wrapping onto two lines to keep a short expression on one is the
    # wrong way round.
    grid.setColumnStretch(1, 2)
    grid.setColumnStretch(2, 3)
    for row, check in enumerate(checks):
        held = bool(check.get("held"))
        colour = ink("OK" if held else "BAD")
        mark = QLabel()
        # Painted, never typed: ``test_theme`` refuses a literal tick, and a
        # glyph that depends on the font is not a drawing.
        mark.setPixmap(icons.pixmap("pass" if held else "fail",
                                    SPEC["detail"]["size"], colour))
        mark.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        grid.addWidget(mark, row, 0, Qt.AlignTop)
        grid.addWidget(_paragraph(str(check.get("name", "")), colour),
                       row, 1, Qt.AlignTop)
        grid.addWidget(_paragraph(str(check.get("account", "")),
                                  ink("NEUTRAL600"), mono=True),
                       row, 2, Qt.AlignTop)
    return [box]


def _review_widgets(stage, summary):
    """A review laid out: the verdict, what it said, what it found, what to do.

    Built from the fields the core parsed and validated, so a row can only
    claim to be a review when it is one - see ``cycle/plugins/agent_stream.py``.
    """
    body = stage.get("body") or {}
    widgets = []
    if summary:
        widgets.append(_paragraph(summary, ink("NEUTRAL700"),
                                  size=SPEC["detail"]["size"] + 1))

    issues = [one for one in (body.get("issues") or []) if isinstance(one, dict)]
    if issues:
        widgets.append(_heading("Issues"))
        for issue in issues:
            widgets.append(_paragraph(_issue_line(issue),
                                      ink(SEVERITY.get(issue.get("severity"),
                                                       "NEUTRAL600")),
                                      indent=8))

    advice = [str(one) for one in (body.get("recommendations") or []) if one]
    if advice:
        widgets.append(_heading("Recommendations"))
        for one in advice:
            widgets.append(_paragraph("- " + one, ink("NEUTRAL600"), indent=8))
    return widgets


def _issue_line(issue):
    """``high  login.py:42  MD5 without a salt`` - severity, where, what."""
    where = str(issue.get("file") or "").strip()
    line = issue.get("line")
    if where and isinstance(line, int):
        where = "%s:%d" % (where, line)
    head = "  ".join(part for part in (str(issue.get("severity") or ""), where)
                     if part)
    return "%s  %s" % (head, issue.get("description", "")) if head \
        else str(issue.get("description", ""))


def _trimmed(text):
    """A body cut to a readable height. The whole of it is in the step's log."""
    lines = text.splitlines()
    limit = SPEC["detail"]["lines"]
    if len(lines) <= limit:
        return text
    return "\n".join(lines[:limit] + ["... %d more lines" % (len(lines) - limit)])


class StageList(QScrollArea):
    """Every stage, top to bottom, following the newest as they arrive.

    **Appended to, not rebuilt.** This used to clear the column and build every
    row again on each change, on the reasoning that a run emits only a few
    hundred stages. That reasoning was about the size of the list and missed
    how often it is redrawn: the page repaints on every event of a run - a
    stage, but also every batch of a step's output - so drawing n stages cost
    the sum of 1..n rows, each one a widget tree with a stylesheet to parse.
    A long cycle spent its time rebuilding rows that had not changed, and the
    window stopped answering.

    So a redraw that is the same rows plus some new ones at the end - which is
    what a running cycle produces, over and over - adds only the new ones. The
    model appends at the back and trims at the front once it is full, so the
    rows on screen are always a contiguous block of what arrives; anything else
    (another scope, a theme change) falls back to the wholesale rebuild.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        # Its own ground, like the text views it sits beside in the same tab
        # strip. Without it the list borrows the page behind it and reads as
        # part of the furniture rather than as the panel's content.
        self.setStyleSheet("QScrollArea { background: %s; }" % theme.BG)
        self.viewport().setAutoFillBackground(True)
        self._rows = []
        # The entries that produced those rows, so the next call can tell a
        # step on from a different list. Identity is what is compared: these
        # are the very objects ``RunState`` keeps, so a stage that is still the
        # same stage is still the same object.
        self._drawn = []
        self._named = False
        self._labels = {}
        # Follow the newest row while the reader is at the bottom, and stop the
        # moment they scroll up. A run emits stages for half a minute, and a
        # view that yanks you back down every time one arrives is unreadable
        # exactly when somebody is trying to read it.
        self._follow = True
        bar = self.verticalScrollBar()
        bar.valueChanged.connect(self._moved)
        # The range grows one layout pass AFTER the rows go in, so scrolling to
        # the bottom straight after inserting them lands on the old maximum -
        # which is why this waits to be told the range changed instead.
        bar.rangeChanged.connect(self._grew)
        self._body = QWidget()
        self._body.setAutoFillBackground(True)
        self._body.setStyleSheet("background: %s;" % theme.BG)
        self._column = QVBoxLayout(self._body)
        self._column.setContentsMargins(6, 6, 6, 6)
        self._column.setSpacing(0)
        self._column.addStretch(1)
        self.setWidget(self._body)
        self._empty = QLabel("")
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setStyleSheet("color: %s; font-size: 11px;"
                                  % ink("NEUTRAL500"))
        self._column.insertWidget(0, self._empty)
        self.set_stages([])

    def set_stages(self, stages, named=False, labels=None):
        """Show these. ``named`` puts each row's step in front of its title.

        ``labels`` is what the graph calls each step, by id. A step that is not
        in it - or a caller that has no graph to hand - is named by its id, the
        way every row was before.
        """
        incoming = list(stages or [])
        named = bool(named)
        labels = dict(labels or {})
        moved = (self._shift(incoming)
                 if named == self._named and labels == self._labels else None)
        if moved is None:
            self._rebuild(incoming, named, labels)
        else:
            self._extend(incoming, moved)
        self._drawn = incoming
        self._empty.setText("" if incoming else EMPTY)
        self._empty.setVisible(not incoming)
        self._to_bottom()

    def _shift(self, incoming):
        """How many drawn rows ``incoming`` has left behind, or None.

        None means this is not the same list gone on: a different step's
        stages, a reordering, a shorter list. The caller rebuilds instead.

        Only the two ends are compared rather than every row. The model can do
        exactly two things to this list - append at the back, drop from the
        front - and neither can change a row in the middle while leaving both
        ends where they were.
        """
        if not self._drawn:
            return 0
        if not incoming:
            return None
        for dropped in range(min(len(self._drawn), MAX_SHIFT)):
            if self._drawn[dropped] is not incoming[0]:
                continue
            kept = len(self._drawn) - dropped
            if kept > len(incoming):
                return None       # the list was cut short, not gone on
            return dropped if self._drawn[-1] is incoming[kept - 1] else None
        return None

    def _rebuild(self, incoming, named, labels):
        """Every row again, from nothing. The fallback, not the usual path."""
        self.clear()
        self._named = named
        self._labels = labels
        for index, entry in enumerate(incoming):
            self._add(entry, first=index == 0, last=index == len(incoming) - 1)

    def _extend(self, incoming, dropped):
        """Forget ``dropped`` rows off the front, then draw whatever is new."""
        self._forget(dropped)
        if self._rows:
            # Whatever the model trimmed down to heads the column now, and the
            # row that was last has something below it to be joined to.
            self._rows[0].marker.set_ends(first=True)
            if len(incoming) > len(self._rows):
                self._rows[-1].marker.set_ends(last=False)
        for index in range(len(self._rows), len(incoming)):
            self._add(incoming[index], first=index == 0,
                      last=index == len(incoming) - 1)

    def _add(self, entry, first, last):
        """One row at the bottom of the column, above the stretch."""
        step_id, stage = entry if isinstance(entry, tuple) else ("", entry)
        row = StageRow(stage, first=first, last=last,
                       step_id=step_id if self._named else "",
                       label=self._labels.get(step_id, ""))
        self._rows.append(row)
        self._column.insertWidget(self._column.count() - 1, row)

    def _forget(self, count):
        """Take the first ``count`` rows off the column and out of the layout."""
        for row in self._rows[:count]:
            self._column.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        del self._rows[:count]

    def clear(self):
        self._forget(len(self._rows))
        self._drawn = []

    def count(self):
        return len(self._rows)

    def titles(self):
        """What the rows say, for a test to read without walking the layout."""
        return [row.stage.get("title", "") for row in self._rows]

    def _moved(self, value):
        """Whether the reader is still at the bottom, after every scroll."""
        self._follow = value >= self.verticalScrollBar().maximum() - SLACK

    def _grew(self, _lowest, highest):
        if self._follow:
            self.verticalScrollBar().setValue(highest)

    def _to_bottom(self):
        if self._follow:
            bar = self.verticalScrollBar()
            bar.setValue(bar.maximum())

    def at_bottom(self):
        """Whether the newest row is being followed. For a test to read."""
        return self._follow

    def restyle(self):
        """Repaint in the current theme. Called where icons.clear_cache() is."""
        self.set_stages([(row.step_id, row.stage) for row in self._rows],
                        named=self._named, labels=self._labels)
