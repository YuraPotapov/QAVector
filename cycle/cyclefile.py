"""Read and write cycle files, so nothing outside the core has to know YAML.

The same job ``engine/flowfile.py`` does for scenarios, and for the same reason:
the GUI depends on PySide6 and nothing else, so the file format is the core's
business and the launcher exposes this over ``--cycle-show`` / ``--cycle-save``.

Two habits are copied deliberately rather than reinvented:

* **Nothing that does not parse is ever written.** A cycle is validated in a
  scratch tree staged in front of the real ones, so a file that turns out to be
  broken never exists on disk - not even for the moment it takes to find out.
  A cycle that cannot run is worse than no cycle: it fails when somebody starts
  it, minutes into the day they needed it.
* **The YAML is written by hand**, not with ``yaml.dump``. The tree is read by
  people, and dump would quote every key, sort them alphabetically and expand
  every list into a block - so a cycle saved through the editor would stop
  looking like the ones written by hand beside it.
* **A form's save is an edit, not a rewrite.** What a form hands back is laid
  over the file it came from (``cycle/cyclepatch.py``), so the comments and
  layout somebody wrote survive a change to one step. A save that would lose
  any is refused until a person has seen what goes - see :func:`save`.

Reading never raises for a broken file. A cycle that does not parse is exactly
the one somebody needs to open and fix, so the problems travel in the payload.
"""

import difflib
import hashlib
import os
import re
import shutil
import tempfile

from cycle import cyclepatch, loader, model

#: What may appear in a cycle id. A cycle id becomes a filename and a run
#: directory name, and a dot would be read as an extension, so both go.
_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")

#: The order keys are written in. Not alphabetical - this is the order somebody
#: reads a cycle in: what it is, then what it takes, then what it does.
_CYCLE_KEY_ORDER = ("id", "name", "description", "project", "project_folder", "version",
                    "variables", "subject", "triggers", "actions", "execution", "restrictions", "phases", "steps")
_TRIGGER_KEY_ORDER = ("id", "watch", "every", "pin", "enabled")
#: The order a subject reads in: what kind of thing, which one, then the rest.
_SUBJECT_KEY_ORDER = ("kind", "key", "title", "memory", "pin")
_STEP_KEY_ORDER = ("id", "plugin", "label", "group", "phase", "important", "importance_reason", "runtime",
                   "system", "restrictions", "needs", "if", "with",
                   "timeout", "retry", "on_failure", "disabled")


class CycleFileError(Exception):
    """A cycle could not be read or written."""


def safe_id(raw):
    """A cycle id that will still resolve to its own file. Never empty."""
    cleaned = _SAFE_ID.sub("_", (raw or "").strip()).strip("_-")
    return cleaned or "cycle"


# -- reading ------------------------------------------------------------------
def describe_cycle(cycle_id, cycles_dir=None, registry=None):
    """Everything an editor needs about one cycle, as plain data.

    Carries the file's own text *and* the graph: the YAML is what a person
    edits, the graph is what the canvas draws, and both open on the same file
    without the front-end parsing anything.
    """
    path = loader.cycle_path(cycle_id, cycles_dir)
    if not os.path.exists(path):
        raise CycleFileError("no cycle %r (looked at %s)" % (cycle_id, path))
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise CycleFileError("cannot read %s: %s" % (path, exc))

    writable = is_writable(path, cycles_dir)
    payload = {
        "id": cycle_id,
        "path": path,
        "writable": writable,
        "source": "user" if writable else "bundled",
        "yaml": text,
        # What the file said when it was read. Handed back to save() as
        # `base`, it is how an edit made here refuses to overwrite one made
        # somewhere else in the meantime.
        "digest": text_digest(text),
        "cycle": None,
        "document": None,
        "problems": [],
    }
    try:
        parsed, raw = loader.read_cycle(cycle_id, cycles_dir)
    except Exception as exc:              # noqa: BLE001 - the report IS the error
        payload["problems"].append(str(exc))
        return payload

    # The very payload the canvas draws and the run event carries. One producer,
    # so a cycle looks the same opened as it does running.
    payload["cycle"] = model.to_graph(parsed)
    # What to draw, and what to change: an editor that cannot parse YAML needs
    # both, and they are two different shapes of the same file.
    payload["document"] = model.to_document(parsed)
    payload["problems"] = model.problems(parsed, registry=registry, raw=raw)
    guidance = model.warnings(parsed)
    if guidance:
        payload["warnings"] = guidance
    return payload


