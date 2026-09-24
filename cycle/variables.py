"""``${...}`` resolution against a run's scope.

A cycle's steps do not know about each other. A step that needs the workspace a
checkout produced writes ``${steps.checkout.outputs.workspace}``, and what it
gets is whatever that plugin put in its outputs. That indirection is the whole
reason steps compose, so this module is small but load-bearing.

The scope a reference resolves against::

    vars.<name>                       the cycle's variables, as the run started
    env.<NAME>                        the process environment
    run.id / run.workspace / run.cycle
    steps.<id>.status                 pending | running | success | failed | ...
    steps.<id>.outputs.<key>          whatever that plugin returned
    steps.<id>.artifacts.<name>       a path, relative to the run workspace

**Sibling to, not the same as, ``engine.context.substitute``.** That function
resolves ``{{a.b}}`` for scenarios, and everything about its shape is right:
a dotted walk, no template dependency, an unknown path is an error rather than
an empty string, non-strings pass through untouched. This does the same things
the same way. It is a separate function for three reasons, all of which would
be wrong to paper over:

* **Binding time.** ``substitute`` runs at compile time against a root that is
  fixed before anything executes. A cycle's ``with:`` has to resolve at the
  moment its step becomes ready, because the outputs it reads do not exist
  until the step that produces them has finished.
* **Syntax and root.** ``{{a.b}}`` over two keys against ``${...}`` over five.
  One function with both would have two modes and two vocabularies of error.
* **Blast radius.** ``engine/context.py`` is in the compile path of every
  scenario run. Cycle concepts do not belong in code that every existing run
  executes.

One difference from ``substitute`` is deliberate and worth knowing: a string
that is *entirely* one reference returns the raw value, so
``"${steps.pg.outputs.port}"`` stays an ``int`` and can be handed to something
that wants a number. A reference embedded in a larger string stringifies, which
is the only thing it could do. Without that rule every port, count and flag
arrives as text and each plugin has to convert it back.
"""

import os
import re

#: One reference. The path allows brackets so ``${steps.a.outputs.ports[0]}``
#: has somewhere to go later; today an index is part of the path and simply
#: fails to resolve, which is an honest error rather than a wrong answer.
REFERENCE = re.compile(r"\$\{\s*([A-Za-z0-9_.\[\]-]+)\s*\}")

#: The same reference, but only when it is the whole string. This is what
#: decides between returning a raw value and building a new string.
_WHOLE = re.compile(r"^\s*\$\{\s*([A-Za-z0-9_.\[\]-]+)\s*\}\s*$")


class ResolveError(Exception):
    """A ``${...}`` reference pointed at something that is not there."""


def scope(run=None, cycle=None, step_runs=None, env=None, secrets=None):
    """The root a reference resolves against.

    Built fresh each time a step becomes ready, because ``steps`` is the half
    that changes: every finished step adds its status and its outputs, and that
    is what a later step is written to read.
    """
    steps = {}
    for step_id, step_run in (step_runs or {}).items():
        steps[step_id] = {
            "status": step_run.status,
            "outputs": dict(step_run.outputs or {}),
            "artifacts": {(artifact.name or os.path.basename(artifact.path)):
                          artifact.path
                          for artifact in (step_run.artifacts or [])},
            "message": step_run.message,
            "attempts": step_run.attempts,
            "duration_ms": step_run.duration_ms,
        }

    variables = dict((cycle.variables if cycle is not None else {}) or {})
    # Secrets come last and only for the names the cycle declared secret: a
    # store holding a stale entry for a variable that is no longer secret must
    # not quietly reappear as a value.
    for name in (getattr(cycle, "secrets", ()) or ()):
        variables[name] = (secrets or {}).get(name, "")
    if run is not None:
        variables.update(run.variables or {})

    return {
        "vars": variables,
        "env": dict(os.environ if env is None else env),
        "run": {"id": getattr(run, "id", ""),
                "workspace": getattr(run, "workspace", ""),
                "cycle": getattr(run, "cycle_id", ""),
                "trigger": getattr(run, "trigger", "")},
        "steps": steps,
    }


def resolve(value, root):
    """``value`` with every ``${a.b}`` replaced from ``root``.

    Recurses into dicts and lists, so a whole ``with:`` mapping goes through in
    one call. Anything that is not a string, dict or list passes through as it
    is - a number stays a number, and None stays None.
    """
    if isinstance(value, dict):
        return {key: resolve(item, root) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        resolved = [resolve(item, root) for item in value]
        return type(value)(resolved) if isinstance(value, tuple) else resolved
    if not isinstance(value, str):
        return value

    whole = _WHOLE.match(value)
    if whole:
        return lookup(whole.group(1), root)

    def replace(match):
        found = lookup(match.group(1), root)
        return "" if found is None else str(found)

    return REFERENCE.sub(replace, value)


def lookup(path, root):
    """Walk a dotted path through ``root``. Raises :class:`ResolveError`.

    An unknown path is an error rather than an empty string, for the reason
    ``engine.context.substitute`` gives: a typo should fail where it was
    written, not quietly produce a command with a hole in it.
    """
    current = root
    walked = []
    for part in path.split("."):
        walked.append(part)
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        raise ResolveError(
            "${%s} does not exist%s" % (path, _near(walked, current)))
    return current


def _near(walked, current):
    """Where the walk stopped and what it could have gone to, for the message.

    A bare "does not exist" leaves someone comparing their file against a
    five-branch scope by eye. Naming the step that has no such output, and what
    outputs it does have, is usually the whole of the fix.
    """
    where = ".".join(walked[:-1])
    if not isinstance(current, dict):
        return " (%s is not a mapping)" % (where or "the scope")
    options = sorted(str(key) for key in current)
    if not options:
        return " (%s is empty)" % (where or "the scope")
    shown = ", ".join(options[:8])
    if len(options) > 8:
        shown += ", ..."
    return " (%s has: %s)" % (where or "the scope", shown)


def references(value):
    """Every path referenced inside ``value``, recursively. For diagnostics."""
    found = []
    if isinstance(value, dict):
        for item in value.values():
            found.extend(references(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(references(item))
    elif isinstance(value, str):
        found.extend(REFERENCE.findall(value))
    return found
