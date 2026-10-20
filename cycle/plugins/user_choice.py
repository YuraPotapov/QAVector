"""Stopping to ask somebody to pick from a list an earlier step produced.

A cycle used to take whatever its queue put first - ``limit: 1`` and the first
task by date - or everything the queue held. ``${...}`` cannot index a list
and a gate's buttons are fixed, so there was no way to say *this one* in the
middle of a run. This shows the list as a table, one row an item, and hands on
what was ticked: ``${steps.<id>.outputs.values}`` and the rest.

**The author says how many.** ``mode: single`` takes exactly one - a cycle
that works on one task per run; ``mode: multiple`` takes one or more - a
release that takes the tasks somebody ticked. ``checked`` says what is ticked
when the window opens.

**Nothing to choose from is said, never shown.** An empty list never opens a
window: ``when_empty`` decides whether that skips the step and what needs it,
lets the run carry on with nothing chosen, or fails. A run started on one item
(``choose``, usually the subject's pin) takes it without asking - and fails
when the list is empty or does not hold it, because asking for a task that is
not there is an error, not an absence of work.

**Not answered is not an answer**, as with ``approval.gate``: Cancel, a closed
window, the deadline passing or no application to ask in all fail the step.
What was chosen is reused on Resume and by a partial run, like ``user.input``.

**The items stay here.** Only what the table shows - each row's cells and its
detail text, cut to length - goes out with the question; the answer is row
indexes, mapped back to the items the step was given. A queue of Jira issues
with their comments would otherwise travel to the window and back for nothing.
"""

import json
import re
import string

from cycle import ask as ask_mod
from cycle import registry
from cycle.registry import CyclePlugin, PluginMetadata, field, output

#: The two answers. The first hands the ticked rows on; the other refuses.
CHOOSE, CANCEL = "Choose", "Cancel"
MULTIPLE, SINGLE = "multiple", "single"
MODES = (MULTIPLE, SINGLE)
SKIP, CONTINUE, FAIL = "skip", "continue", "fail"
WHEN_EMPTY = (SKIP, CONTINUE, FAIL)
ALL, NONE = "all", "none"

#: More rows than anybody picks from by reading. A list this long wants a
#: narrower query in the step before, not a longer table.
MAX_ITEMS = 1000
#: What one cell carries to the window. The table elides to its column and
#: shows this much on hover.
CELL_CHARS = 200
#: What one row's detail carries: a task description, not an attachment.
DETAIL_CHARS = 3000
#: The ending a cut value gets, so a cut reads as a cut.
ELLIPSIS = "…"

#: A whole ``${steps.<id>....}`` reference, for naming the step a list came from.
_FROM_STEP = re.compile(r"^\s*\$\{\s*steps\.([A-Za-z0-9_-]+)\.")


