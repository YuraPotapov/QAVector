"""The ``if:`` grammar: whether a step runs at all.

Deliberately tiny, and deliberately not Python. Three forms:

    ${steps.backend_tests.status} == 'failed'
    ${steps.backend_tests.status} != 'success'
    ${steps.deploy.outputs.changed}                 (truthiness)

No ``eval``. A condition comes out of a file that a cycle may have been shared
in, it is evaluated in the middle of a run that is starting services and
spawning processes, and "it is only a small expression language" is how every
template engine ends up executing arbitrary code. When a real need appears for
``and``, ``or`` or comparison against a number, that is the moment to choose a
grammar on purpose - half an expression evaluator is worse than a small
complete one.

:func:`problems` exists so a malformed condition is caught when the file is
read, by ``model.problems``, rather than at three in the morning in the middle
of the one run that reached that branch.
"""

import re

from cycle import variables
from domain.cycle import SKIPPED

#: ``<reference> <operator> <literal>``. The literal is quoted - single or
#: double - because an unquoted one would make ``== failed`` and ``== ${x}``
#: two different-looking things that both had to work.
_COMPARISON = re.compile(
    r"^\s*(?P<left>.+?)\s*(?P<op>==|!=)\s*(?P<right>.+?)\s*$")

#: A quoted literal, and nothing after it. The text may not contain the quote
#: that delimits it - a literal ends at its own closing quote, which is the
#: ordinary rule and, here, load-bearing: with a greedy ``.*`` this matched
#: from the FIRST quote to the LAST, so ``'a' and ${b} == 'c'`` read as one
#: long literal. That made a condition with ``and`` in it pass validation
#: silently and then evaluate false, and the step it guarded was skipped with
#: nothing said about why.
_QUOTED = re.compile(r"^(?P<quote>['\"])(?P<text>(?:(?!(?P=quote)).)*)(?P=quote)$",
                     re.DOTALL)

#: Words that mean somebody expected a grammar this does not have. Named so the
#: refusal can say which, because "neither a reference nor a quoted string" is
#: true and unhelpful when what you wrote was `and`.
#:
#: Deliberately without ``not``: it is a word ordinary prose uses, and a
#: malformed condition that happens to read "not an expression" should get the
#: general message rather than a lecture about an operator nobody used.
_ABSENT = (" and ", " or ", "&&", "||")

#: What counts as false when a condition is a bare reference. Everything else -
#: a non-empty string, a non-zero number, a non-empty list - is true. The
#: strings are here because a value that travelled through YAML or through a
#: process's output arrives as text, and "false" reading as true is the kind of
#: surprise that costs an afternoon.
_FALSEY = ("", "0", "false", "no", "off", "none", "null")


class ConditionError(Exception):
    """A condition that cannot be evaluated - usually an unresolvable reference."""


class ConditionUnavailable(ConditionError):
    """A condition needs outputs from a skipped step that produced none.

    This is not a false value: comparing an absent output to 'false' must not
    enter a branch that means a check actually ran and refused. The scheduler
    skips the dependent step instead.
    """


def evaluate(expression, root):
    """True when ``expression`` holds against ``root``. Raises ConditionError.

    ``root`` is the scope from :func:`cycle.variables.scope`. A reference to
    something that does not exist is an error rather than a quiet false: a
    condition on a step id that has been renamed should stop the run and say so,
    not silently skip the branch it was guarding. Outputs of a known skipped
    step that produced none raise ConditionUnavailable instead: the branch
    cannot be decided because its prerequisite never ran.
    """
    text = (expression or "").strip()
    if not text:
        return True

    match = _COMPARISON.match(text)
    if match:
        left = _side(match.group("left"), root)
        right = _side(match.group("right"), root)
        equal = _same(left, right)
        return equal if match.group("op") == "==" else not equal

    return _truthy(_side(text, root))


def problems(expression):
    """Everything wrong with ``expression``, as messages. Never raises.

    Checks the shape only - whether it parses, and whether its references look
    like references. What they point at cannot be known until a run is under
    way, so that stays :func:`evaluate`'s to complain about.
    """
    text = (expression or "").strip()
    if not text:
        return []

    joined = [one.strip() for one in _ABSENT if one in " %s " % text]
    if joined:
        # Named before anything else: the general message below is true of this
        # too, and would send somebody looking at their quoting.
        return ["if: %r uses %r, and conditions have no %r. A condition is one "
                "<reference> == 'value', the same with !=, or a bare "
                "reference. Two things that must both hold are two steps, or "
                "one step that checks both." % (expression, joined[0],
                                                joined[0])]

    match = _COMPARISON.match(text)
    sides = ([match.group("left"), match.group("right")] if match else [text])

    found = []
    for side in sides:
        side = side.strip()
        if not side:
            found.append("if: %r has an empty side." % expression)
            continue
        if _QUOTED.match(side):
            continue
        if variables.REFERENCE.fullmatch(side):
            continue
        found.append("if: %r is neither a ${...} reference nor a quoted "
                     "string. A condition is <reference> == 'value', the same "
                     "with !=, or a bare reference." % side)
    return found


def _side(text, root):
    """One side of a comparison: a quoted literal, or a resolved reference."""
    text = text.strip()
    quoted = _QUOTED.match(text)
    if quoted:
        return quoted.group("text")
    try:
        return variables.resolve(text, root)
    except variables.ResolveError as exc:
        reference = variables.REFERENCE.fullmatch(text)
        if reference:
            parts = reference.group(1).split(".")
            if len(parts) > 3 and parts[0] == "steps" and parts[2] == "outputs":
                step = root.get("steps", {}).get(parts[1], {})
                if step.get("status") == SKIPPED and not step.get("outputs"):
                    raise ConditionUnavailable(
                        "%s was skipped and produced no outputs for %s"
                        % (parts[1], text)) from exc
        raise ConditionError(str(exc))


def _same(left, right):
    """Equality that survives the trip through YAML and process output.

    ``3`` and ``"3"`` are the same answer to "what port did it come up on", and
    a condition should not have to know which side of the wire it is reading.
    Comparing as text when the types differ is the smallest rule that gets that
    right; two values of the same type compare as themselves.
    """
    if type(left) is type(right):
        return left == right
    if isinstance(left, bool) or isinstance(right, bool):
        return _truthy(left) == _truthy(right)
    return str(left) == str(right)


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() not in _FALSEY
    if value is None:
        return False
    if isinstance(value, (int, float, bool)):
        return bool(value)
    return bool(value)
