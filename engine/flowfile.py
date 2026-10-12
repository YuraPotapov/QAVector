"""Read and write scenario files, so nothing outside the engine has to know YAML.

The GUI needs to list, edit and create scenarios, and it depends on PySide6 and
nothing else - by design, so the front-end and the launcher stay independent
environments. That makes the file format the core's business, not something the
two of them agree on: this module is the whole of it, and the launcher exposes it
over ``--flow-show`` / ``--flow-save`` (see session_launcher).

Everything written here goes back through the compiler before it reaches disk.
A file that does not compile is never written, because a scenario that cannot run
is worse than no scenario at all - it fails at the end of a launch, minutes later,
in front of whoever was watching.
"""

import os
import re
import shutil
import tempfile

from engine import compiler, loader
from engine.context import RunContext

#: Stand-in values for the four ``{{...}}`` placeholders flows may use.
#:
#: Templating is resolved against a real session at run time, so checking a
#: scenario has to supply *something* or every flow that says ``{{env.origin}}``
#: - which is most of them, through ``auth.login`` - would look broken. These
#: values are never written anywhere; they only have to be non-null, so that a
#: placeholder nobody defines still fails the check.
CHECK_CONTEXT = RunContext(user={"login": "check", "class": "Check"},
                           env={"origin": "http://example.invalid",
                                "url": "http://example.invalid/"})

#: What may appear in a scenario id. Anything else is replaced.
#:
#: The dot is the one that matters. Ids map to paths, and a dot means "directory"
#: (``auth.login`` -> ``flows/auth/login.yaml``), so a scenario saved as
#: ``my.thing`` would be discovered under the name ``my.thing`` and then looked
#: for at ``flows/my/thing.yaml``, which does not exist. It would vanish the
#: moment anyone tried to run it.
_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")

#: Written when a scenario has none, so `--run-tests=all` does not pick up a
#: half-finished recording. `template` is already the tree's word for "not ready".
DEFAULT_TAGS = ["template"]


class FlowFileError(Exception):
    """A scenario could not be read or written."""


def safe_id(raw):
    """A scenario id that will still resolve to its own file. Never empty."""
    cleaned = _SAFE_ID.sub("_", (raw or "").strip()).strip("_-")
    return cleaned or "scenario"


# -- reading ------------------------------------------------------------------
def describe_flow(flow_id, flows_dir=None):
    """Everything the editor needs about one scenario, as plain data.

    Carries the file's own text *and* its parsed steps: the YAML is what a person
    edits, the steps are what a form edits, and both views have to open on the
    same file without the front-end parsing anything.

    Never raises for a broken file - a scenario that does not compile is exactly
    the one someone needs to open and fix, so the problems travel in the payload.
    """
    path = loader.flow_path(flow_id, flows_dir)
    if not os.path.exists(path):
        raise FlowFileError("no scenario %r (looked at %s)" % (flow_id, path))
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise FlowFileError("cannot read %s: %s" % (path, exc))

    writable = is_writable(path, flows_dir)
    payload = {
        "id": flow_id,
        "path": path,
        "writable": writable,
        "source": "user" if writable else "bundled",
        "yaml": text,
        "meta": {"id": flow_id, "name": "", "description": "", "tags": []},
        "steps": [],
        "problems": [],
        "unresolved": {"use": [], "selectors": []},
    }
    try:
        flow = loader.load_flow(flow_id, flows_dir)
    except Exception as exc:
        payload["problems"].append(str(exc))
        return payload
    payload["meta"] = {"id": flow.id or flow_id, "name": flow.name or "",
                       "description": flow.description or "",
                       "tags": list(flow.tags or [])}
    payload["steps"] = [step_to_json(raw) for raw in flow.steps]
    payload["problems"] = _problems(flow_id, flows_dir)
    payload["unresolved"] = _unresolved(flow, flows_dir)
    return payload


