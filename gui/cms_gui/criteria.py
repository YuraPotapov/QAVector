"""What a service's own log says about where it has got to.

The Services page can say whether a service's **process** is up, and that is the
weakest useful claim: a server whose port is already taken is running, and so
is one that booted cleanly. What separates them is in the log the service writes -
``started localhost:8069``, ``tests passed``, a ``CRITICAL`` line.

A **criterion** is a name somebody chose, a colour, and the logic that lights it.
It is shown beside the service and does nothing else: it never changes the status,
never gates what waits for what, never stops anything. STATUS means the process
and this means the log, and neither pretends to be the other.

**The rules are about the whole log, not about one line.** ``grep "started" &&
grep "!ERRORS"`` asks whether the log contains one thing and never contains the
other; matching both against a single line would be a different and useless
question, since no line contains ``started localhost:8069`` and the absence of
``ERRORS`` at once. So a :class:`Matcher` carries state across the run:

* a ``match`` rule is satisfied once **some** line has matched it, and stays so;
* an ``exclude`` rule is satisfied while **no** line has matched it, and is
  poisoned permanently by the first that does.

A criterion is *lit* when every match rule has been seen and no exclude rule has
been violated - which means it can go dark again. ``start`` stops being true the
moment a ``CRITICAL`` line arrives, and that is the honest reading of the notation
as well as the useful one. The state is reset when the service starts, so what is
shown always describes the current run.

Matching is case-sensitive, like grep. A regex rule can open with ``(?i)``.

Nothing here imports Qt: it is tables and matching, so it can be tested without a
widget - the same split :mod:`~cms_gui.runnertypes` makes.
"""

import re

from . import theme

#: Whether a rule says the log must contain this, or must not.
MATCH = "match"
EXCLUDE = "exclude"
MODES = (MATCH, EXCLUDE)

#: How a pattern is read. Plain text is the common case and needs no escaping;
#: regex is the escape hatch, and the only way to say "either of these".
TEXT = "text"
REGEX = "regex"
KINDS = (TEXT, REGEX)

#: What each mode and kind is called in the form, and what it means there.
MODE_LABELS = {MATCH: "must contain", EXCLUDE: "must not contain"}
KIND_LABELS = {TEXT: "text", REGEX: "regex"}

#: Colour names, and where each one's value comes from. Stored by *name* and
#: resolved when it is painted, never captured at import: ``theme`` rewrites its
#: palette in place when dark mode is set, and a value read once would go on
#: painting the light one forever - which is what ``widgets.Tag`` did until it
#: read its colours when set (``widgets.Tag.styles()``).
#:
#: Amber and red are the theme's own WARN and BAD, which are already one value
#: for both modes. Green and blue are named here because the theme has no green:
#: its palette is a blueprint one and ``theme.OK`` is a slate blue, so a
#: criterion someone coloured "green" would have come out blue - the interface
#: lying about the one thing it was asked to remember. These two sit in the same
#: lightness band as WARN and BAD, which is what makes one value legible on the
#: light background, the dark one, and the selected row's band alike.
GREEN = "#3f8547"
BLUE = "#3a6ea5"

COLORS = ("green", "amber", "red", "blue", "grey")
_COLOR_SOURCES = {
    "green": lambda: GREEN,
    "amber": lambda: theme.WARN,
    "red": lambda: theme.BAD,
    "blue": lambda: BLUE,
    "grey": lambda: theme.NEUTRAL[600],
}
DEFAULT_COLOR = "green"

#: What an unlit criterion is drawn in - the same for every colour, because the
#: point of unlit is that it is not yet its own colour.
def unlit_color():
    return theme.NEUTRAL[500]


def color_of(name):
    """The colour a criterion paints in, read from the theme as it is now."""
    return _COLOR_SOURCES.get(name, _COLOR_SOURCES[DEFAULT_COLOR])()


RULE_KEYS = ("mode", "kind", "pattern")
CRITERION_KEYS = ("name", "color", "source", "rules")


class Rule:
    """One condition on the log, plus whatever unknown keys came with it."""

    def __init__(self, mode=MATCH, kind=TEXT, pattern="", extra=None):
        self.mode = mode if mode in MODES else MATCH
        self.kind = kind if kind in KINDS else TEXT
        self.pattern = pattern or ""
        self.extra = dict(extra or {})

    @classmethod
    def from_entry(cls, entry):
        if not isinstance(entry, dict):
            raise ValueError("every rule must be a JSON object, got %r" % (entry,))
        return cls(mode=entry.get("mode", MATCH),
                   kind=entry.get("kind", TEXT),
                   pattern=entry.get("pattern", "") or "",
                   extra={k: v for k, v in entry.items() if k not in RULE_KEYS})

    def to_entry(self):
        entry = dict(self.extra)
        entry["mode"] = self.mode
        entry["kind"] = self.kind
        entry["pattern"] = self.pattern
        return entry

    def describe(self):
        return "%s %s %r" % (MODE_LABELS[self.mode], KIND_LABELS[self.kind],
                             self.pattern)

    def copy(self):
        return Rule(self.mode, self.kind, self.pattern, dict(self.extra))