def list_cycles(cycles_dir=None, registry=None):
    """Every cycle, with enough about each to list it without opening it.

    Kept cheap on purpose: this is what ``--describe`` calls on every start, and
    the splash waits on that. Parse problems are reported; per-plugin settings
    validation is left to :func:`describe_cycle`, which is asked for one cycle
    at a time.
    """
    return [list_row(cycle_id, path, cycles_dir, registry)
            for cycle_id, path in sorted(loader.cycle_files(cycles_dir).items())]


def list_row(cycle_id, path, cycles_dir=None, registry=None):
    """One cycle's row of :func:`list_cycles` - also what a save answers with,
    so a front-end can put the one row it changed in place instead of reading
    every cycle on the machine again."""
    writable = is_writable(path, cycles_dir)
    row = {"id": cycle_id, "name": "", "description": "", "project": "",
           "path": path, "writable": writable,
           "source": "user" if writable else "bundled",
           "steps": 0, "problems": [],
           # So a front-end can tell whether the cycle it has open changed
           # on disk without reading it again - see describe_cycle.
           "digest": text_digest(_read_text(path))}
    try:
        parsed, raw = loader.read_cycle(cycle_id, cycles_dir)
    except Exception as exc:              # noqa: BLE001 - a broken file is a row
        row["problems"] = [str(exc)]
        return row
    row["name"] = parsed.name
    row["description"] = parsed.description
    row["project"] = parsed.project
    row["steps"] = len(parsed.steps)
    # So a front-end knows which cycles to listen for without opening each.
    row["triggers"] = [dict(id=one.id, watch=one.watch, every=one.every,
                            pin=model.trigger_pin(parsed, one),
                            enabled=one.enabled)
                       for one in parsed.triggers]
    row["problems"] = model.problems(parsed, registry=registry, raw=raw)
    return row


def is_writable(path, cycles_dir=None):
    """True when ``path`` is in the tree we are allowed to write to.

    Keyed on location, not on filesystem permissions: the bundled tree is
    perfectly writable in a source checkout and still must not be edited through
    the app, because an upgrade replaces it wholesale.
    """
    root = os.path.normpath(os.path.abspath(loader.search_path(cycles_dir)[0]))
    target = os.path.normpath(os.path.abspath(path))
    return target == root or target.startswith(root + os.sep)


# -- writing ------------------------------------------------------------------
def render(document):
    """A cycle document as YAML text, written to be read.

    ``document`` is the mapping a cycle file contains. Keys come out in the
    order somebody reads them rather than alphabetically, and a list of one
    short string stays on its line - both because the tree beside this file was
    written by hand and should not become two dialects.
    """
    lines = []
    for key in _CYCLE_KEY_ORDER:
        if key not in document or key == "steps":
            continue
        value = document[key]
        if value in ("", None, {}, []):
            continue
        lines.extend(_top_entry(key, value))

    lines.append("")
    lines.append("steps:")
    for step in document.get("steps") or []:
        lines.extend(_render_step(step))
    return "\n".join(lines).rstrip() + "\n"