def _problems(flow_id, flows_dir=None):
    """What stops this scenario compiling, as a list of sentences."""
    try:
        compiler.compile_scenario(flow_id, flows_dir,
                                  selectors=loader.load_selectors(flows_dir),
                                  ctx=CHECK_CONTEXT)
    except Exception as exc:
        return [str(exc)]
    return []


def _unresolved(flow, flows_dir=None):
    """Blocks and selector names this scenario mentions that do not exist here.

    Not errors: a raw CSS target is a perfectly ordinary thing to write, and only
    a missing ``use:`` actually breaks the compile. They are reported so the page
    can warn before someone exports a scenario into a tree that lacks them.
    """
    selectors = loader.load_selectors(flows_dir)
    missing_use, missing_selectors = [], []
    for raw in flow.steps or []:
        try:
            step = compiler.parse_step(raw)
        except Exception:
            continue
        if step.action == "use":
            if not os.path.exists(loader.flow_path(step.target, flows_dir)):
                missing_use.append(step.target)
        elif step.action in compiler.SELECTOR_TARGET and step.target:
            # A name is anything that could plausibly be one; raw CSS is not.
            if (step.target not in selectors
                    and re.fullmatch(r"[a-z][a-z0-9_]*", str(step.target))):
                missing_selectors.append(step.target)
    return {"use": sorted(set(missing_use)),
            "selectors": sorted(set(missing_selectors))}


def is_writable(path, flows_dir=None):
    """True when ``path`` is in the tree we are allowed to write to.

    Keyed on location, not on filesystem permissions: the bundled tree can be
    perfectly writable in a source checkout and still must not be edited through
    the app, because an upgrade replaces it wholesale.
    """
    root = loader.search_path(flows_dir)[0]
    root = os.path.normpath(os.path.abspath(root))
    target = os.path.normpath(os.path.abspath(path))
    return target == root or target.startswith(root + os.sep)


# -- writing ------------------------------------------------------------------
def render(meta, steps):
    """A scenario document as YAML text.

    Written by hand rather than with ``yaml.dump`` for one reason: the tree is
    read by people. dump would quote every key, order them alphabetically and
    expand every shorthand step into a mapping, so a recorded scenario would look
    nothing like the 53 sitting next to it.
    """
    flow_id = safe_id(meta.get("id") or "")
    lines = ["id: %s" % flow_id]
    if meta.get("name"):
        lines.append("name: %s" % _scalar(meta["name"]))
    if meta.get("description"):
        lines.append("description: %s" % _scalar(meta["description"]))
    tags = [str(tag) for tag in (meta.get("tags") or []) if str(tag).strip()]
    lines.append("tags: [%s]" % ", ".join(tags))
    if not steps:
        # An empty list, not a bare key: `steps:` alone parses as None, which the
        # loader rejects as malformed rather than reading as "no steps yet".
        lines.append("steps: []")
    else:
        lines.append("steps:")
        for step in steps:
            lines.extend(_render_step(step))
    return "\n".join(lines) + "\n"


def _render_step(step):
    """One step as YAML lines: shorthand when it fits, verbose when it must."""
    action = (step.get("action") or "").strip()
    target, value = step.get("target"), step.get("value")
    timeout, state, retry = step.get("timeout"), step.get("state"), step.get("retry")

    # state and retry exist only in the verbose form, and so does a timeout on
    # anything that is not a {target, value} action.
    verbose = state or retry or (
        timeout and action not in compiler.TARGET_AND_VALUE)
    if verbose:
        out = ["  - type: %s" % action]
        if target is not None:
            out.append("    target: %s" % _scalar(target))
        if value is not None:
            out.append("    value: %s" % _scalar(value))
        if state:
            out.append("    state: %s" % _scalar(state))
        if timeout:
            out.append("    timeout: %s" % int(timeout))
        if retry:
            out.append("    retry: {attempts: %s, delay: %s}"
                       % (int(retry.get("attempts", 1)), retry.get("delay", 1)))
        return out

    if action in compiler.TARGET_AND_VALUE:
        inner = "target: %s, value: %s" % (_scalar(target), _scalar(value))
        if timeout:
            inner += ", timeout: %s" % int(timeout)
        return ["  - %s: {%s}" % (action, inner)]
    if action in compiler.VALUE_ONLY:
        return ["  - %s: %s" % (action, _scalar(value))]
    return ["  - %s: %s" % (action, _scalar(target))]


