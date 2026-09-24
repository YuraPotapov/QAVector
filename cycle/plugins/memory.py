"""Reading and writing what the project remembers between runs.

Three steps over ``cycle/memory.py``, and the split is the point: reading is
harmless and a step that only reads should say so, writing changes what later
runs believe, and holding a task is a thing that has to be given back when the
run ends whatever way it ended.

    memory.recall     what is known about this key, as outputs to branch on
    memory.remember   put something in the record, or move a counter
    memory.claim      take this task for this run, and give it back at the end

**Keys mean whatever the cycle says.** A key is a string the step supplies,
usually built out of the cycle's own variables - `${vars.project}/${vars.issue}`
and so on. The core does not decide what a task is; two cycles that disagree
about identity simply do not collide, and nobody has to argue with the engine
about whether a branch is part of one.

**What a later run can do with it.** `memory.recall` puts the record in its
outputs, so the next step guards on it the ordinary way::

    - id: seen
      plugin: memory.recall
      with: {key: "${vars.issue}"}

    - id: work
      plugin: agent.implement
      needs: [seen]
      if: "${steps.seen.outputs.fields.status} != 'done'"

That is the whole mechanism by which a cycle stops redoing an afternoon's work.
"""

from cycle import memory, registry
from cycle.registry import (CyclePlugin, ManagedPlugin, PluginMetadata, field,
                            output)


class MemoryRecall(CyclePlugin):
    metadata = PluginMetadata(
        id="memory.recall", name="Recall", category=registry.ACTION,
        summary="Read what earlier runs remembered about this key. Reads only.",
        inputs=(
            field("key", "Key", required=True,
                  hint="Whatever identifies this work to you - usually built "
                       "from the cycle's own variables. Two cycles that mean "
                       "different things by a key do not collide."),
            field("field", "The one worth guarding on",
                  hint="Its value comes back as `value`, empty when nothing "
                       "was remembered - so `if: ${steps.<id>.outputs.value} "
                       "!= 'done'` holds on the first run too. Reaching into "
                       "`fields` directly fails the step when the name is not "
                       "there yet, which on a first run is every name."),
            field("default", "What it is before anything wrote it",
                  hint="`value` on a key that has never been written to. "
                       "Empty unless you say otherwise, which is right for a "
                       "guard and wrong for a counter: a budget read as '' "
                       "compares as 'not numbers' and reads like a broken "
                       "gate rather than like an unspent budget. Write '0'."),
        ),
        outputs=(
            output("known", "boolean",
                   "Whether anything was ever remembered about this key."),
            output("value", "text",
                   "The named field, or empty. Always present, which is what "
                   "makes it safe to write a condition against."),
            output("fields", "mapping",
                   "Everything remembered. Reach into it with "
                   "${steps.<id>.outputs.fields.<name>} only where you know "
                   "the name is there."),
            output("holder", "text",
                   "The run that holds this key right now, or empty."),
            output("updated_at", "number", "When it was last written."),
        ),
    )

    def execute(self, context, step):
        key = str(self.setting(step.settings, "key") or "").strip()
        if not key:
            return registry.failed("A key is required.")
        found = memory.describe(key, context.memory_path)
        fields = found.get("fields") or {}
        name = str(self.setting(step.settings, "field", "") or "").strip()
        # The stored value wins whenever there is one, so a counter that has
        # reached zero again is nought rather than whatever the default says.
        blank = str(self.setting(step.settings, "default", "") or "")
        return registry.succeeded(
            known=bool(found),
            value=("" if not name else str(fields.get(name, blank))),
            fields=fields,
            holder=memory.holder(key, context.memory_path),
            updated_at=found.get("updated_at") or 0,
            message=("memory: %d thing%s known about %s"
                     % (len(fields), "" if len(fields) == 1 else "s", key))
            if found else "memory: nothing known about %s" % key)