def _top_entry(key, value):
    """One key of the cycle itself, as the lines :func:`render` writes for it."""
    lines = []
    if key == "variables" and isinstance(value, dict):
        lines.append("variables:")
        for name in sorted(value):
            lines.extend(_variable_entry(name, value[name], 2))
    elif key == "triggers" and isinstance(value, list):
        # Written like steps: one entry each, the id first. Without this
        # branch a trigger was dropped by every save the application made,
        # which is how turning one off in the step's dialog deleted it.
        lines.append("triggers:")
        for one in value:
            if not isinstance(one, dict):
                continue
            names = _TRIGGER_KEY_ORDER + tuple(sorted(set(one) - set(_TRIGGER_KEY_ORDER)))
            first = True
            for name in names:
                if name not in one or one[name] in ("", None):
                    continue
                lines.append("%s%s: %s" % ("  - " if first else "    ", name,
                                           _scalar(one[name])))
                first = False
    elif key == "subject" and isinstance(value, dict):
        lines.append("subject:")
        for name in _SUBJECT_KEY_ORDER + tuple(sorted(set(value) - set(_SUBJECT_KEY_ORDER))):
            if value.get(name) not in ("", None):
                lines.extend(_render_value(name, value[name], 2))
    elif key == "steps" and isinstance(value, list):
        lines.append("steps:")
        for step in value:
            lines.extend(_render_step(step))
    else:
        lines.extend(_render_value(key, value, 0))
    return lines


def _variable_entry(name, value, indent):
    """One variable. A secret says only that it is one: its value lives in the
    secrets store, never here - this file is committed. A path keeps both its
    kind and its value; treating every declaration as secret discards it."""
    if isinstance(value, dict) and value.get("secret"):
        return ["%s%s: {secret: true}" % (" " * indent, name)]
    return _render_value(name, value, indent)


def _render_step(step, dash=2):
    """One step as a YAML list entry, its dash ``dash`` columns in."""
    lines = []
    for key in _STEP_KEY_ORDER:
        if key not in step:
            continue
        value = step[key]
        if value in ("", None, [], {}) and key not in ("disabled",):
            continue
        if key == "disabled" and not value:
            continue
        lines.extend(_step_entry(key, value, dash + 2))
    if lines:
        lines[0] = " " * dash + "- " + lines[0][dash + 2:]
    return lines


def _step_entry(key, value, indent):
    """One key of a step, at ``indent``."""
    pad = " " * indent
    if key == "with" and isinstance(value, dict):
        if not value:
            return ["%swith: {}" % pad]
        lines = ["%swith:" % pad]
        for name in sorted(value):
            lines.extend(_render_value(name, value[name], indent + 2))
        return lines
    if key == "retry" and isinstance(value, dict):
        return ["%sretry: {%s}" % (pad, ", ".join(
            "%s: %s" % (name, _scalar(value[name])) for name in sorted(value)))]
    if key == "needs" and isinstance(value, (list, tuple)):
        return ["%sneeds: [%s]" % (pad, ", ".join(map(str, value)))]
    return _render_value(key, value, indent)


class _Renderer(object):
    """The writer's vocabulary, as ``cyclepatch`` asks for it.

    Every value a patch writes is written by the functions above, so a cycle
    saved through a form and one written whole look the same wherever they
    were changed.
    """

    #: Which set of rules a nested mapping is written by, by (parent, key).
    _CHILDREN = {("cycle", "variables"): "variables",
                 ("cycle", "subject"): "subject",
                 ("step", "with"): "value"}

    def child(self, context, name):
        return self._CHILDREN.get((context, name), "value")

    def order(self, context):
        return {"cycle": _CYCLE_KEY_ORDER, "step": _STEP_KEY_ORDER,
                "subject": _SUBJECT_KEY_ORDER}.get(context)

    def entry(self, context, name, value, indent):
        if context == "cycle":
            return _top_entry(name, value)
        if context == "step":
            return _step_entry(name, value, indent)
        if context == "variables":
            return _variable_entry(name, value, indent)
        return _render_value(name, value, indent)

    def item(self, step, dash):
        return _render_step(step, dash)

    def inline(self, value):
        """``value`` on one line, or None when it needs lines of its own."""
        if isinstance(value, dict):
            if value and all(_flow_safe(one) and _flow_safe(key)
                             for key, one in value.items()):
                return "{%s}" % ", ".join("%s: %s" % (key, _scalar(value[key]))
                                          for key in sorted(value))
            return "{}" if not value else None
        if isinstance(value, (list, tuple)):
            if all(_flow_safe(one) for one in value):
                return "[%s]" % ", ".join(_scalar(one) for one in value)
            return None
        text = _scalar(value)
        return None if "\n" in str(value) else text


