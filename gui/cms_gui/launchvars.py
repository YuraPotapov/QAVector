"""Variables in a launch address: ``http://localhost:{{port}}/web``.

An address is the one thing that changes between two runs of the same accounts -
another port, another branch of the same application - so it can carry
placeholders, and each one is filled from the nearest place that says something
about it. From the furthest to the nearest:

    the project's default  ->  the environment  ->  the saved configuration
                           ->  this run

``{{port}}`` and ``{{vars.port}}`` name the same variable. The project decides a
variable's type (string, integer, port, boolean) and nothing nearer can change
it; the nearer places only give it values. A place that says nothing about a
variable *inherits* it - which is not the same as giving it the empty string,
something only a string can be.

The syntax is the scenarios' own (``engine.context``), so an address reads like
a step does, but the rules are stricter because an address is checked before
any window opens: a placeholder that is never closed, an expression, a name
nobody defined, a value of the wrong type - each is a problem with the field it
belongs to, not a broken URL found by a browser. Substitution happens once; a
value that itself contains ``{{...}}`` is inserted as it is, never expanded by a
second, hidden pass. There are no expressions and no code: a name, or nothing.

No Qt in this module, and no core import - data and rules, tested directly.
"""

import collections
import re

TYPES = ("string", "integer", "port", "boolean")

#: The roots of the scenario context and the ones kept for it. ``vars`` is the
#: long spelling of a launch variable; the others are filled in for scenarios
#: (``{{env.origin}}``, ``{{user.login}}``) and mean nothing in an address.
RESERVED = frozenset(("vars", "env", "user", "project", "run"))

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_WHOLE = re.compile(r"-?\d+\Z")
_TRUE = ("true", "yes", "on", "1")
_FALSE = ("false", "no", "off", "0")

#: What the project says a variable is.
Definition = collections.namedtuple("Definition", "name type default required choices")

#: One place values come from: its label (said beside the value) and
#: ``{name: value}``. A name that is absent, or None, inherits.
Layer = collections.namedtuple("Layer", "source values")

#: Everything a resolution needs besides the template.
Scope = collections.namedtuple("Scope", "definitions layers")

#: A variable as resolved: typed value, its text in the address, the place it
#: came from, and what the next place down would have given it ("" and "" when
#: nothing would have).
Value = collections.namedtuple(
    "Value", "name value text source inherited inherited_source")

#: What is wrong, where: ``field`` is "address" for the template itself and a
#: variable's name for its value.
Problem = collections.namedtuple("Problem", "field code message")

#: ``text`` with every placeholder filled; ``parts`` the same text as
#: ``[(text, variable name or "")]`` for showing which part came from where;
#: ``values`` ``{name: Value}``; ``problems`` [Problem]. ``text`` is only an
#: address when ``problems`` is empty.
Resolution = collections.namedtuple("Resolution", "text parts values problems")

#: The project defaults' label, the furthest layer.
PROJECT = "project"

EMPTY = Scope({}, [])


# -- reading a template -------------------------------------------------------
def parse(template, field="address"):
    """``(tokens, problems)``: the template as text and placeholders.

    Each token is ``(text, name)``: ``name`` is "" for plain text and the
    variable's name for a placeholder, whose ``text`` is the placeholder as
    written. A placeholder that cannot be read stays in the tokens as plain
    text, so what is shown is still what was written.
    """
    template = str(template or "")
    tokens, problems = [], []
    position = 0
    while position < len(template):
        start = template.find("{{", position)
        literal = template[position:start if start >= 0 else len(template)]
        if "}}" in literal:
            problems.append(Problem(field, "unmatched",
                                    "There is a }} with no {{ before it."))
        if literal:
            tokens.append((literal, ""))
        if start < 0:
            break
        end = template.find("}}", start + 2)
        if end < 0:
            problems.append(Problem(field, "unterminated",
                                    "The {{ at character %d is never closed." % (start + 1)))
            tokens.append((template[start:], ""))
            break
        raw = template[start:end + 2]
        name, problem = _placeholder(template[start + 2:end].strip(), field)
        if problem:
            problems.append(problem)
            tokens.append((raw, ""))
        else:
            tokens.append((raw, name))
        position = end + 2
    return tokens, problems


def _placeholder(inner, field):
    """``(name, problem)`` for what stands between ``{{`` and ``}}``."""
    if not inner:
        return "", Problem(field, "empty", "There is an empty {{ }}.")
    if "." in inner:
        root, rest = inner.split(".", 1)
        if root == "vars" and _NAME.match(rest):
            return rest, None
        if root in RESERVED:
            return "", Problem(field, "reserved",
                               "{{%s}} is filled in for scenarios; an address can only "
                               "use its own variables, as {{name}}." % inner)
        return "", Problem(field, "expression", _NOT_AN_EXPRESSION % inner)
    if _NAME.match(inner):
        if inner in RESERVED:
            return "", Problem(field, "reserved",
                               "\"%s\" is a reserved word and cannot be a variable." % inner)
        return inner, None
    return "", Problem(field, "expression", _NOT_AN_EXPRESSION % inner)