def _scalar(value):
    """A YAML scalar that always reads back as the string it went in as.

    Quoting is not cosmetic here. An unquoted ``value: 100`` parses as an int, and
    ``assert_text_contains`` then does ``100 in text`` and raises TypeError mid-run;
    ``value: no`` parses as False. Quoting everything makes that impossible.
    """
    text = "" if value is None else str(value)
    return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"')


def step_to_json(raw):
    """One raw YAML step as the flat dict the editor and the recorder both use."""
    try:
        step = compiler.parse_step(raw)
    except Exception:
        # Unparseable steps still have to reach the editor - that is where they
        # get fixed. Show it as the mapping it is.
        return {"action": "", "target": "", "value": None, "raw": raw}
    return {"action": step.action, "target": step.target, "value": step.value,
            "state": step.state, "timeout": step.timeout, "retry": step.retry}


def save(flow_id, flows_dir=None, yaml_text=None, meta=None, steps=None):
    """Validate a scenario and write it into the writable tree.

    Takes either the raw text (what the YAML view edits) or meta+steps (what the
    step list and the recorder produce). Returns ``{ok, id, path, problems}`` and
    writes nothing at all unless it compiles.
    """
    flow_id = safe_id(flow_id)
    if yaml_text is None:
        yaml_text = render(dict(meta or {}, id=flow_id), steps or [])

    path = loader.canonical_path(flow_id, flows_dir)
    if not is_writable(path, flows_dir):
        return {"ok": False, "id": flow_id, "path": path,
                "problems": ["%s is not in the writable flows tree" % path]}

    problems = _validate_text(flow_id, yaml_text, flows_dir)
    if problems:
        return {"ok": False, "id": flow_id, "path": path, "problems": problems}
    try:
        _atomic_write(path, yaml_text)
    except OSError as exc:
        return {"ok": False, "id": flow_id, "path": path,
                "problems": ["cannot write %s: %s" % (path, exc)]}
    return {"ok": True, "id": flow_id, "path": path, "problems": []}


def _validate_text(flow_id, yaml_text, flows_dir=None):
    """Compile ``yaml_text`` as if it were already installed as ``flow_id``.

    Done in a scratch copy of the writable tree rather than in place, so a file
    that turns out not to compile never exists on disk - not even for the moment
    it takes to find out.
    """
    scratch = tempfile.mkdtemp(prefix="cms-flow-check-")
    try:
        staged = os.path.join(scratch, "scenarios")
        os.makedirs(staged, exist_ok=True)
        with open(os.path.join(staged, flow_id + ".yaml"), "w",
                  encoding="utf-8") as handle:
            handle.write(yaml_text)
        # The scratch tree in front of the real ones, so `use:` and named
        # selectors resolve exactly as they will once the file is in place.
        staged_path = [scratch] + loader.search_path(flows_dir)
        compiler.compile_scenario(flow_id, staged_path,
                                  selectors=loader.load_selectors(staged_path),
                                  ctx=CHECK_CONTEXT)
    except Exception as exc:
        return [str(exc)]
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return []


def _atomic_write(path, text):
    """Write ``text`` to ``path``, keeping one backup, never a partial file.

    Same shape as the users.json writer in the GUI: temp file, fsync, keep a
    .bak, rename into place. A scenario is hand-edited work; losing it to a
    crash halfway through a save would be unforgivable.
    """
    directory = os.path.dirname(path)
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


