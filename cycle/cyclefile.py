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

Reading never raises for a broken file. A cycle that does not parse is exactly
the one somebody needs to open and fix, so the problems travel in the payload.
"""

import os
import re
import shutil
import tempfile

from cycle import loader, model

#: What may appear in a cycle id. A cycle id becomes a filename and a run
#: directory name, and a dot would be read as an extension, so both go.
_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")

#: The order keys are written in. Not alphabetical - this is the order somebody
#: reads a cycle in: what it is, then what it takes, then what it does.
_CYCLE_KEY_ORDER = ("id", "name", "description", "project", "version",
                    "variables", "subject", "triggers", "steps")
_TRIGGER_KEY_ORDER = ("id", "watch", "every", "pin", "enabled")
#: The order a subject reads in: what kind of thing, which one, then the rest.
_SUBJECT_KEY_ORDER = ("kind", "key", "title", "memory", "pin")
_STEP_KEY_ORDER = ("id", "plugin", "label", "needs", "if", "with", "timeout",
                   "retry", "on_failure", "disabled")


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
    return payload


def list_cycles(cycles_dir=None, registry=None):
    """Every cycle, with enough about each to list it without opening it.

    Kept cheap on purpose: this is what ``--describe`` calls on every start, and
    the splash waits on that. Parse problems are reported; per-plugin settings
    validation is left to :func:`describe_cycle`, which is asked for one cycle
    at a time.
    """
    rows = []
    for cycle_id, path in sorted(loader.cycle_files(cycles_dir).items()):
        writable = is_writable(path, cycles_dir)
        row = {"id": cycle_id, "name": "", "description": "", "project": "",
               "path": path, "writable": writable,
               "source": "user" if writable else "bundled",
               "steps": 0, "problems": []}
        try:
            parsed, raw = loader.read_cycle(cycle_id, cycles_dir)
        except Exception as exc:          # noqa: BLE001 - a broken file is a row
            row["problems"] = [str(exc)]
            rows.append(row)
            continue
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
        rows.append(row)
    return rows


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
        if key == "variables":
            lines.append("variables:")
            for name in sorted(value):
                one = value[name]
                # A secret says only that it is one. Its value lives in the
                # secrets store, never here - this file is committed.
                if isinstance(one, dict) and one.get("secret"):
                    lines.append("  %s: {secret: true}" % name)
                else:
                    # Paths carry both a kind and a value. Keep the mapping;
                    # treating every declaration as secret discards the path.
                    lines.extend(_render_value(name, one, 2))
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
        else:
            lines.append("%s: %s" % (key, _scalar(value)))

    lines.append("")
    lines.append("steps:")
    for step in document.get("steps") or []:
        lines.extend(_render_step(step))
    return "\n".join(lines).rstrip() + "\n"


def _render_step(step):
    """One step as a YAML list entry."""
    lines, first = [], True
    for key in _STEP_KEY_ORDER:
        if key not in step:
            continue
        value = step[key]
        if value in ("", None, [], {}) and key not in ("disabled",):
            continue
        if key == "disabled" and not value:
            continue

        prefix = "  - " if first else "    "
        first = False
        if key == "with":
            lines.append("%swith:" % prefix)
            for name in sorted(value):
                lines.extend(_render_value(name, value[name], 6))
        elif key == "retry":
            lines.append("%sretry: {%s}" % (
                prefix, ", ".join("%s: %s" % (name, _scalar(value[name]))
                                  for name in sorted(value))))
        elif key == "needs":
            lines.append("%sneeds: [%s]" % (prefix, ", ".join(map(str, value))))
        else:
            lines.append("%s%s: %s" % (prefix, key, _scalar(value)))
    return lines


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
    return ["%s%s: %s" % (pad, name, _scalar(value))]


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
        import yaml
    except ImportError:
        return None
    try:
        return yaml.safe_load("v: " + text) == {"v": text}
    except Exception:  # noqa: BLE001 - anything unparseable needs the quotes
        return False


def _looks_numeric(text):
    try:
        float(text)
        return True
    except ValueError:
        return False


def save(cycle_id, cycles_dir=None, yaml_text=None, document=None,
         registry=None):
    """Validate a cycle and write it into the writable tree.

    Takes either the raw text - what a YAML view edits - or the document a form
    produces. Returns ``{ok, id, path, problems}`` and writes nothing at all
    unless it parses and validates.
    """
    cycle_id = safe_id(cycle_id)
    if yaml_text is None:
        yaml_text = render(dict(document or {}, id=cycle_id))

    path = loader.canonical_path(cycle_id, cycles_dir)
    if not is_writable(path, cycles_dir):
        return {"ok": False, "id": cycle_id, "path": path,
                "problems": ["%s is not in the writable cycles tree" % path]}

    problems = validate_text(cycle_id, yaml_text, cycles_dir, registry)
    if problems:
        return {"ok": False, "id": cycle_id, "path": path,
                "problems": problems}
    try:
        _atomic_write(path, yaml_text)
    except OSError as exc:
        return {"ok": False, "id": cycle_id, "path": path,
                "problems": ["cannot write %s: %s" % (path, exc)]}
    return {"ok": True, "id": cycle_id, "path": path, "problems": []}


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
    """Remove a cycle, refusing anything the application only ships."""
    cycle_id = safe_id(cycle_id)
    path = loader.cycle_path(cycle_id, cycles_dir)
    if not os.path.exists(path):
        return {"ok": False, "id": cycle_id, "path": path,
                "problems": ["no cycle %r" % cycle_id]}
    if not is_writable(path, cycles_dir):
        return {"ok": False, "id": cycle_id, "path": path,
                "problems": ["%s ships with the application and cannot be "
                             "deleted; duplicate it instead" % cycle_id]}
    try:
        os.remove(path)
    except OSError as exc:
        return {"ok": False, "id": cycle_id, "path": path,
                "problems": ["cannot delete %s: %s" % (path, exc)]}
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