_RENDERER = _Renderer()


def _render_value(name, value, indent):
    """``name: value`` as YAML lines, nesting whatever is not a scalar.

    A step's ``with:`` is whatever its plugin declares, and plugins declare
    mappings (``agent.review``'s structured inputs) and lists (its list of files)
    as freely as they declare strings. Handing one of those to :func:`_scalar`
    produces ``inputs: "{'a': 1}"`` - a Python repr in quotes, which reads back
    as a string and makes the step fail validation on the next load. That is not
    a rendering blemish: it is a step the interface can open and then cannot
    save, and it hits every plugin with a mapping or a list field.
    """
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return ["%s%s: {}" % (pad, name)]
        lines = ["%s%s:" % (pad, name)]
        for key in sorted(value):
            lines.extend(_render_value(key, value[key], indent + 2))
        return lines
    if isinstance(value, (list, tuple)):
        if not value:
            return ["%s%s: []" % (pad, name)]
        if all(_flow_safe(item) for item in value):
            return ["%s%s: [%s]" % (pad, name,
                                    ", ".join(_scalar(item) for item in value))]
        lines = ["%s%s:" % (pad, name)]
        for item in value:
            if isinstance(item, dict) and item:
                keys = sorted(item)
                first = _render_value(keys[0], item[keys[0]], indent + 4)
                first[0] = "%s  - %s" % (pad, first[0].lstrip())
                lines.extend(first)
                for key in keys[1:]:
                    lines.extend(_render_value(key, item[key], indent + 4))
            else:
                lines.append("%s  - %s" % (pad, _scalar(item)))
        return lines
    if isinstance(value, str) and "\n" in value:
        block = _literal(value, indent + 2)
        if block is not None:
            return ["%s%s: %s" % (pad, name, block[0])] + block[1:]
    return ["%s%s: %s" % (pad, name, _scalar(value))]


def _literal(text, indent):
    """``text`` as a ``|`` block - the header, then its lines - or None.

    Several lines of prose - a task, a plan's question, a list of checks - are
    read far more often than they are written, and as one quoted line full of
    ``\\n`` they cannot be read at all. A block is used only when the parser
    gives exactly this text back; anything it would not (a first line that
    starts with a space, a control character, a carriage return) stays quoted.
    """
    if "\r" in text or any(ord(one) < 32 and one not in "\n\t" for one in text):
        return None
    body = text.rstrip("\n")
    trailing = len(text) - len(body)
    # Several trailing newlines would need "|+", whose blank lines the end of
    # a file is free to lose; one that rare stays quoted instead.
    if not body or body[0] in " \t" or trailing > 1:
        return None
    header = "|" if trailing == 1 else "|-"

    def block(pad):
        return [header] + [(pad + line) if line else "" for line in body.split("\n")]

    try:
        from engine import yamlload
    except ImportError:
        return None
    try:
        if yamlload.safe_load("v: " + "\n".join(block("  ")) + "\n") != {"v": text}:
            return None
    except Exception:  # noqa: BLE001 - anything unparseable stays quoted
        return None
    return block(" " * indent)


def _flow_safe(value):
    """Whether this may go inside ``[a, b]`` rather than on a line of its own.

    A comma or a bracket inside a value would end the sequence early, and
    :func:`_scalar` only quotes what is risky at the *start* of a value - which
    is the right rule everywhere except in flow form.
    """
    if isinstance(value, (dict, list, tuple)):
        return False
    return not any(character in _scalar(value) for character in ",[]{}\n")