def delete(flow_id, flows_dir=None):
    """Remove a scenario, refusing anything the app only ships."""
    flow_id = safe_id(flow_id)
    path = loader.flow_path(flow_id, flows_dir)
    if not os.path.exists(path):
        return {"ok": False, "id": flow_id, "path": path,
                "problems": ["no scenario %r" % flow_id]}
    if not is_writable(path, flows_dir):
        return {"ok": False, "id": flow_id, "path": path,
                "problems": ["%s ships with the application and cannot be deleted; "
                             "duplicate it instead" % flow_id]}
    try:
        os.remove(path)
    except OSError as exc:
        return {"ok": False, "id": flow_id, "path": path,
                "problems": ["cannot delete %s: %s" % (path, exc)]}
    return {"ok": True, "id": flow_id, "path": path, "problems": []}


# -- selectors ----------------------------------------------------------------
# The named targets flows point at. They are the vocabulary a scenario is written
# in - `click: menu_settings` rather than a CSS path - so an editor that cannot
# show what a name resolves to leaves you reading aliases with nothing behind
# them, and one that cannot add a name makes every new scenario reach for raw CSS.

def describe_selectors(flows_dir=None):
    """The merged selector map, plus the user's own file as editable text."""
    merged = loader.load_selectors(flows_dir)
    bundled = {}
    roots = loader.search_path(flows_dir)
    for root in roots[1:]:
        path = os.path.join(root, "selectors.yaml")
        if os.path.exists(path):
            data = loader._load_yaml(path)
            if isinstance(data, dict):
                bundled.update(data)

    own = os.path.join(roots[0], "selectors.yaml")
    text = ""
    if os.path.exists(own):
        try:
            with open(own, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            return {"path": own, "writable": False, "yaml": "",
                    "entries": {}, "problems": [str(exc)]}
    entries = {}
    for name, value in merged.items():
        entries[name] = {
            "value": value,
            # "overridden" is worth its own word: the name exists in both trees
            # and what runs is the user's, which is exactly the thing that is
            # confusing to discover later from a failing selector.
            "source": ("overridden" if name in bundled and bundled[name] != value
                       else "bundled" if name in bundled else "user"),
        }
    return {"path": own, "writable": is_writable(own, flows_dir), "yaml": text,
            "entries": entries, "problems": []}


def save_selectors(yaml_text, flows_dir=None):
    """Write the user's selectors.yaml, refusing anything that is not a map."""
    from engine import yamlload

    path = os.path.join(loader.search_path(flows_dir)[0], "selectors.yaml")
    if not is_writable(path, flows_dir):
        return {"ok": False, "path": path,
                "problems": ["%s is not in the writable flows tree" % path]}
    try:
        data = yamlload.safe_load(yaml_text)
    except Exception as exc:
        return {"ok": False, "path": path, "problems": [str(exc)]}
    if data is not None and not isinstance(data, dict):
        return {"ok": False, "path": path,
                "problems": ["selectors.yaml must be a mapping of name: selector"]}
    for name, value in (data or {}).items():
        if not isinstance(value, str):
            return {"ok": False, "path": path,
                    "problems": ["%s: a selector must be a string, got %r"
                                 % (name, value)]}
    try:
        _atomic_write(path, yaml_text)
    except OSError as exc:
        return {"ok": False, "path": path,
                "problems": ["cannot write %s: %s" % (path, exc)]}
    return {"ok": True, "path": path, "problems": []}


def import_file(source_path, flows_dir=None, flow_id=None):
    """Copy a scenario file into the writable tree, validating it first.

    The id comes from the file name unless one is given, and is sanitised either
    way - a file called ``my.thing.yaml`` would otherwise import to an id that
    resolves to a directory that does not exist.
    """
    try:
        with open(source_path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        return {"ok": False, "id": "", "path": source_path,
                "problems": ["cannot read %s: %s" % (source_path, exc)]}
    base = os.path.basename(source_path)
    if base.endswith(".yaml"):
        base = base[:-5]
    elif base.endswith(".yml"):
        base = base[:-4]
    return save(flow_id or base, flows_dir, yaml_text=text)
