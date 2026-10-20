"""An edit of one cycle that is not written yet.

Building a cycle is a run of small changes - add a step, point it at the one
before, give it its settings - and almost every state in between is one the
core refuses to write: a step whose required field is still empty, a
dependency on a step not added yet. Saving each change as it was made meant
the editor could only move from one valid file to the next, which is not how
anybody builds a graph. So the changes collect here, the canvas and the
Problems tab show what they add up to, and nothing reaches the file until
somebody presses Save - through the core, which still writes nothing that does
not hold.

Plain data and no Qt, like the layout: every change is a function of a
document, so what the editor does can be tested without a window. The
document is the one ``cycle.model.to_document`` makes and ``--cycle-save``
takes; nothing here parses YAML or knows what a plugin does.

**The way back.** Every change is kept, so Undo and Redo walk through them;
Revert goes back to what the file said. What the file said is also kept as
``base`` - its digest when the draft began - so a save made after somebody
else changed the file is refused rather than quietly undoing their change.
"""

import copy
import re

#: The most changes Undo can walk back through. A cycle is small; this is only
#: a bound on memory for somebody who edits for an afternoon.
UNDO_LIMIT = 200

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")

#: A step's output read in a setting: ``${steps.<id>.outputs...}``.
_STEP_READ = re.compile(r"\$\{\s*steps\.([A-Za-z0-9_-]+)[.}\s]")

#: What a step carries that says where it is, not what it does.
_NOT_SETTINGS = ("id", "needs")


