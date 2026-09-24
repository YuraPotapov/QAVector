"""Turning a cycle mapping into a :class:`~domain.cycle.Cycle`, and checking it.

Two jobs, and the split between them matters:

* :func:`parse_cycle` is strict about *shape*. A cycle whose ``steps`` is not a
  list, or whose step is not a mapping, cannot be represented at all, so it
  raises :class:`CycleError` and there is nothing further to say about it.
* :func:`problems` is exhaustive about *meaning*, and never raises. It reports
  everything wrong with a parsed cycle at once - a bad ``needs``, a dependency
  ring, an unknown plugin, a malformed condition - because someone fixing a file
  wants the whole list, not the first complaint followed by another run.

That is the same division ``engine/compiler.py`` and ``engine/flowfile.py``
already make for scenarios, and it is what lets the editor show problems beside
a file it is still willing to open.

:func:`to_graph` is the third job and the one with a rule attached: it is the
*only* function that produces the graph payload. Both ``--cycle-show`` and the
``cycle.run.start`` event carry what it returns, so the canvas draws the same
nodes whether a file was opened or a run was started. There is no second
"visual" model to drift from this one - not by convention, but because there is
nowhere else for one to come from.
"""

import re

from domain.cycle import Cycle, CycleStep, ON_FAILURE, STOP, Subject

#: A step id has to survive being a dict key, a path segment under the run
#: workspace (``steps/<id>/``), and a reference inside ``${steps.<id>....}``.
#: That last one is why a dot is not allowed: it is the path separator there.
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

#: Keys a step may carry. Anything else is a typo worth reporting - a silently
#: ignored ``timout:`` is a step that quietly never times out.
STEP_KEYS = frozenset((
    "id", "plugin", "label", "needs", "with", "if", "timeout", "retry",
    "on_failure", "disabled",
))

#: Keys a cycle may carry.
CYCLE_KEYS = frozenset(("id", "name", "description", "project", "version",
                        "variables", "subject", "steps"))

#: Keys of the ``subject:`` mapping - see :class:`~domain.cycle.Subject`.
SUBJECT_KEYS = frozenset(("kind", "key", "title", "memory", "pin"))

#: The subject fields that are ``${...}`` expressions, resolved during a run.
SUBJECT_EXPRESSIONS = ("key", "title", "memory")

#: What a variable declared as a mapping may be, besides a secret. Its value is
#: ordinary text and is read as such - ``${vars.repo}`` gets the same string
#: whatever it was declared as. The declaration only tells a form how to help
#: somebody fill it in.
PATH = "path"

#: Keys of the ``retry:`` mapping. ``backoff`` is accepted and ignored, with a
#: warning from :func:`problems`: the plan calls for it later, and a file
#: written against it now should be told plainly that it does nothing yet
#: rather than quietly behaving as though it said nothing.
RETRY_KEYS = frozenset(("attempts", "delay", "backoff"))


class CycleError(Exception):
    """A cycle mapping that cannot be turned into a Cycle at all."""


# -- parsing ------------------------------------------------------------------
def parse_cycle(raw, cycle_id, source=None):
    """Build a :class:`Cycle` from a mapping. Raises :class:`CycleError`.

    ``cycle_id`` is what the caller knows the cycle as - its filename, usually.
    A document with an ``id`` of its own must agree with it, for the same reason
    a scenario must: the id is how everything else refers to the file, and two
    answers to "what is this called" is a bug waiting for a rename.
    """
    if not isinstance(raw, dict):
        raise CycleError("a cycle file must contain a mapping, not %s"
                         % type(raw).__name__)

    declared = str(raw.get("id") or "").strip()
    if declared and cycle_id and declared != cycle_id:
        raise CycleError("id is %r but the file is %r; they have to match"
                         % (declared, cycle_id))

    steps_raw = raw.get("steps")
    if steps_raw is None:
        steps_raw = []
    if not isinstance(steps_raw, list):
        raise CycleError("steps must be a list, not %s" % type(steps_raw).__name__)

    variables, secrets, paths = _parse_variables(raw.get("variables") or {})

    steps = [_parse_step(entry, index) for index, entry in enumerate(steps_raw)]

    return Cycle(id=declared or cycle_id,
                 subject=_parse_subject(raw.get("subject")),
                 name=str(raw.get("name") or "").strip(),
                 description=str(raw.get("description") or "").strip(),
                 project=str(raw.get("project") or "").strip(),
                 version=_int_or(raw.get("version"), 1),
                 variables=variables,
                 secrets=secrets,
                 paths=paths,
                 steps=steps,
                 source=source)