def _scalar(value):
    """One value, quoted only when it would otherwise be read as something else."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if not text:
        return '""'
    if "\n" in text:
        return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"').replace(
            "\n", "\\n")
    quoted = '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"')

    # Quoted when the plain form would read back as something other than this
    # string. Asked of the parser rather than guessed at: what YAML gives a
    # meaning to is a long list that quietly grows - "12:30" is 750 seconds,
    # "2026-09-19" is a date, ".inf" is a float - and every one of those is a
    # value somebody typed into a form and would get back as something else.
    plain = _reads_back_as_text(text)
    if plain is not None:
        return text if plain else quoted

    # Only when pyyaml is not installed, which is not how anything writes a
    # file: save() validates through the loader, and that needs it.
    risky = text[0] in "-?:,[]{}#&*!|>'\"%@` " or text[-1] == " "
    reserved = text.lower() in ("true", "false", "null", "yes", "no", "on", "off",
                                "~")
    if risky or reserved or _looks_numeric(text) or ": " in text:
        return quoted
    return text


def _reads_back_as_text(text):
    """Whether ``key: <text>`` would be read as exactly this string.

    ``None`` when there is no parser to ask, so the caller falls back to the
    rules written out above.
    """
    try:
        from engine import yamlload
    except ImportError:
        return None
    try:
        return yamlload.safe_load("v: " + text) == {"v": text}
    except Exception:  # noqa: BLE001 - anything unparseable needs the quotes
        return False


def _looks_numeric(text):
    try:
        float(text)
        return True
    except ValueError:
        return False


def text_digest(text):
    """What a file said, as a short fingerprint - see :func:`save`'s ``base``."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _target(cycle_id, cycles_dir=None):
    """Where a save of ``cycle_id`` goes: the file already there, if it is ours.

    Not always the canonical ``<id>.yaml``. A cycle somebody wrote as
    ``nightly.yml`` used to gain a ``nightly.yaml`` beside it on its first
    save - which then won every lookup, so the edit looked saved, and deleting
    it brought the old file back.
    """
    root = loader.search_path(cycles_dir)[0]
    for extension in loader.EXTENSIONS:
        candidate = os.path.join(root, cycle_id + extension)
        if os.path.exists(candidate):
            return candidate
    return loader.canonical_path(cycle_id, cycles_dir)


def compose(document, origin=None):
    """``(text, lost comments)``: ``document`` written over ``origin``.

    ``origin`` is the file the document was made from, when there is one. The
    change is made to it as edits (``cycle/cyclepatch.py``), so whatever the
    document does not carry - comments above all - stays where it was. The
    whole document is written from scratch only when that cannot be done
    exactly, and in either case the comments that did not survive are named:
    the caller decides what to do about losing them, not this.
    """
    rendered = render(document)
    if not origin:
        return rendered, []
    text = rendered
    try:
        from engine import yamlload
        base = model.to_document(model.parse_cycle(yamlload.safe_load(origin) or {},
                                                   None))
        patched = cyclepatch.patch(origin, base, document, _RENDERER)
        if _meaning(patched) == _meaning(rendered):
            text = patched
    except (cyclepatch.PatchError, model.CycleError):
        pass
    except Exception:  # noqa: BLE001 - a patch that went wrong writes whole
        pass
    return text, cyclepatch.lost_comments(origin, text)


def _meaning(text):
    """What ``text`` says, in the one shape two writings can be compared in."""
    from engine import yamlload
    try:
        return model.to_document(model.parse_cycle(yamlload.safe_load(text) or {}, None))
    except Exception:  # noqa: BLE001
        return None


def diff(before, after, path=""):
    """A unified diff of a save, for a person deciding whether to make it."""
    return "".join(difflib.unified_diff(
        (before or "").splitlines(True), (after or "").splitlines(True),
        fromfile=path or "before", tofile=(path + " (saved)") if path else "after"))