_NOT_AN_EXPRESSION = ("{{%s}} is not a variable's name. Only a name can go between "
                      "{{ and }} - no sums, calls or code.")


def references(template):
    """The names of the variables ``template`` uses, in order, each once."""
    tokens, _problems = parse(template)
    return list(dict.fromkeys(name for _text, name in tokens if name))


# -- types --------------------------------------------------------------------
def valid_name(name):
    """Whether ``name`` can be a variable: letters, digits and _, not reserved."""
    return bool(_NAME.match(str(name or ""))) and name not in RESERVED


def coerce(definition, raw):
    """``(value, error)``: ``raw`` as ``definition``'s type, or why it is not."""
    kind = definition.type
    if kind == "string":
        if isinstance(raw, bool):
            raw = "true" if raw else "false"
        value = str(raw)
        if definition.choices and value not in definition.choices:
            return None, "has to be one of: %s" % ", ".join(definition.choices)
        return value, ""
    if kind == "boolean":
        if isinstance(raw, bool):
            return raw, ""
        text = str(raw).strip().lower()
        if text in _TRUE:
            return True, ""
        if text in _FALSE:
            return False, ""
        return None, "has to be true or false"
    # integer and port
    if isinstance(raw, bool):
        number = None
    elif isinstance(raw, int):
        number = raw
    elif _WHOLE.match(str(raw).strip()):
        number = int(str(raw).strip())
    else:
        number = None
    if kind == "port":
        if number is None or not 1 <= number <= 65535:
            return None, "has to be a port: a whole number from 1 to 65535"
        return number, ""
    if number is None:
        return None, "has to be a whole number"
    return number, ""


def text_of(definition, value):
    """How ``value`` is written into an address."""
    if definition.type == "boolean":
        return "true" if value else "false"
    return str(value)


def definitions_from(raw):
    """``({name: Definition}, problems)`` from a project's ``variables`` mapping.

    ``raw`` is ``{name: {"type", "default", "required", "choices"}}``. A
    definition that cannot be read is left out, with a problem naming it.
    """
    found, problems = {}, []
    for name, spec in (raw or {}).items():
        if not valid_name(name):
            problems.append(Problem(str(name), "name",
                                    "\"%s\" cannot be a variable's name: letters, digits "
                                    "and _, not starting with a digit, and not one of "
                                    "%s." % (name, ", ".join(sorted(RESERVED)))))
            continue
        spec = spec if isinstance(spec, dict) else {}
        kind = spec.get("type", "string")
        if kind not in TYPES:
            problems.append(Problem(name, "type",
                                    "%s has the type %r; it can be one of %s."
                                    % (name, kind, ", ".join(TYPES))))
            continue
        choices = [str(c) for c in spec.get("choices") or ()] if kind == "string" else []
        definition = Definition(name, kind, None, bool(spec.get("required", True)),
                                tuple(choices))
        default = spec.get("default")
        if default is not None:
            default, error = coerce(definition, default)
            if error:
                problems.append(Problem(name, "type", "The default of %s %s." % (name, error)))
                default = None
        found[name] = definition._replace(default=default)
    return found, problems


# -- resolving ----------------------------------------------------------------
def resolve(template, definitions, layers, field="address"):
    """Fill ``template``'s placeholders: a :data:`Resolution`.

    ``layers`` run from the furthest place to the nearest; the project's
    defaults, taken from ``definitions``, come before all of them.
    """
    tokens, problems = parse(template, field)
    stack = [Layer(PROJECT, {name: d.default for name, d in definitions.items()})]
    stack.extend(layers or ())
    parts, values = [], {}
    for text, name in tokens:
        if not name:
            parts.append((text, ""))
            continue
        definition = definitions.get(name)
        if definition is None:
            problems.append(Problem(field, "unknown",
                                    "{{%s}} is not defined. Define %s to give it a value."
                                    % (name, name)))
            parts.append((text, ""))
            continue
        if name in values:
            parts.append((values[name].text, name))
            continue
        given = [(layer.source, layer.values[name]) for layer in stack
                 if name in (layer.values or {}) and layer.values[name] is not None]
        if not given:
            if definition.required:
                problems.append(Problem(name, "missing",
                                        "%s has no value. Give it one in Variables." % name))
                parts.append((text, ""))
                continue
            given = [(PROJECT, "")]
        source, raw = given[-1]
        value, error = coerce(definition, raw)
        if error:
            problems.append(Problem(name, "type", "%s %s (it is %r here, from %s)."
                                    % (name, error, raw, source)))
            parts.append((str(raw), name))
            continue
        inherited, inherited_source = "", ""
        if len(given) > 1:
            below, below_raw = given[-2]
            below_value, below_error = coerce(definition, below_raw)
            inherited = str(below_raw) if below_error else text_of(definition, below_value)
            inherited_source = below
        values[name] = Value(name, value, text_of(definition, value), source,
                             inherited, inherited_source)
        parts.append((values[name].text, name))
    return Resolution("".join(text for text, _name in parts), parts, values, problems)