def _parse_variables(raw):
    """``(plain values, names declared secret, names declared paths)``.

    Three shapes::

        branch: main                          an ordinary variable
        token: {secret: true}                 the value lives elsewhere
        repo: {kind: path, value: /src/app}   a folder on this machine

    A **secret** has no value to write down, and one carrying a value in the
    file is refused outright rather than quietly accepted: writing one there is
    the mistake the split exists to prevent, and accepting it would leave
    somebody believing it was safe.

    A **path** is the opposite: its value is ordinary and lives here like any
    other. ``${vars.repo}`` reads the same string whatever it was declared as,
    and nothing at run time treats it differently. The declaration is for
    whoever is *editing* the cycle - a form can offer to find a folder rather
    than leaving somebody to type one correctly, which is how a cycle ends up
    pointed at a directory that is nearly right.
    """
    if not isinstance(raw, dict):
        raise CycleError("variables must be a mapping, not %s"
                         % type(raw).__name__)

    values, secrets, paths = {}, [], []
    for name, value in raw.items():
        if not isinstance(value, dict):
            values[name] = value
            continue
        kind = value.get("kind")
        if value.get("secret"):
            if kind not in (None, "secret"):
                raise CycleError("variable %r cannot be both secret and %s."
                                 % (name, kind))
            for key in ("value", "default"):
                if key in value:
                    raise CycleError(
                        "secret variable %r must not carry a %s in the cycle "
                        "file. Cycle files are committed and shipped; set the "
                        "value in the application instead." % (name, key))
            secrets.append(name)
            continue
        if kind == PATH:
            if "default" in value:
                raise CycleError("path variable %r takes a value, not a "
                                 "default." % name)
            values[name] = value.get("value", "")
            paths.append(name)
            continue
        raise CycleError("variable %r is a mapping but says neither "
                         "secret: true nor kind: %s." % (name, PATH))
    return values, tuple(sorted(secrets)), tuple(sorted(paths))


def _parse_subject(raw):
    """``subject:`` -> :class:`Subject`, or None when the cycle declares none."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise CycleError("subject must be a mapping with kind and key, not %s"
                         % type(raw).__name__)
    return Subject(**{name: str(raw.get(name) or "").strip()
                      for name in sorted(SUBJECT_KEYS)})


def _parse_step(entry, index):
    """One step mapping -> :class:`CycleStep`. Raises on a shape that cannot hold."""
    if not isinstance(entry, dict):
        raise CycleError("step %d must be a mapping, not %s"
                         % (index + 1, type(entry).__name__))

    step_id = str(entry.get("id") or "").strip()
    if not step_id:
        raise CycleError("step %d has no id" % (index + 1))

    settings = entry.get("with") or {}
    if not isinstance(settings, dict):
        raise CycleError("%s: with must be a mapping, not %s"
                         % (step_id, type(settings).__name__))

    retry = entry.get("retry") or {}
    if not isinstance(retry, dict):
        raise CycleError("%s: retry must be a mapping, not %s"
                         % (step_id, type(retry).__name__))

    return CycleStep(
        id=step_id,
        plugin=str(entry.get("plugin") or "").strip(),
        label=str(entry.get("label") or "").strip(),
        needs=tuple(_as_list(entry.get("needs"))),
        settings=dict(settings),
        condition=str(entry.get("if") or "").strip(),
        timeout=_float_or_none(entry.get("timeout")),
        retry_attempts=_int_or(retry.get("attempts"), 1),
        retry_delay=_float_or(retry.get("delay"), 0.0),
        on_failure=str(entry.get("on_failure") or STOP).strip(),
        disabled=bool(entry.get("disabled")),
        source_index=index,
    )


def _as_list(value):
    """``needs:`` written as one name or as a list; both are natural to write."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _int_or(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_or(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _float_or_none(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# -- validation ---------------------------------------------------------------
