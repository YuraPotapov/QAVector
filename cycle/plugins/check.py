"""Several things that must all be true before the run goes on.

The pair to ``approval.gate``: one asks a person, this one asks the facts.

**Why it is not an ``if:``.** A condition on a step is deliberately one
comparison - ``cycle/conditions.py`` refuses ``and`` and ``or`` outright, and
has no ``<`` at all, because half an expression evaluator is worse than a small
complete one. That rule is right for an edge in the graph, and wrong for the
place a run decides whether it has earned the next step. Those decisions are
conjunctions: *the tree is the one that was verified, and the checks passed,
and the review is clean, and the acceptance is in*.

Written as ``if:`` that is a chain of four empty steps, four nodes of noise on
the canvas, and a refusal that reads "if: ... was not true" without saying
which. Written here it is one node, and the message names the check that did
not hold. A gate that cannot say what failed is not an explicit outcome.

**Order matters.** Checks are read in the order they are written and the first
one that does not hold is the answer, so a gate is written cheapest first - ask
whether there is anything to do before asking whether it was any good.

**What it is not.** Not a calculator and not a place for logic: every check is
one comparison, there is no ``or``, and a check that needs one is two gates or
a plugin. ``${...}`` is resolved before this ever sees it, so what arrives is
already ``"1 > 0"``.
"""

from cycle import registry
# The same equality `if:` uses, imported rather than rewritten: two rules about
# what makes 3 and "3" the same answer would eventually disagree, and the one
# somebody is reading would be the other one.
from cycle.conditions import _same
from cycle.registry import CyclePlugin, PluginMetadata, field, output

#: The comparisons a check may make, longest spelling first so that ``<=`` is
#: found before ``<`` when the line is scanned.
OPERATORS = (">=", "<=", "==", "!=", ">", "<")

#: The ones that only mean something for numbers. Everything else compares the
#: way ``if:`` compares, which is the point - one truth about equality in this
#: application, not two.
NUMERIC = (">", ">=", "<", "<=")

#: How the two halves of the application spell a boolean. Python writes
#: ``True``; YAML, JSON and every command-line tool write ``true``. A plugin
#: output that is a real boolean arrives here as Python's spelling - the
#: executor resolves ``${...}`` into the line before this sees it, and the type
#: is gone by then - while the literal beside it was typed by a person into a
#: YAML file. Comparing those as text makes ``verified == true`` quietly false
#: on exactly the runs where it should be true.
BOOLEAN_WORDS = ("true", "false")


class CheckGate(CyclePlugin):
    metadata = PluginMetadata(
        id="check.gate", name="Gate", category=registry.UTILITY,
        summary="Several things that must all hold. Names the one that did not.",
        inputs=(
            field("checks", "What must hold", "env", required=True,
                  hint="A name for each check, and the comparison it makes - "
                       "'${steps.impl.outputs.tree} == ${steps.commit.outputs."
                       "tree}'. Operators: == != < <= > >. Read in order, and "
                       "the first one that does not hold is the answer, so "
                       "write them cheapest first."),
        ),
        outputs=(
            output("passed", "boolean", "True only when every check held."),
            output("failed_check", "text",
                   "The name of the first check that did not hold."),
            output("reasons", "list",
                   "Every check, what it compared, and how it came out."),
        ),
    )

    def problems(self, settings):
        settings = settings or {}
        found = super().problems(settings)
        checks = settings.get("checks")
        if checks is not None and not isinstance(checks, dict):
            found.append("What must hold must be a mapping of names to "
                         "comparisons.")
            return found
        for name, expression in (checks or {}).items():
            if not isinstance(expression, str):
                found.append("%s: a check is written as text." % name)
                continue
            if _operator_in(expression) is None:
                # Caught here rather than at run time, because a line with no
                # operator is not a check that fails - it is a check nobody
                # finished writing, and failing the gate would read as though
                # the thing being guarded had gone wrong.
                found.append(
                    "%s: %r makes no comparison. Write it as '<left> == "
                    "<right>', with spaces around one of %s."
                    % (name, expression, " ".join(OPERATORS)))
        return found

    def execute(self, context, step):
        problems = self.problems(step.settings)
        if problems:
            return registry.failed("; ".join(problems))
        context.cancel.raise_if_set()

        checks = self.setting(step.settings, "checks", {}) or {}
        reasons, rows, failed, why = [], [], "", ""
        for name, expression in checks.items():
            held, account = _held(str(expression))
            reasons.append("%s: %s" % (name, account))
            # The same check twice over: as the line it has always been, for
            # the outputs and for anything reading text, and as a row saying
            # which way it went - which is the thing a reader of the gate
            # actually wants and the thing the joined text cannot say.
            rows.append({"name": str(name), "expression": str(expression),
                         "account": account, "held": held})
            if not held and not failed:
                # The first one that did not hold, not the last: a gate is
                # written cheapest first, and the earliest failure is the one
                # that explains the rest.
                failed, why = str(name), account

        context.stage(step.id, {
            # Its own kind, not "review": a gate is a verdict on comparisons
            # somebody wrote, and rendering it through the agent review's
            # layout drew three lines that all looked alike with the refusal
            # hidden in the heading.
            "kind": "gate", "status": "failed" if failed else "done",
            "title": _verdict(failed, len(reasons)),
            # Kept as it was, so a reader that does not know this kind - an
            # older GUI, a log - still shows what it showed before.
            "detail": "\n".join(reasons),
            "body": {"checks": rows}})

        outputs = {"passed": not failed, "failed_check": failed,
                   "reasons": reasons}
        if failed:
            return registry.PluginResult(
                "failed", outputs=outputs,
                message="%s: %s" % (failed, why))
        return registry.succeeded(
            message="all %d held" % len(reasons) if reasons
            else "nothing was asked", **outputs)