class Draft(object):
    """One cycle's unsaved document, its history, and what it was made from."""

    def __init__(self, cycle_id, document, base=""):
        self.cycle_id = cycle_id
        #: The digest of the file the draft was made from - see the module.
        self.base = base or ""
        self._saved = copy.deepcopy(document or {})
        self.document = copy.deepcopy(self._saved)
        self._undo, self._redo = [], []
        #: What the core last said about the document - its dry run.
        self.check = {}

    # -- state ----------------------------------------------------------------
    @property
    def dirty(self):
        return self.document != self._saved

    def can_undo(self):
        return bool(self._undo)

    def can_redo(self):
        return bool(self._redo)

    def change(self, document, merge=False):
        """Make ``document`` the draft, keeping the one before for Undo.

        ``merge`` folds this change into the last one, so Undo takes both
        back at once - a step added and then filled in is one change.
        """
        if document == self.document:
            return False
        if not merge or not self._undo:
            self._undo.append(copy.deepcopy(self.document))
            del self._undo[:-UNDO_LIMIT]
        self._redo = []
        self.document = copy.deepcopy(document)
        return True

    def undo(self):
        if not self._undo:
            return False
        self._redo.append(self.document)
        self.document = self._undo.pop()
        return True

    def redo(self):
        if not self._redo:
            return False
        self._undo.append(self.document)
        self.document = self._redo.pop()
        return True

    def saved(self, base=""):
        """The file now says what the draft does: nothing is unsaved."""
        self._saved = copy.deepcopy(self.document)
        self.base = base or self.base

    # -- reading it -----------------------------------------------------------
    def steps(self):
        return list(self.document.get("steps") or [])

    def step(self, step_id):
        for one in self.document.get("steps") or []:
            if one.get("id") == step_id:
                return one
        return None

    def phases(self):
        """The phases the steps are in, in the order they first appear."""
        found = []
        for one in self.document.get("steps") or []:
            name = (one.get("group") or "").strip()
            if name and name not in found:
                found.append(name)
        return found

    def unique_id(self, wanted):
        """``wanted`` as a step id nothing else has: ``shell``, ``shell_2``..."""
        stem = _UNSAFE.sub("_", (wanted or "step").strip()).strip("_-") or "step"
        taken = {one.get("id") for one in self.document.get("steps") or []}
        if stem not in taken:
            return stem
        number = 2
        while "%s_%d" % (stem, number) in taken:
            number += 1
        return "%s_%d" % (stem, number)

    def needs_of(self, step_id):
        """What ``step_id`` waits for directly, as a list whatever the file says."""
        return list(_needs(self.step(step_id) or {}))

    def upstream(self, step_id):
        """Every step ``step_id`` waits for, however far back."""
        needs = {one.get("id"): _needs(one) for one in self.document.get("steps") or []}
        found, walking = set(), [step_id]
        while walking:
            for need in needs.get(walking.pop(), ()):
                if need not in found:
                    found.add(need)
                    walking.append(need)
        return found

    def could_need(self, step_id, other):
        """Whether ``step_id`` may wait for ``other`` without a loop."""
        return (other != step_id and self.step(other) is not None
                and step_id not in self.upstream(other))

    def why_not_need(self, step_id, other):
        """Why ``step_id`` may not start waiting for ``other`` - "" if it may.

        Said the way the canvas says it while a link is dragged, so a refusal
        is a reason rather than a link that silently does not appear.
        """
        if self.step(step_id) is None or self.step(other) is None:
            return "No such step."
        if other == step_id:
            return "A step cannot wait for itself."
        if other in _needs(self.step(step_id)):
            return "%s already waits for %s." % (step_id, other)
        if step_id in self.upstream(other):
            return ("%s already waits for %s, so this would be a loop."
                    % (other, step_id))
        return ""

    def references_to(self, step_id):
        """The steps that name ``step_id``: in ``needs``, or in a ``${...}``."""
        pattern = re.compile(r"\$\{\s*steps\.%s[.}\s]" % re.escape(step_id))
        found = []
        for one in self.document.get("steps") or []:
            if one.get("id") == step_id:
                continue
            if step_id in _needs(one) or pattern.search(repr(one)):
                found.append(one.get("id"))
        return found

    def missing_reads(self, step_id):
        """The step ids ``step_id`` reads in a ``${steps.<id>...}`` that are not
        steps of this cycle, in the order they first appear.

        The usual cause is a step brought from another cycle, which still
        reads its old neighbour by the id that neighbour had there.
        """
        step = self.step(step_id)
        if step is None:
            return []
        have = {one.get("id") for one in self.document.get("steps") or []}
        found = []
        for name in _STEP_READ.findall(repr(_settings_of(step))):
            if name not in have and name not in found:
                found.append(name)
        return found

    def point_reads(self, step_id, missing, to, merge=False):
        """Make ``step_id`` read ``to`` wherever it read ``missing``.

        Only ``${steps.<missing>...}`` references in its own settings change;
        its id and ``needs`` are not settings. ``merge`` folds this into the
        last change, so a link and what it fixed go back with one Undo.
        """
        document = copy.deepcopy(self.document)
        target = next((one for one in document.get("steps") or []
                       if one.get("id") == step_id), None)
        if target is None or not missing or not to:
            return False
        pattern = re.compile(r"(\$\{\s*steps\.)%s(?=[.}\s])" % re.escape(missing))
        for key in list(target):
            if key not in _NOT_SETTINGS:
                target[key] = _replaced(target[key], pattern, r"\g<1>" + to)
        return self.change(document, merge=merge)

    # -- changing it ----------------------------------------------------------
    def add_step(self, plugin, after="", step=None, variables=None):
        """A new step of ``plugin``, waiting for ``after``. Returns its id.

        Placed straight after ``after`` in the file, so the file reads in the
        order the graph runs - which is also where the core's writer puts it
        without rewriting anything around it.

        ``variables`` are declarations the step refers to - a saved step's,
        brought from another cycle. Only the ones this cycle lacks are added,
        and in the same change, so one undo takes the step and them back out.
        A variable the cycle already has keeps its own value.
        """
        made = dict(step or {})
        made["id"] = self.unique_id(made.get("id") or plugin.split(".")[-1])
        made["plugin"] = plugin
        phases = self.document.get("phases") or []
        if phases and "phase" not in made:
            made["phase"] = (self.step(after) or {}).get("phase") or phases[0]["id"]
        if after and self.step(after) is not None and "needs" not in made:
            made["needs"] = [after]
            group = (self.step(after).get("group") or "").strip()
            if group and "group" not in made:
                made["group"] = group
        document = copy.deepcopy(self.document)
        if variables:
            declared = document.get("variables")
            if not isinstance(declared, dict):
                declared = document["variables"] = {}
            for name, value in variables.items():
                declared.setdefault(name, copy.deepcopy(value))
        steps = document.setdefault("steps", [])
        where = next((index for index, one in enumerate(steps)
                      if one.get("id") == after), None)
        steps.insert(len(steps) if where is None else where + 1, made)
        self.change(document)
        return made["id"]

    def replace_step(self, step_id, step):
        """Put an edited step where the old one was; a renamed id follows."""
        document = copy.deepcopy(self.document)
        steps = document.get("steps") or []
        for index, one in enumerate(steps):
            if one.get("id") == step_id:
                steps[index] = copy.deepcopy(step)
                break
        else:
            return False
        renamed = step.get("id")
        if renamed and renamed != step_id:
            for one in steps:
                needs = _needs(one)
                if step_id in needs:
                    one["needs"] = [renamed if need == step_id else need
                                    for need in needs]
            for trigger in document.get("triggers") or []:
                if isinstance(trigger, dict) and trigger.get("watch") == step_id:
                    trigger["watch"] = renamed
        return self.change(document)

    def remove_step(self, step_id):
        """Take a step out, and out of every ``needs`` that named it.

        A ``${steps.<id>...}`` somewhere else is left alone: what it should
        read instead is the author's decision, and the Problems tab names it.
        """
        document = copy.deepcopy(self.document)
        steps = document.get("steps") or []
        kept = [one for one in steps if one.get("id") != step_id]
        if len(kept) == len(steps):
            return False
        for one in kept:
            needs = _needs(one)
            if step_id in needs:
                rest = [need for need in needs if need != step_id]
                if rest:
                    one["needs"] = rest
                else:
                    one.pop("needs", None)
        document["steps"] = kept
        return self.change(document)

    def duplicate_step(self, step_id):
        """A copy of a step, beside it, waiting for what it waited for."""
        original = self.step(step_id)
        if original is None:
            return ""
        copied = copy.deepcopy(original)
        copied["id"] = self.unique_id(step_id)
        if copied.get("label"):
            copied["label"] += " (copy)"
        document = copy.deepcopy(self.document)
        steps = document["steps"]
        index = next(i for i, one in enumerate(steps) if one.get("id") == step_id)
        steps.insert(index + 1, copied)
        self.change(document)
        return copied["id"]

    def why_not_move(self, step_id, other, new_step_id, new_other):
        """Why the link ``step_id`` <- ``other`` may not become ``new_step_id``
        <- ``new_other`` - "" if it may. Judged without the old link, which
        is the one being moved and so no longer in the way."""
        if other not in self.needs_of(step_id):
            return "%s does not wait for %s." % (step_id, other)
        if (step_id, other) == (new_step_id, new_other):
            return ""
        trial = Draft("", self.document)
        trial.set_need(step_id, other, False)
        return trial.why_not_need(new_step_id, new_other)

    def move_need(self, step_id, other, new_step_id, new_other):
        """Point one link somewhere else, as one change - one Undo.

        When the same step goes on waiting, only for another, the new name
        takes the old one's place in its list, so the file reads in the order
        it did and the diff is one word.
        """
        if ((step_id, other) == (new_step_id, new_other)
                or self.why_not_move(step_id, other, new_step_id, new_other)):
            return False
        document = copy.deepcopy(self.document)
        steps = {one.get("id"): one for one in document["steps"]}
        old = steps[step_id]
        if new_step_id == step_id:
            old["needs"] = [new_other if need == other else need
                            for need in _needs(old)]
        else:
            rest = [need for need in _needs(old) if need != other]
            if rest:
                old["needs"] = rest
            else:
                old.pop("needs", None)
            steps[new_step_id]["needs"] = _needs(steps[new_step_id]) + [new_other]
        return self.change(document)

    def set_need(self, step_id, other, wanted):
        """Make ``step_id`` wait for ``other``, or stop. False if refused."""
        step = self.step(step_id)
        if step is None or (wanted and not self.could_need(step_id, other)):
            return False
        needs = _needs(step)
        if wanted == (other in needs):
            return False
        document = copy.deepcopy(self.document)
        target = next(one for one in document["steps"] if one.get("id") == step_id)
        needs = [need for need in needs if need != other] + ([other] if wanted else [])
        if needs:
            target["needs"] = needs
        else:
            target.pop("needs", None)
        return self.change(document)

    def set_group(self, step_id, group):
        document = copy.deepcopy(self.document)
        target = next((one for one in document.get("steps") or []
                       if one.get("id") == step_id), None)
        if target is None:
            return False
        if (group or "").strip():
            target["group"] = group.strip()
        else:
            target.pop("group", None)
        return self.change(document)


def _needs(step):
    needs = step.get("needs") or []
    if isinstance(needs, str):
        return [needs] if needs.strip() else []
    return [str(one) for one in needs]


def _settings_of(step):
    return {key: value for key, value in step.items() if key not in _NOT_SETTINGS}


def _replaced(value, pattern, replacement):
    """``value`` with ``pattern`` replaced in every string inside it."""
    if isinstance(value, str):
        return pattern.sub(replacement, value)
    if isinstance(value, dict):
        return {key: _replaced(one, pattern, replacement)
                for key, one in value.items()}
    if isinstance(value, list):
        return [_replaced(one, pattern, replacement) for one in value]
    return value