def problems(cycle, registry=None, raw=None):
    """Everything wrong with ``cycle``, as messages a person can read.

    Never raises. ``registry`` is the plugin table (``cycle.registry``); without
    one, plugin ids and their settings go unchecked - which is what
    ``--describe`` wants, because it reads every cycle on the machine and should
    not pay for loading plugins to do it.

    ``raw`` is the original mapping, when the caller still has it. Unknown keys
    can only be reported from there: a parsed Cycle has already dropped them,
    and a silently ignored ``timout:`` is exactly the kind of thing that is
    worth a line.
    """
    found = []
    found.extend(_key_problems(raw))
    found.extend(_subject_problems(cycle))
    found.extend(_step_problems(cycle, registry))
    found.extend(_needs_problems(cycle))
    found.extend(_ring_problems(cycle))
    from cycle import revisions
    for step in cycle.steps:
        target = step.settings.get("revision_step") if step.plugin == "approval.gate" else None
        if target:
            try:
                revisions.targets(cycle, step.id, target)
            except ValueError as exc:
                found.append("%s: %s" % (step.id, exc))
    return found


def _key_problems(raw):
    if not isinstance(raw, dict):
        return []
    found = []
    for key in sorted(set(raw) - CYCLE_KEYS):
        found.append("unknown key %r; a cycle takes %s"
                     % (key, ", ".join(sorted(CYCLE_KEYS))))
    if isinstance(raw.get("subject"), dict):
        for key in sorted(set(raw["subject"]) - SUBJECT_KEYS):
            found.append("subject: unknown key %r; a subject takes %s"
                         % (key, ", ".join(sorted(SUBJECT_KEYS))))
    for index, entry in enumerate(raw.get("steps") or []):
        if not isinstance(entry, dict):
            continue
        where = str(entry.get("id") or "step %d" % (index + 1))
        for key in sorted(set(entry) - STEP_KEYS):
            found.append("%s: unknown key %r; a step takes %s"
                         % (where, key, ", ".join(sorted(STEP_KEYS))))
        retry = entry.get("retry")
        if isinstance(retry, dict):
            for key in sorted(set(retry) - RETRY_KEYS):
                found.append("%s: unknown retry key %r" % (where, key))
            if "backoff" in retry:
                found.append("%s: retry.backoff is not implemented yet and is "
                             "ignored; every attempt waits retry.delay." % where)
    return found


def _subject_problems(cycle):
    """What would stop a run from saying what it is working on.

    A secret is refused anywhere in the subject: the subject is written into
    the run record and the run index in plain text, and shown on screen, which
    is exactly where a secret must not end up.
    """
    subject = cycle.subject
    if subject is None:
        return []
    from cycle import variables

    found = []
    if not subject.kind:
        found.append("subject: kind is empty; say what the cycle works on - "
                     "task, branch, release.")
    if not subject.key:
        found.append("subject: key is empty; it is what tells one subject from "
                     "the next, e.g. ${steps.todo.outputs.key}.")
    known = {step.id for step in cycle.steps}
    for name in SUBJECT_EXPRESSIONS:
        for path in variables.references(getattr(subject, name)):
            parts = path.split(".")
            if parts[0] == "steps" and len(parts) > 1 and parts[1] not in known:
                found.append("subject: %s reads %r, which is not a step in this "
                             "cycle." % (name, parts[1]))
            elif parts[0] == "vars" and len(parts) > 1 and parts[1] in cycle.secrets:
                found.append("subject: %s reads the secret %r. The subject is "
                             "written into the run record and shown on screen, "
                             "so it cannot hold a secret." % (name, parts[1]))
    if subject.pin:
        if subject.pin in cycle.secrets:
            found.append("subject: pin names the secret %r; a pinned subject "
                         "is passed on the command line in plain text."
                         % subject.pin)
        elif subject.pin not in cycle.variables:
            found.append("subject: pin names %r, which is not one of the "
                         "cycle's variables." % subject.pin)
        elif not any(path == "vars." + subject.pin
                     for step in cycle.steps
                     for path in variables.references(step.settings)):
            found.append("subject: pin names %r, but no step reads "
                         "${vars.%s}, so setting it could not change what the "
                         "run works on." % (subject.pin, subject.pin))
    return found