class MemoryRemember(CyclePlugin):
    metadata = PluginMetadata(
        id="memory.remember", name="Remember", category=registry.ACTION,
        summary="Write something into this key's record for later runs.",
        permissions=("filesystem.write",),
        inputs=(
            field("key", "Key", required=True,
                  hint="The same key a later run will recall by."),
            field("fields", "What to remember", "env",
                  hint="Merged into the record rather than replacing it, so "
                       "two steps can each remember their own part. A blank "
                       "value removes that field."),
            field("bump", "Counters to move", "args",
                  hint="Names of counters to add one to, and the reason they "
                       "are here rather than in fields: a counter read and "
                       "written by two runs loses one of them, so the store "
                       "moves it under its own lock."),
        ),
        outputs=(
            output("fields", "mapping", "The record as it now stands."),
        ),
    )

    def problems(self, settings):
        settings = settings or {}
        found = super().problems(settings)
        counters = settings.get("bump")
        if counters is not None and not isinstance(counters, (list, str)):
            found.append("Counters to move must be names, one list or one "
                         "line of them.")
        return found

    def execute(self, context, step):
        key = str(self.setting(step.settings, "key") or "").strip()
        if not key:
            return registry.failed("A key is required.")
        fields = self.setting(step.settings, "fields", {}) or {}
        counters = _names(self.setting(step.settings, "bump", []))
        try:
            held = memory.remember(key, fields, context.memory_path) if fields \
                else memory.recall(key, context.memory_path)
            for name in counters:
                memory.bump(key, name, path=context.memory_path)
            if counters:
                held = memory.recall(key, context.memory_path)
        except memory.MemoryError_ as exc:
            return registry.failed(str(exc))
        return registry.succeeded(
            fields=held,
            message="memory: remembered %d thing%s about %s"
                    % (len(held), "" if len(held) == 1 else "s", key))


def _names(value):
    """A list of names from a list, or from the string somebody wrote instead.

    The form always produces a list, and a YAML file written by hand often does
    not: ``bump: runs`` is a string, and iterating a string gives you one
    counter per letter. Splitting it is what the field's own hint promises, and
    a silent four-counter answer is the worst of the three possibilities.
    """
    if isinstance(value, str):
        return value.split()
    return [str(one) for one in (value or [])]


class MemoryClaim(ManagedPlugin):
    metadata = PluginMetadata(
        id="memory.claim", name="Claim", category=registry.ACTION,
        summary="Take this task for this run, so a second run does not start "
                "the same work. Given back when the run ends.",
        permissions=("filesystem.write",),
        inputs=(
            field("key", "Key", required=True,
                  hint="What is being claimed. Usually the same key the rest "
                       "of the cycle recalls and remembers by."),
            field("owner", "Owner",
                  hint="Blank uses this run's id, which is almost always what "
                       "you want. A name of your own lets a resumed run take "
                       "back a claim it already had."),
            field("seconds", "How long it is good for", "number",
                  default=int(memory.CLAIM_SECONDS),
                  hint="A claim expires so a machine that crashed does not "
                       "hold a task until somebody notices."),
        ),
        outputs=(
            output("claimed", "boolean", "Whether this run took it."),
            output("owner", "text", "Who holds it."),
            output("key", "text", "What was claimed."),
        ),
    )

    def problems(self, settings):
        settings = settings or {}
        literal = dict(settings)
        # Held back from the generic number check, which cannot know that a
        # reference becomes a number once the run resolves it.
        if isinstance(literal.get("seconds"), str) and "${" in literal["seconds"]:
            literal.pop("seconds")
        found = super().problems(literal)
        seconds = settings.get("seconds", memory.CLAIM_SECONDS)
        if not (isinstance(seconds, str) and "${" in seconds):
            try:
                if isinstance(seconds, bool) or float(seconds) <= 0:
                    raise ValueError()
            except (TypeError, ValueError):
                found.append("How long it is good for must be a positive "
                             "number of seconds.")
        return found

    def execute(self, context, step):
        """Take it, or fail loudly. Held so the run gives it back either way.

        ``ManagedPlugin.execute`` always succeeds, which is right for a service
        that started; a claim that somebody else holds has to fail instead, so
        this one is written out. What it keeps is the holding: the run stops
        what it holds in its own cleanup, so a crashed step, a timeout and a
        Ctrl+C all release the claim without this plugin knowing about any of
        them.
        """
        key = str(self.setting(step.settings, "key") or "").strip()
        if not key:
            return registry.failed("A key is required.")
        owner = (str(self.setting(step.settings, "owner", "") or "").strip()
                 or context.run_id or "a run with no id")
        seconds = float(self.setting(step.settings, "seconds",
                                     memory.CLAIM_SECONDS))
        try:
            ok, held = memory.claim(key, owner, seconds, context.memory_path)
        except memory.MemoryError_ as exc:
            return registry.failed(str(exc))
        if not ok:
            return registry.failed(
                "%s is already being worked on by %s. Nothing was started; "
                "this is not a failure of the work, only of the timing."
                % (key, held))

        context.hold(step.id, self, (key, owner))
        return registry.succeeded(claimed=True, owner=owner, key=key,
                                  message="memory: claimed %s" % key)

    def start(self, context, step):        # pragma: no cover - execute is ours
        raise NotImplementedError("memory.claim writes its own execute")

    def stop(self, context, step, handle):
        """Give the claim back. Only ours to give, which release() checks."""
        key, owner = handle
        memory.release(key, owner, context.memory_path)

    def status(self, context, step, handle):
        return {"claimed": True, "key": handle[0], "owner": handle[1]}