class UserChoice(CyclePlugin):
    metadata = PluginMetadata(
        id="user.choice", name="User Choice", category=registry.ACTION,
        asks_when_overdue=False,
        summary="Stop and ask a person to pick from a list for later steps.",
        inputs=(
            field("question", "What to ask", "multiline", required=True,
                  hint="Said as the choice being made: \"Which task should "
                       "this run take?\""),
            field("detail", "What they should see", "multiline",
                  hint="Shown above the table, for anything that helps them "
                       "choose."),
            field("items", "Items to choose from", "text", required=True,
                  hint="A list from an earlier step, written as the reference "
                       "alone: ${steps.queue.outputs.issues}. Each item is a "
                       "mapping, or a plain value."),
            field("value", "The field that names an item", "text", default="key",
                  hint="What later steps get for each item chosen, and what "
                       "choose and checked are matched against. Every item "
                       "must have it, and no two the same. Ignored for plain "
                       "values, which name themselves."),
            field("columns", "Columns", "args",
                  hint="The fields shown as the table's columns, in order: "
                       "key summary status assignee. Blank shows the value "
                       "alone."),
            field("item_detail", "Detail for the selected row", "multiline",
                  hint="Shown under the table for the row selected, with "
                       "{field} for an item's fields: {status}\\n\\n"
                       "{description}. A field an item lacks is blank."),
            field("mode", "How many", "choice", default=MULTIPLE, options=MODES,
                  hint="single takes exactly one - for a cycle that works on "
                       "one item per run. multiple takes one or more."),
            field("checked", "Ticked when it opens", "text", default=NONE,
                  hint="all, none, or values - QA-1, QA-4. A value the list "
                       "does not hold is passed over. all is for multiple "
                       "only."),
            field("choose", "Chosen without asking", "text",
                  hint="Values taken without opening the window - usually "
                       "${vars.task_key}, the pin of a run started on one "
                       "item. Blank asks. A value the list does not hold "
                       "fails the step."),
            field("when_empty", "When the list is empty", "choice", default=SKIP,
                  options=WHEN_EMPTY,
                  hint="skip passes this step and everything that needs it - "
                       "nothing to do. continue succeeds with nothing chosen, "
                       "for later steps that decide for themselves. fail "
                       "stops the run. No window opens in any of them."),
        ),
        outputs=(
            output("items", "list", "The items chosen, in the list's order."),
            output("values", "list", "Their values, in the same order."),
            output("value", "text", "The first value chosen - the one, in "
                                    "single mode."),
            output("item", "mapping", "The first item chosen, whole: "
                                      "${steps.<id>.outputs.item.summary}."),
            output("joined", "text", "The values joined by commas, for a "
                                     "shell step."),
            output("count", "number", "How many were chosen."),
            output("who", "text", "Who answered, when the application says."),
        ),
    )

    def problems(self, settings):
        settings = settings or {}
        found = super().problems(settings)
        for key in ("question", "detail", "value", "item_detail"):
            if key in settings and settings[key] is not None \
                    and not isinstance(settings[key], str):
                found.append("%s must be text." % key)
        mode = settings.get("mode")
        if isinstance(mode, str) and "${" not in mode and mode \
                and mode not in MODES:
            found.append("mode must be single or multiple, not %r." % mode)
        empty = settings.get("when_empty")
        if isinstance(empty, str) and "${" not in empty and empty \
                and empty not in WHEN_EMPTY:
            found.append("when_empty must be skip, continue or fail, not %r."
                         % empty)
        if mode == SINGLE and str(settings.get("checked") or "").strip().lower() == ALL:
            found.append("checked: all ticks every row, and mode: single takes "
                         "one - name the value to tick instead.")
        columns = settings.get("columns")
        if columns is not None and not isinstance(columns, (list, str)):
            found.append("columns must be a list of field names.")
        template = settings.get("item_detail")
        if isinstance(template, str):
            found.extend(_template_problems(template))
        return found

    def execute(self, context, step):
        problems = self.problems(step.settings)
        if problems:
            return registry.failed("; ".join(problems))
        context.cancel.raise_if_set()
        settings = step.settings
        mode = str(self.setting(settings, "mode", MULTIPLE) or MULTIPLE)
        if mode not in MODES:
            return registry.failed("mode must be single or multiple, not %r." % mode)
        when_empty = str(self.setting(settings, "when_empty", SKIP) or SKIP)
        if when_empty not in WHEN_EMPTY:
            return registry.failed("when_empty must be skip, continue or fail, "
                                   "not %r." % when_empty)

        try:
            table = Choices.of(settings.get("items"),
                               str(self.setting(settings, "value", "key") or "key"),
                               _names(settings.get("columns"), words=True))
        except ChoiceError as exc:
            return registry.failed(str(exc))

        preset = _names(settings.get("choose"))
        if not table.items:
            return self._empty(context, step, preset, when_empty)

        if preset:
            return self._preset(context, step, table, preset, mode)

        question = str(self.setting(settings, "question") or "")
        detail_template = str(self.setting(settings, "item_detail", "") or "")
        checked, unknown = table.indexes(_names(settings.get("checked")),
                                         every=_is_all(settings.get("checked")))
        if mode == SINGLE:
            checked = checked[:1]
        if unknown:
            context.log(step.id, "stdout", ["checked %s is not in the list" % one
                                            for one in unknown])

        context.stage(step.id, {"kind": "start", "title": "Waiting for a choice",
                                "detail": question, "status": "running"})
        timeout_ms = int(step.timeout * 1000) if step.timeout else None
        answer = ask_mod.ask(
            question, detail=str(self.setting(settings, "detail", "") or ""),
            options=[CHOOSE, CANCEL], timeout_ms=timeout_ms, step=step.id,
            cancel=context.cancel, progress=getattr(context, "progress", None),
            choice=table.question(mode, detail_template, set(checked)))

        who = answer.get("who", "")
        if not answer.get("answered"):
            return self._refused(context, step, answer.get("message", "nobody answered"),
                                 who)
        if answer.get("answer") != CHOOSE:
            return self._refused(context, step, "cancelled", who)
        try:
            chosen = _chosen(answer.get("chosen"), len(table.items), mode)
        except ChoiceError as exc:
            return self._refused(context, step, str(exc), who)
        return self._done(context, step, table, chosen, who,
                          "chosen: %s" % ", ".join(table.values[i] for i in chosen))

    # -- the ways it ends -----------------------------------------------------
    def _empty(self, context, step, preset, when_empty):
        if preset:
            return registry.failed(
                "%s was asked for, but the list%s is empty."
                % (", ".join(preset), _source(context, step)))
        message = "Nothing to choose from: the list%s is empty." % _source(context, step)
        context.stage(step.id, {"kind": "note", "title": "Nothing to choose from",
                                "detail": message,
                                "status": "failed" if when_empty == FAIL else "done"})
        if when_empty == FAIL:
            return registry.failed(message)
        if when_empty == SKIP:
            return registry.skipped(message)
        return registry.succeeded(message=message, **_outputs([], [], ""))

    def _preset(self, context, step, table, preset, mode):
        chosen, unknown = table.indexes(preset)
        if unknown:
            return registry.failed(
                "%s %s not among the %d item(s) to choose from."
                % (", ".join(unknown), "is" if len(unknown) == 1 else "are",
                   len(table.items)))
        if mode == SINGLE and len(chosen) != 1:
            return registry.failed(
                "this choice takes one, and choose names %d: %s."
                % (len(chosen), ", ".join(preset)))
        return self._done(context, step, table, chosen, "",
                          "chosen without asking: %s"
                          % ", ".join(table.values[i] for i in chosen))

    def _refused(self, context, step, message, who):
        context.stage(step.id, {"kind": "review", "title": "Nothing chosen",
                                "detail": message, "status": "failed"})
        return registry.PluginResult("failed", message=message,
                                     outputs=_outputs([], [], who))

    def _done(self, context, step, table, chosen, who, message):
        values = [table.values[i] for i in chosen]
        context.stage(step.id, {"kind": "review", "title": "Chosen",
                                "detail": ", ".join(values), "status": "done"})
        return registry.succeeded(message=message,
                                  **_outputs([table.items[i] for i in chosen],
                                             values, who))