def subject_sources(cycle):
    """The steps a cycle's subject is read from, in file order.

    What a reader is told before the subject is known: "decided by *Take one
    task*" is an answer, "not known yet" is only half of one.
    """
    subject = cycle.subject
    if subject is None:
        return []
    from cycle import variables

    named = set()
    for name in SUBJECT_EXPRESSIONS:
        for path in variables.references(getattr(subject, name)):
            parts = path.split(".")
            if parts[0] == "steps" and len(parts) > 1:
                named.add(parts[1])
    return [step.id for step in sorted(cycle.steps, key=lambda s: s.source_index)
            if step.id in named]


def _step_problems(cycle, registry):
    found, seen = [], set()
    if not cycle.steps:
        found.append("a cycle needs at least one step.")
    for step in cycle.steps:
        if not ID_PATTERN.match(step.id):
            found.append("%r is not a usable step id; use letters, digits, "
                         "underscores and hyphens." % step.id)
        if step.id in seen:
            found.append("%s: two steps share this id, so nothing could tell "
                         "them apart." % step.id)
        seen.add(step.id)

        if not step.plugin:
            found.append("%s: no plugin." % step.id)
        elif registry is not None and registry.get(step.plugin) is None:
            known = ", ".join(sorted(p.metadata.id for p in registry.all_plugins()))
            found.append("%s: unknown plugin %r. Installed: %s"
                         % (step.id, step.plugin, known or "(none)"))

        if step.on_failure not in ON_FAILURE:
            found.append("%s: on_failure is %r; it has to be %s."
                         % (step.id, step.on_failure, " or ".join(ON_FAILURE)))
        if step.timeout is not None and step.timeout <= 0:
            found.append("%s: timeout is %s; it has to be a positive number of "
                         "seconds, or absent for no deadline."
                         % (step.id, step.timeout))
        if step.retry_attempts < 1:
            found.append("%s: retry.attempts is %d; it counts tries, so the "
                         "smallest useful value is 1."
                         % (step.id, step.retry_attempts))
        if step.retry_delay < 0:
            found.append("%s: retry.delay cannot be negative." % step.id)

        if step.condition:
            from cycle import conditions
            found.extend("%s: %s" % (step.id, message)
                         for message in conditions.problems(step.condition))

        if registry is not None and step.plugin:
            plugin = registry.get(step.plugin)
            if plugin is not None:
                found.extend("%s: %s" % (step.id, message)
                             for message in plugin.problems(step.settings))
    return found


def _needs_problems(cycle):
    found = []
    known = {step.id for step in cycle.steps}
    for step in cycle.steps:
        for need in step.needs:
            if need == step.id:
                found.append("%s: needs itself, so it could never start."
                             % step.id)
            elif need not in known:
                found.append("%s: needs %r, which is not a step in this cycle."
                             % (step.id, need))
        found.extend(_race_problems(step, known))
    return found


def _race_problems(step, known):
    """A condition reading a step this one does not wait for is a race.

    ``if: ${steps.tests.status} == 'failed'`` on a step that does not ``need:``
    tests is evaluated the moment the run starts, when tests is still pending -
    so it silently never fires. Nothing about that is visible in the run; the
    step is simply skipped, every time, for a reason that looks like the
    condition being false. Worth a message rather than an afternoon.
    """
    if not step.condition:
        return []
    from cycle import variables

    found = []
    for path in variables.references(step.condition):
        parts = path.split(".")
        if len(parts) < 2 or parts[0] != "steps":
            continue
        other = parts[1]
        if other == step.id or other not in known or other in step.needs:
            continue
        found.append("%s: its `if:` reads %s, but it does not need %s - so the "
                     "condition is evaluated before %s has run and will not "
                     "see what it came to. Add %s to needs."
                     % (step.id, other, other, other, other))
    return found


def _ring_problems(cycle):
    """Dependency loops, in the words ``servicesfile.validate`` already uses.

    Deliberately the same message and the same algorithm as the ring check for
    service ``depends`` (``gui/cms_gui/servicesfile.py``). It is the same idea
    about the same kind of graph, and somebody who has met one should recognise
    the other rather than having to read it twice.
    """
    found = []
    for ring in _rings(cycle):
        if len(ring) == 2 and ring[0] == ring[1]:
            continue          # a self-loop, already reported in its own words
        # " -> " rather than an arrow glyph: a literal symbol renders as a box
        # in DejaVu and as its ASCII stand-in on Windows.
        found.append("%s wait for each other in a loop, so none of them could "
                     "ever start." % " -> ".join(ring))
    return found