def save(cycle_id, cycles_dir=None, yaml_text=None, document=None,
         registry=None, base=None, allow_loss=False, dry_run=False,
         source_id=None):
    """Validate a cycle and write it into the writable tree.

    Takes either the raw text - what a YAML view edits - or the document a form
    produces. Returns ``{ok, id, path, problems, digest}`` and writes nothing
    at all unless it parses and validates.

    A **document** is applied to the file as edits (see :func:`compose`), so
    comments and layout it does not carry survive. When some cannot - a step
    rewritten whole had a comment inside it - nothing is written unless
    ``allow_loss``: the answer says ``confirm``, lists what would go under
    ``lost`` and shows the change as ``diff``, for a person to agree to.

    ``base`` is the :func:`text_digest` of the file as the editor read it.
    When the file on disk no longer says that - somebody changed it in
    another editor, or in another window - nothing is written and the answer
    says ``stale``, rather than one edit silently undoing the other.

    ``dry_run`` answers everything a save would - the problems, the text, the
    diff, the graph - and writes nothing: what an editor checks a draft with.

    ``source_id`` names the file a document came from when it is not the one
    being written: a duplicate, or a cycle whose id is changing, keeps the
    comments of the file it was made from.
    """
    cycle_id = safe_id(cycle_id)
    path = _target(cycle_id, cycles_dir)
    answer = {"ok": False, "id": cycle_id, "path": path, "problems": [],
              "lost": [], "digest": ""}
    if not is_writable(path, cycles_dir):
        answer["problems"] = ["%s is not in the writable cycles tree" % path]
        return answer

    shown = loader.cycle_path(cycle_id, cycles_dir)
    current = _read_text(shown) if os.path.exists(shown) else None
    if base and current is not None and text_digest(current) != base:
        answer.update(stale=True, digest=text_digest(current), problems=[
            "%s changed on disk after it was opened here. Nothing was "
            "written, so neither version is lost - reload it to see the "
            "other change." % os.path.basename(shown)])
        return answer

    if yaml_text is None:
        origin = current
        if source_id:
            source = loader.cycle_path(safe_id(source_id), cycles_dir)
            origin = _read_text(source) if os.path.exists(source) else None
        yaml_text, answer["lost"] = compose(dict(document or {}, id=cycle_id),
                                            origin)
    answer["digest"] = text_digest(yaml_text)

    answer["problems"] = validate_text(cycle_id, yaml_text, cycles_dir, registry)
    if dry_run:
        answer.update(ok=not answer["problems"], text=yaml_text,
                      diff=diff(current, yaml_text, path),
                      **_shapes(yaml_text))
        return answer
    if answer["problems"]:
        return answer
    if answer["lost"] and not allow_loss:
        answer.update(confirm=True, diff=diff(current, yaml_text, path),
                      problems=["Saving this rewrites part of the file and drops "
                                "%d comment(s) written in it. Nothing was "
                                "written." % len(answer["lost"])])
        return answer
    try:
        _atomic_write(path, yaml_text)
    except OSError as exc:
        answer["problems"] = ["cannot write %s: %s" % (path, exc)]
        return answer
    answer["ok"] = True
    # What a front-end would otherwise start two more processes for straight
    # after: the cycle's row in the list (a whole --describe) and the cycle
    # as an editor opens it (a --cycle-show). The row is read without the
    # registry, the way --describe reads it, so the two cannot disagree.
    answer["row"] = list_row(cycle_id, path, cycles_dir)
    try:
        answer["shown"] = describe_cycle(cycle_id, cycles_dir, registry)
    except CycleFileError:
        pass
    return answer


def move(moves, cycles_dir=None, registry=None):
    """Rewrite ``project:`` in many cycles at once - one process, not two per
    cycle. ``moves`` is ``[{"id": ..., "project": ...}]``, an empty project
    meaning none. Each goes through :func:`save` as a document edit, so the
    rest of the file - comments included - stays as it was.

    Returns ``{ok, moved: [row, ...], failed: [{id, problem}, ...]}``.
    """
    moved, failed = [], []
    for one in moves or []:
        cycle_id = str((one or {}).get("id") or "")
        project = str((one or {}).get("project") or "").strip()
        try:
            shown = describe_cycle(cycle_id, cycles_dir, registry)
        except (CycleFileError, ValueError) as exc:
            failed.append({"id": cycle_id, "problem": str(exc)})
            continue
        document = shown.get("document")
        if not document or not shown.get("writable"):
            failed.append({"id": cycle_id, "problem": (
                "%s cannot be read" % cycle_id if not document
                else "%s ships with the application" % cycle_id)})
            continue
        document = dict(document)
        if project:
            document["project"] = project
        else:
            document.pop("project", None)
        answer = save(cycle_id, cycles_dir, document=document, registry=registry,
                      base=shown.get("digest") or None)
        if answer.get("ok"):
            moved.append(answer["row"])
        else:
            failed.append({"id": cycle_id, "problem": "; ".join(
                str(problem) for problem in answer.get("problems") or [])
                or "not written"})
    return {"ok": not failed, "moved": moved, "failed": failed}