def _verdict(failed, how_many):
    """What the gate decided, in words rather than in a bare name.

    The title used to be the failed check's name on its own, which reads as the
    name of a phase - "within its budget" looks like something that went well.
    A gate has two answers and the row should say which one this is.
    """
    if failed:
        return "Gate refused: %s" % failed
    if how_many:
        return "Gate passed: all %d checks held" % how_many
    return "Gate had nothing to check"


# -- one comparison -----------------------------------------------------------
def _operator_in(expression):
    """``(operator, left, right)`` for the leftmost operator, or None.

    Two rules, and both are there because values arrive **already resolved**: a
    commit message, a summary or a diff is quite likely to contain a ``>`` of
    its own, and splitting on it would compare two things nobody asked about.

    1. An operator must have a space on each side. ``a>b`` is a value.
    2. An operator inside quotes is part of the value, not a comparison. That
       is what makes ``'a > b' == 'a > b'`` mean what it looks like.

    Leftmost rather than by some precedence among the operators: a line with
    two of them is ambiguous however it is read, and the reading that matches
    how somebody scans it is the one that will surprise them least.
    """
    quote = ""
    for index, letter in enumerate(expression):
        if quote:
            if letter == quote:
                quote = ""
            continue
        if letter in "'\"":
            quote = letter
            continue
        for operator in OPERATORS:       # longest spelling first
            token = " %s " % operator
            if expression.startswith(token, index):
                return (operator, expression[:index],
                        expression[index + len(token):])
    return None


def _held(expression):
    """``(True/False, what happened)`` for one check.

    The account comes back either way and says what was actually compared, so
    a gate that refuses shows the values rather than only the expression that
    was written - which is usually the whole question.
    """
    parsed = _operator_in(expression)
    if parsed is None:                    # refused by problems(); belt and braces
        return False, "%r makes no comparison" % expression
    operator, left, right = parsed
    left, right = _value(left), _value(right)
    account = "%s %s %s" % (_shown(left), operator, _shown(right))

    if operator in NUMERIC:
        numbers = _numbers(left, right)
        if numbers is None:
            # Deliberately not False: "not a number" and "smaller" are
            # different answers, and reporting the second would send somebody
            # looking for the wrong problem.
            return False, "%s - not numbers" % account
        return _compare(operator, *numbers), account

    same = _same(*_comparable(left, right))
    return (same if operator == "==" else not same), account


def _comparable(left, right):
    """The two sides, with a boolean written two ways made one thing.

    Only when **both** sides are boolean words: ``1 == true`` staying false is
    right, because a person who wrote that meant a number. See
    :data:`BOOLEAN_WORDS` for why this is needed at all.
    """
    if (str(left).strip().lower() in BOOLEAN_WORDS
            and str(right).strip().lower() in BOOLEAN_WORDS):
        return (str(left).strip().lower() == "true",
                str(right).strip().lower() == "true")
    return left, right


def _value(text):
    """One side of a comparison, with the quotes taken off if it has any.

    Quoting is optional here - ``${...}`` has already been resolved, so most
    sides arrive bare - but a value with spaces in it needs them, and a person
    who quotes out of habit should not get the quotes back in the answer.
    """
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return text


def _shown(value):
    """One side as it should be read back.

    Quoted, because quotes are the only thing that shows a trailing space or an
    empty answer - except for a number, where they turn arithmetic into what
    looks like a comparison of two pieces of text: ``'5' <= '3'`` reads as a
    mistake, and ``5 <= 3`` reads as the answer. An empty side is named rather
    than shown as two quotes nobody can measure.
    """
    text = "" if value is None else str(value)
    if not text:
        return "(empty)"
    if _numbers(text, text) is not None:
        return text
    return repr(text)


def _numbers(left, right):
    """Both sides as floats, or None when either is not a number."""
    try:
        return float(left), float(right)
    except (TypeError, ValueError):
        return None


def _compare(operator, left, right):
    if operator == ">":
        return left > right
    if operator == ">=":
        return left >= right
    if operator == "<":
        return left < right
    return left <= right