def _rings(cycle):
    """Every dependency loop, each as the step ids going round it."""
    by_id = {step.id: step for step in cycle.steps}
    found, seen = [], set()
    for start in cycle.steps:
        if start.id in seen:
            continue
        stack, on_stack = [], set()

        def walk(step):
            if step.id in on_stack:
                # Report from where the loop closes, not from where we entered.
                found.append(stack[stack.index(step.id):] + [step.id])
                return True
            if step.id in seen:
                return False
            seen.add(step.id)
            stack.append(step.id)
            on_stack.add(step.id)
            for need in step.needs:
                dependency = by_id.get(need)
                if dependency is not None and walk(dependency):
                    break
            stack.pop()
            on_stack.discard(step.id)
            return False

        walk(start)
    return found


def is_valid(cycle, registry=None, raw=None):
    """True when nothing in :func:`problems` would stop this cycle running."""
    return not problems(cycle, registry, raw)


# -- shape --------------------------------------------------------------------
def layers(cycle):
    """``{step id: layer}`` - the longest path from a step with no dependencies.

    Longest rather than shortest, so a step never sits to the left of something
    it waits for. In ``a -> b -> d`` and ``a -> d``, ``d`` belongs after ``b``,
    and the shortest path would put it beside it.

    Assumes the graph is acyclic; :func:`problems` is what establishes that. A
    ring here would recurse forever, so the walk carries its own guard and
    treats a revisited step as layer 0 rather than looping - a broken cycle
    should still be drawable, since seeing it is how someone fixes it.
    """
    by_id = {step.id: step for step in cycle.steps}
    depth, walking = {}, set()

    def walk(step_id):
        if step_id in depth:
            return depth[step_id]
        if step_id in walking:
            return 0                   # a ring; problems() reports it properly
        step = by_id.get(step_id)
        if step is None:
            return 0
        walking.add(step_id)
        needs = [walk(need) for need in step.needs if need in by_id]
        walking.discard(step_id)
        depth[step_id] = 1 + max(needs) if needs else 0
        return depth[step_id]

    for step in cycle.steps:
        walk(step.id)
    return depth


def order(cycle):
    """The steps grouped into layers, in file order within each layer.

    Layer *n* can only start once every earlier layer is done, so this is a
    correct sequential order as well as a picture of what may overlap. The
    executor does not use it - it schedules on readiness, which starts a step
    the moment its own dependencies are done rather than waiting for its whole
    layer - but it is what a renderer draws and what a test reads.
    """
    depth = layers(cycle)
    grouped = {}
    for step in sorted(cycle.steps, key=lambda s: s.source_index):
        grouped.setdefault(depth.get(step.id, 0), []).append(step)
    return [grouped[key] for key in sorted(grouped)]


def upstream(cycle, step_ids):
    """Every step these ones wait for, however far back. Not the ones named.

    What a partial run has to have an answer for: run ``commit`` on its own and
    it still reads ``${steps.implement.outputs.tree}``, so something has to
    supply that or the step fails on a reference rather than on its work.
    """
    by_id = {step.id: step for step in cycle.steps}
    found, walking = set(), list(step_ids)
    while walking:
        step = by_id.get(walking.pop())
        if step is None:
            continue
        for need in step.needs:
            if need in by_id and need not in found:
                found.add(need)
                walking.append(need)
    return found - set(step_ids)


def downstream(cycle, step_ids):
    """Every step that waits on these, however far forward, plus these.

    "From here on": the step somebody picked and everything that could only
    have run after it. Inclusive, because the point of asking is to run it.
    """
    by_id = {step.id: step for step in cycle.steps}
    found = {one for one in step_ids if one in by_id}
    changed = True
    while changed:
        changed = False
        for step in cycle.steps:
            if step.id in found:
                continue
            if any(need in found for need in step.needs):
                found.add(step.id)
                changed = True
    return found


