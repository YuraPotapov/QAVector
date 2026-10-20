"""Saved steps: a configured step kept under a name, to add to any cycle.

A step worth having twice is usually one whose settings took a while to get
right - a Jira site, an account, a status - and differ from the next one in a
single field. Saved here, it is one pick from the Add step menu instead of a
form filled in again, in this cycle or another one.

**What a preset keeps.** The step's settings and label, and nothing that ties
it to the cycle it came from: no id (the new cycle names it), no ``needs`` (it
waits for whatever it is added after), no phase. A condition reading another
step's outputs is dropped as well, since that step is not where it is going.

**Variables travel with it, a secret's value through the store.** A step
reading ``${vars.jira_site}`` is no use in a cycle that has no ``jira_site``,
so the declarations it refers to are kept beside it and added to a cycle that
lacks them. A secret is kept here the way a cycle file keeps one -
``{secret: true}`` - because this file is somebody's plain JSON on disk. The
preset remembers the cycle it was saved from instead, and adding it copies
that cycle's value inside the secrets store, which is the one place it is
ever decrypted.

The module is the same shape as ``cycleprojectsfile``: **load, validate,
save**, with the writer refusing anything ``validate`` complains about, and
the file beside that one.
"""

import copy
import json
import os
import re

from . import cycleprojectsfile

#: Bumped when the shape changes in a way an older build could not read.
VERSION = 1

FILE_NAME = "cyclepresets.json"

#: What a row may carry. Anything else is kept but not understood.
KEYS = ("name", "plugin", "step", "variables", "cycle")

#: What a step carries that belongs to the cycle it is in, not to the step.
_PLACE = ("id", "plugin", "needs", "group", "phase")

#: A reference to a cycle variable, as ``cycle/variables.py`` reads one.
_VARIABLE = re.compile(r"\$\{\s*vars\.([A-Za-z0-9_-]+)")

#: A reference to another step, which a preset cannot bring along.
_STEP = re.compile(r"\$\{\s*steps\.")


class CyclePresetsFileError(Exception):
    """The presets file could not be read or written."""


class Preset(object):
    """One saved step: a name, the plugin, its settings, what it refers to."""

    def __init__(self, name="", plugin="", step=None, variables=None,
                 extra=None, cycle=""):
        self.name = (name or "").strip()
        #: The cycle it was saved from - where its secrets' values are.
        self.cycle = str(cycle or "").strip()
        self.plugin = (plugin or "").strip()
        self.step = dict(step or {})
        self.variables = dict(variables or {})
        self.extra = dict(extra or {})

    @classmethod
    def from_entry(cls, entry):
        if not isinstance(entry, dict):
            raise CyclePresetsFileError("a saved step has to be an object, "
                                        "not %s" % type(entry).__name__)
        step, variables = entry.get("step") or {}, entry.get("variables") or {}
        if not isinstance(step, dict) or not isinstance(variables, dict):
            raise CyclePresetsFileError("%s: step and variables have to be "
                                        "objects" % (entry.get("name") or "?"))
        return cls(name=entry.get("name", ""), plugin=entry.get("plugin", ""),
                   step=step, variables=variables,
                   cycle=entry.get("cycle", ""),
                   extra={key: value for key, value in entry.items()
                          if key not in KEYS})

    @classmethod
    def from_step(cls, name, step, document=None):
        """A preset of ``step``, as it stands in the cycle ``document``."""
        kept = {key: copy.deepcopy(value) for key, value in (step or {}).items()
                if key not in _PLACE}
        if _STEP.search(repr(kept.get("if", ""))):
            kept.pop("if")
        declared = (document or {}).get("variables") or {}
        variables = {}
        for wanted in _VARIABLE.findall(repr(kept)):
            if wanted not in declared or wanted in variables:
                continue
            value = declared[wanted]
            # Never the value of a secret, even if a cycle file held one.
            variables[wanted] = ({"secret": True}
                                 if isinstance(value, dict) and value.get("secret")
                                 else copy.deepcopy(value))
        return cls(name=name, plugin=(step or {}).get("plugin", ""), step=kept,
                   variables=variables, cycle=(document or {}).get("id", ""))

    def secrets(self):
        """The names of the secret variables it brings, sorted."""
        return sorted(name for name, value in self.variables.items()
                      if isinstance(value, dict) and value.get("secret"))

    def step_for(self):
        """The step to add, its id taken from the preset's name.

        ``jira_in_progress`` rather than ``issues_4``: a cycle holding five
        saved Jira steps is read by their ids in every ``${steps...}``.
        """
        made = copy.deepcopy(self.step)
        made["id"] = re.sub(r"[^a-z0-9]+", "_", self.name.casefold()).strip("_")
        return made

    def to_entry(self):
        entry = dict(self.extra)
        entry.update(name=self.name, plugin=self.plugin,
                     step=copy.deepcopy(self.step))
        if self.variables:
            entry["variables"] = copy.deepcopy(self.variables)
        if self.cycle:
            entry["cycle"] = self.cycle
        return entry

    def __repr__(self):
        return "Preset(%r, %r)" % (self.name, self.plugin)