class ChoiceError(Exception):
    """A list, a column or an answer that cannot be chosen from as it is."""


class Choices(object):
    """The items, what names each one, and the columns shown for them."""

    def __init__(self, items, values, columns, mapped, value_field):
        self.items = items
        self.values = values
        self.columns = columns
        self.mapped = mapped
        self.value_field = value_field

    @classmethod
    def of(cls, items, value_field, columns):
        """Check everything a choice depends on. Raises :class:`ChoiceError`."""
        if items is None:
            raise ChoiceError("items is empty: it must be a list from an "
                              "earlier step, such as ${steps.queue.outputs.issues}.")
        if isinstance(items, str):
            raise ChoiceError(
                "items must be a list, and it is text - write the reference on "
                "its own, items: ${steps.<id>.outputs.<list>}, not inside other "
                "words.")
        if isinstance(items, dict):
            raise ChoiceError("items must be a list, not a mapping.")
        if not isinstance(items, (list, tuple)):
            raise ChoiceError("items must be a list, not %s." % type(items).__name__)
        items = list(items)
        if len(items) > MAX_ITEMS:
            raise ChoiceError(
                "%d items is more than a table can offer to choose from (%d at "
                "most) - narrow the step that makes the list." % (len(items), MAX_ITEMS))
        mapped = [isinstance(one, dict) for one in items]
        if items and any(mapped) and not all(mapped):
            raise ChoiceError("items must all be mappings or all plain values; "
                              "item %d is not like item 1."
                              % (mapped.index(not mapped[0]) + 1))
        mapped = bool(items) and mapped[0]
        for index, one in enumerate(items):
            if not mapped and not isinstance(one, (str, int, float, bool)):
                raise ChoiceError("item %d is a %s; items must be mappings or "
                                  "plain values." % (index + 1, type(one).__name__))

        values = []
        for index, one in enumerate(items):
            named = _plain(one.get(value_field)) if mapped else _plain(one)
            if not named:
                if mapped:
                    raise ChoiceError("item %d has no %s (it has: %s)."
                                      % (index + 1, value_field, _fields([one])))
                raise ChoiceError("item %d is empty." % (index + 1))
            values.append(named)
        seen = {}
        for index, named in enumerate(values):
            first = seen.setdefault(named.lower(), index)
            if first != index:
                raise ChoiceError("two items share the %s %s (items %d and %d)."
                                  % (value_field if mapped else "value", named,
                                     first + 1, index + 1))

        if not mapped:
            columns = ["value"]
        elif not columns:
            columns = [value_field]
        elif items:
            for column in columns:
                if not any(column in one for one in items):
                    raise ChoiceError("column %s: no item has it (they have: %s)."
                                      % (column, _fields(items)))
        return cls(items, values, list(columns), mapped, value_field)

    def indexes(self, wanted, every=False):
        """(indexes in the list's order, the wanted values it does not hold)."""
        if every:
            return list(range(len(self.items))), []
        by_value = {one.lower(): index for index, one in enumerate(self.values)}
        found, unknown = set(), []
        for one in wanted:
            if one.lower() == NONE:
                continue
            index = by_value.get(one.lower())
            if index is None:
                unknown.append(one)
            else:
                found.add(index)
        return sorted(found), unknown

    def question(self, mode, template, checked):
        """What goes out with the question: the table, and nothing else."""
        # The column that names the rows, drawn as machine text; -1 when the
        # author left it out of the table.
        if not self.mapped:
            value_column = 0
        elif self.value_field in self.columns:
            value_column = self.columns.index(self.value_field)
        else:
            value_column = -1
        rows = []
        for index, one in enumerate(self.items):
            fields = one if self.mapped else {"value": one}
            rows.append({"cells": [_cell(fields.get(column)) for column in self.columns],
                         "detail": _detail(template, fields),
                         "checked": index in checked})
        return {"mode": mode, "columns": list(self.columns),
                "value_column": value_column, "rows": rows}