def to_document(cycle):
    """The cycle as the plain mapping its file contains.

    The inverse of :func:`parse_cycle`, and the other half of what an editor
    needs: :func:`to_graph` says what to *draw*, this says what to *change*.
    A front-end that cannot parse YAML - which is every front-end here, by
    design - edits this and hands it back to ``--cycle-save``, exactly as the
    scenario editor does with ``meta`` and ``steps``.

    Only what was actually written comes out. A step with no ``retry:`` does
    not gain one at its default, because round-tripping a file should not
    quietly fill it with things its author chose not to say.
    """
    document = {"id": cycle.id}
    for key, value in (("name", cycle.name), ("description", cycle.description),
                       ("project", cycle.project)):
        if value:
            document[key] = value
    if cycle.version and cycle.version != 1:
        document["version"] = cycle.version
    if cycle.variables or cycle.secrets:
        written = dict(cycle.variables)
        for name in cycle.secrets:
            written[name] = {"secret": True}
        for name in cycle.paths:
            # Written back as it was declared, so a round trip through the
            # editor does not quietly demote a path to ordinary text.
            written[name] = {"kind": PATH, "value": cycle.variables.get(name, "")}
        document["variables"] = written
    if cycle.subject is not None:
        document["subject"] = {name: getattr(cycle.subject, name)
                               for name in ("kind", "key", "title", "memory", "pin")
                               if getattr(cycle.subject, name)}
    document["steps"] = [_step_document(step) for step in
                         sorted(cycle.steps, key=lambda one: one.source_index)]
    return document


def _step_document(step):
    entry = {"id": step.id, "plugin": step.plugin}
    if step.label:
        entry["label"] = step.label
    if step.needs:
        entry["needs"] = list(step.needs)
    if step.condition:
        entry["if"] = step.condition
    if step.settings:
        entry["with"] = dict(step.settings)
    if step.timeout is not None:
        entry["timeout"] = step.timeout
    retry = {}
    if step.retry_attempts and step.retry_attempts != 1:
        retry["attempts"] = step.retry_attempts
    if step.retry_delay:
        retry["delay"] = step.retry_delay
    if retry:
        entry["retry"] = retry
    if step.on_failure and step.on_failure != STOP:
        entry["on_failure"] = step.on_failure
    if step.disabled:
        entry["disabled"] = True
    return entry


def to_graph(cycle):
    """The wire payload: what the canvas draws and what the events carry.

    The one producer of this shape. ``--cycle-show`` returns it and
    ``cycle.run.start`` carries it, so a front-end draws the same nodes whether
    a file was opened or a run was started, and nothing has to translate between
    a stored cycle and a running one.

    ``layer`` and ``row`` are computed here rather than in the front-end so the
    arrangement is the same everywhere and can be tested without a UI toolkit.
    They are a grid position, not pixels - how wide a node is and how far apart
    they sit is a question for whoever is drawing.
    """
    depth = layers(cycle)
    rows, seen = {}, {}
    for step in sorted(cycle.steps, key=lambda s: s.source_index):
        layer = depth.get(step.id, 0)
        rows[step.id] = seen.get(layer, 0)
        seen[layer] = rows[step.id] + 1

    return {
        "id": cycle.id,
        "name": cycle.title,
        "description": cycle.description,
        "project": cycle.project,
        "version": cycle.version,
        "variables": dict(cycle.variables),
        "secrets": list(cycle.secrets),
        "paths": list(cycle.paths),
        # What the canvas says the cycle works on before a run has found out:
        # the declaration, and the steps that will settle it.
        "subject": (dict(kind=cycle.subject.kind, key=cycle.subject.key,
                         title=cycle.subject.title, memory=cycle.subject.memory,
                         pin=cycle.subject.pin, sources=subject_sources(cycle))
                    if cycle.subject is not None else None),
        "nodes": [{"id": step.id,
                   "label": step.title,
                   "plugin": step.plugin,
                   "needs": list(step.needs),
                   "layer": depth.get(step.id, 0),
                   "row": rows[step.id],
                   "disabled": step.disabled,
                   "condition": step.condition,
                   "timeout": step.timeout,
                   "retry": step.retry_attempts,
                   "on_failure": step.on_failure}
                  for step in sorted(cycle.steps, key=lambda s: s.source_index)],
        "edges": [{"from": need, "to": step.id, "kind": "dependency"}
                  for step in sorted(cycle.steps, key=lambda s: s.source_index)
                  for need in step.needs],
    }