def load(path):
    """Read the file. Returns ``[]`` when it is not there yet.

    A file that exists and cannot be understood is an error rather than an
    empty list: carrying on would mean the next save wrote over it.
    """
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8-sig") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as exc:
        raise CyclePresetsFileError("cannot read %s: %s" % (path, exc))
    if not isinstance(document, dict):
        raise CyclePresetsFileError("%s has to contain an object" % path)
    rows = document.get("presets")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise CyclePresetsFileError("%s: presets has to be a list" % path)
    return [Preset.from_entry(entry) for entry in rows]


def validate(presets):
    """Everything wrong with these rows, as messages. Never raises."""
    problems, seen = [], set()
    for index, preset in enumerate(presets):
        where = preset.name or "saved step %d" % (index + 1)
        if not preset.name:
            problems.append("%s has no name." % where)
        if not preset.plugin:
            problems.append("%s names no plugin." % where)
        key = preset.name.casefold()
        if key and key in seen:
            problems.append("%s: two saved steps share this name." % preset.name)
        seen.add(key)
    return problems


def save(path, presets):
    """Write the presets back, atomically, keeping one backup."""
    problems = validate(presets)
    if problems:
        raise CyclePresetsFileError("\n".join(problems))
    if not path:
        raise CyclePresetsFileError("No presets path configured.")
    payload = {"version": VERSION,
               "presets": [preset.to_entry() for preset in presets]}
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    temp = os.path.join(directory, ".%s.tmp" % os.path.basename(path))
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.path.exists(path):
            backup = path + ".bak"
            try:
                if os.path.exists(backup):
                    os.remove(backup)
                os.replace(path, backup)
            except OSError:
                pass    # a missing backup must never block the save itself
        os.replace(temp, path)
    except OSError as exc:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise CyclePresetsFileError("cannot write %s: %s" % (path, exc))
    return path


def grouped(presets):
    """``[(plugin, [preset, ...])]``: by the kind of step, each in saved order.

    One list serves every cycle and every project, so it grows - five Jira
    columns, a couple of shell commands, an agent - and a long run of names
    is easier to read by what each one is. Kinds by plugin id; within one, the
    order they were saved in rather than the alphabet, since one per column of
    a board is saved in the board's order and the alphabet puts To Do last.
    """
    groups = {}
    for preset in presets:
        groups.setdefault(preset.plugin, []).append(preset)
    return [(plugin, groups[plugin]) for plugin in sorted(groups)]


def path_beside(projects_path):
    """Where the file goes: beside the projects file, wherever that is.

    Not a setting of its own. Both are the Cycles section's own lists, and a
    person who moved one out of the default place meant the section's files.
    """
    where = projects_path or cycleprojectsfile.default_path()
    return os.path.join(os.path.dirname(os.path.abspath(where)), FILE_NAME)