def _shapes(text):
    """The graph and the document ``text`` makes, for an editor's preview."""
    from engine import yamlload
    try:
        parsed = model.parse_cycle(yamlload.safe_load(text) or {}, None)
    except Exception:  # noqa: BLE001 - the problems already say why
        return {"cycle": None, "document": None}
    return {"cycle": model.to_graph(parsed), "document": model.to_document(parsed)}


def validate_text(cycle_id, yaml_text, cycles_dir=None, registry=None):
    """Everything wrong with ``yaml_text`` read as ``cycle_id``. Never raises.

    Checked in a scratch tree staged in front of the real ones, so a file that
    does not hold up never touches disk.
    """
    scratch = tempfile.mkdtemp(prefix="qavector-cycle-check-")
    try:
        with open(os.path.join(scratch, cycle_id + ".yaml"), "w",
                  encoding="utf-8") as handle:
            handle.write(yaml_text)
        staged = [scratch] + loader.search_path(cycles_dir)
        parsed, raw = loader.read_cycle(cycle_id, staged)
    except Exception as exc:              # noqa: BLE001 - the report IS the error
        return [str(exc)]
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return model.problems(parsed, registry=registry, raw=raw)


def _atomic_write(path, text):
    """Write ``text``, keeping one backup, never leaving a partial file.

    The same shape ``engine/flowfile._atomic_write`` uses. A cycle is
    hand-edited work; losing it to a crash halfway through a save would be
    unforgivable.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                         prefix=".tmp-", delete=False)
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if os.path.exists(path):
            backup = path + ".bak"
            try:
                if os.path.exists(backup):
                    os.remove(backup)
                os.replace(path, backup)
            except OSError:
                pass   # a missing backup must never block the save itself
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def delete(cycle_id, cycles_dir=None):
    """Remove a cycle, refusing anything the application only ships.

    Every spelling of it in the writable tree goes: a ``.yml`` left behind a
    deleted ``.yaml`` would be the old cycle coming back from the dead.
    """
    cycle_id = safe_id(cycle_id)
    path = loader.cycle_path(cycle_id, cycles_dir)
    if not os.path.exists(path):
        return {"ok": False, "id": cycle_id, "path": path,
                "problems": ["no cycle %r" % cycle_id]}
    if not is_writable(path, cycles_dir):
        return {"ok": False, "id": cycle_id, "path": path,
                "problems": ["%s ships with the application and cannot be "
                             "deleted; duplicate it instead" % cycle_id]}
    root = loader.search_path(cycles_dir)[0]
    for extension in loader.EXTENSIONS:
        spelling = os.path.join(root, cycle_id + extension)
        if not os.path.exists(spelling):
            continue
        try:
            os.remove(spelling)
        except OSError as exc:
            return {"ok": False, "id": cycle_id, "path": spelling,
                    "problems": ["cannot delete %s: %s" % (spelling, exc)]}
    return {"ok": True, "id": cycle_id, "path": path, "problems": []}


def import_file(source, cycles_dir=None, registry=None):
    """Copy a cycle file into the writable tree, validating it first."""
    if not os.path.isfile(source):
        return {"ok": False, "id": "", "path": source,
                "problems": ["no such file: %s" % source]}
    try:
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        return {"ok": False, "id": "", "path": source,
                "problems": ["cannot read %s: %s" % (source, exc)]}

    cycle_id = safe_id(os.path.splitext(os.path.basename(source))[0])
    existing = loader.cycle_path(cycle_id, cycles_dir)
    if os.path.exists(existing):
        return {"ok": False, "id": cycle_id, "path": existing,
                "problems": ["a cycle called %r is already here; rename the "
                             "file or delete that one first" % cycle_id]}
    return save(cycle_id, cycles_dir, yaml_text=text, registry=registry)