def _outputs(items, values, who):
    return {"items": items, "values": values,
            "value": values[0] if values else "",
            "item": items[0] if items else {},
            "joined": ", ".join(values), "count": len(values), "who": who}


def _chosen(raw, count, mode):
    """The rows the window says were ticked, checked. Raises ChoiceError."""
    if not isinstance(raw, list) or not all(
            isinstance(one, int) and not isinstance(one, bool) for one in raw):
        raise ChoiceError("the window sent no rows it could name")
    if not raw:
        raise ChoiceError("nothing was chosen")
    if len(set(raw)) != len(raw):
        raise ChoiceError("the window sent the same row twice")
    outside = [one for one in raw if not 0 <= one < count]
    if outside:
        raise ChoiceError("the window sent row %d, and the list has %d"
                          % (outside[0] + 1, count))
    if mode == SINGLE and len(raw) != 1:
        raise ChoiceError("this choice takes one, and %d were sent" % len(raw))
    return sorted(raw)


def _names(value, words=False):
    """Values from a list, or from a line with commas between them.

    Commas and line breaks separate, spaces do not: a status such as "In
    Progress" is one value. ``words`` splits on spaces too, for field names,
    which never hold one.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [_plain(one) for one in value if _plain(one)]
    parts = re.split(r"[,\s]+" if words else r"[,\n]+", str(value))
    return [one.strip() for one in parts if one.strip()]


def _is_all(value):
    return isinstance(value, str) and value.strip().lower() == ALL


def _plain(value):
    """A value as one line of text: what names an item, or fills a cell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(_plain(one) for one in value if _plain(one))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
    return " ".join(str(value).split())


def _cell(value):
    return _cut(_plain(value), CELL_CHARS)


def _cut(text, limit, note=""):
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + ELLIPSIS + note


def _detail(template, fields):
    if not template:
        return ""
    values = {name: (value if isinstance(value, str) else _plain(value))
              for name, value in fields.items()}
    text = string.Formatter().vformat(template, (), _Blank(values)).strip()
    return _cut(text, DETAIL_CHARS, "\n(cut at %d characters)" % DETAIL_CHARS)


class _Blank(dict):
    """A field an item lacks reads as nothing, not as an error."""

    def __missing__(self, key):
        return ""


def _template_problems(template):
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        return ["item_detail cannot be read: %s (write {{ for a brace)." % exc]
    found = []
    for _text, name, _spec, _conversion in parsed:
        # None is plain text; "" is {} with no field in it, which would ask
        # for a positional argument nobody passes.
        if name is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            found.append("item_detail names {%s}; only an item's own fields can "
                         "be named, like {summary}." % name)
    return found


def _fields(items):
    names = sorted({str(key) for one in items if isinstance(one, dict) for key in one})
    shown = ", ".join(names[:12])
    return shown + (", ..." if len(names) > 12 else "") if names else "nothing"


def _source(context, step):
    """" from queue", when the list is a reference to a step; "" otherwise."""
    cycle = getattr(context, "cycle", None)
    raw = None
    if cycle is not None and cycle.step(step.id) is not None:
        raw = (cycle.step(step.id).settings or {}).get("items")
    found = _FROM_STEP.match(raw) if isinstance(raw, str) else None
    return " from %s" % found.group(1) if found else ""