class CriterionRow:
    """One named thing to watch a service's log for."""

    def __init__(self, name="", color=DEFAULT_COLOR, source="", rules=(),
                 extra=None):
        self.name = name
        self.color = color if color in COLORS else DEFAULT_COLOR
        # Blank means the service's own output. A path means that file instead,
        # for a backend that logs somewhere rather than to its console - which is
        # what any server started with a logfile does.
        self.source = source or ""
        self.rules = list(rules)
        self.extra = dict(extra or {})

    @classmethod
    def from_entry(cls, entry):
        if not isinstance(entry, dict):
            raise ValueError("every criterion must be a JSON object, got %r"
                             % (entry,))
        rules = entry.get("rules", [])
        if not isinstance(rules, list):
            raise ValueError("criterion %r: 'rules' must be a JSON array."
                             % (entry.get("name", "?"),))
        return cls(name=entry.get("name", "") or "",
                   color=entry.get("color", DEFAULT_COLOR) or DEFAULT_COLOR,
                   source=entry.get("source", "") or "",
                   rules=[Rule.from_entry(rule) for rule in rules],
                   extra={k: v for k, v in entry.items()
                          if k not in CRITERION_KEYS})

    def to_entry(self):
        entry = dict(self.extra)
        entry["name"] = self.name
        entry["color"] = self.color
        if self.source:
            entry["source"] = self.source
        entry["rules"] = [rule.to_entry() for rule in self.rules]
        return entry

    def watches(self):
        """What this reads, for the overview column."""
        return self.source or "its own output"

    def summary(self):
        """The rules on one line, for the table in the runner's form."""
        return " · ".join(rule.describe() for rule in self.rules)

    def copy(self):
        return CriterionRow(self.name, self.color, self.source,
                            [rule.copy() for rule in self.rules],
                            dict(self.extra))


def problems(criterion, where="criterion"):
    """Everything wrong with one criterion, as messages. Never raises."""
    found = []
    if not criterion.name.strip():
        found.append("%s: a name is required." % where)
    if criterion.color not in COLORS:
        found.append("%s: unknown colour %r. Known: %s."
                     % (where, criterion.color, ", ".join(COLORS)))
    if not criterion.rules:
        found.append("%s: add at least one rule - a criterion with none would "
                     "never mean anything." % where)
    for index, rule in enumerate(criterion.rules, start=1):
        spot = "%s, rule %d" % (where, index)
        if rule.mode not in MODES:
            found.append("%s: unknown mode %r. Known: %s."
                         % (spot, rule.mode, ", ".join(MODES)))
        if rule.kind not in KINDS:
            found.append("%s: unknown kind %r. Known: %s."
                         % (spot, rule.kind, ", ".join(KINDS)))
        if not rule.pattern.strip():
            found.append("%s: a pattern is required." % spot)
        elif rule.kind == REGEX:
            # A pattern that will not compile is not a typo anything downstream
            # can survive, and it is worth catching while it is being typed -
            # the same rule logsourcesfile applies to a custom log format.
            try:
                re.compile(rule.pattern)
            except re.error as exc:
                found.append("%s: regex does not compile (%s)." % (spot, exc))
    return found


class _CompiledRule:
    """One rule, ready to be asked about a line."""

    def __init__(self, rule):
        self.mode = rule.mode
        self.pattern = rule.pattern
        self._regex = None
        self._broken = False
        if rule.kind == REGEX:
            try:
                self._regex = re.compile(rule.pattern)
            except re.error:
                # problems() catches this before Save, but a hand-edited file
                # reaches here first. A rule nobody can compile matches nothing,
                # which is visible and harmless; raising would take the service's
                # whole output with it.
                self._broken = True

    def hits(self, line):
        if self._broken or not self.pattern:
            return False
        if self._regex is not None:
            return self._regex.search(line) is not None
        return self.pattern in line


class Matcher:
    """One criterion's state across a run.

    Its whole job is the distinction in the module docstring: a match rule is
    sticky once seen, an exclude rule is poisoned once tripped, and lit means
    every match seen and no exclude tripped.
    """

    def __init__(self, criterion):
        self.criterion = criterion
        self.name = criterion.name
        self.color = criterion.color
        self.source = criterion.source
        self._rules = [_CompiledRule(rule) for rule in criterion.rules]
        self.reset()

    def reset(self):
        """Forget the last run. Called when the service is started."""
        self._seen = set()
        self._tripped = set()

    def feed(self, line):
        """Take one line. True when it changed whether this is lit."""
        was = self.lit()
        for index, rule in enumerate(self._rules):
            if index in self._seen or index in self._tripped:
                continue        # a match already seen, or an exclude already tripped
            if not rule.hits(line):
                continue
            if rule.mode == MATCH:
                self._seen.add(index)
            else:
                self._tripped.add(index)
        return self.lit() != was

    def feed_all(self, lines):
        """Take a whole buffer at once. True when anything changed.

        So that a criterion added to a service that is *already running* answers
        for what it has printed so far, rather than only for whatever it says
        next - which on a quiet service could be never.
        """
        changed = False
        for line in lines:
            changed = self.feed(line) or changed
        return changed

    def lit(self):
        if not self._rules:
            return False
        for index, rule in enumerate(self._rules):
            if rule.mode == MATCH and index not in self._seen:
                return False
            if rule.mode == EXCLUDE and index in self._tripped:
                return False
        return True

    def outstanding(self):
        """What is keeping it dark, in words, for the tooltip."""
        if self.lit():
            return []
        missing = []
        for index, rule in enumerate(self._rules):
            if rule.mode == MATCH and index not in self._seen:
                missing.append("waiting for %r" % rule.pattern)
            elif rule.mode == EXCLUDE and index in self._tripped:
                missing.append("saw %r" % rule.pattern)
        return missing


def matchers_for(criteria):
    """A matcher per criterion, in the order they are configured."""
    return [Matcher(criterion) for criterion in criteria]


def sources_of(criteria):
    """The distinct files these criteria watch. Blank sources are not files."""
    seen = []
    for criterion in criteria:
        source = (criterion.source or "").strip()
        if source and source not in seen:
            seen.append(source)
    return seen
