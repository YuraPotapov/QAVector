"""Listening for work: what is new in the queue a cycle takes its task from.

A trigger names a step whose plugin declares itself ``watchable``. :func:`check`
asks that plugin, with the step's settings resolved from the cycle's own
variables and secrets, what the step would take now (``CyclePlugin.peek``), and
says which items it has not seen before; the application asks a person about
each one and starts the cycle only if they agree. Nothing here starts anything,
and nothing here knows what kind of queue it is - Jira or anything else is the
plugin's business.

**What "new" means.** The first check a trigger ever makes remembers everything
already in the queue and reports none of it: turning a listener on must not
greet somebody with a question per open task. After that, an issue is new until
it is marked seen - which the application does when it is started or ignored,
and deliberately does not do when somebody answers "not now", so it is asked
about again next time.

What has been seen lives in the memory store under ``watch/<cycle>/<trigger>``,
beside the records the cycles themselves keep, so it survives a restart and
can be read or forgotten like any other record.

**One offer or one per item - the step decides.** What is offered is what a
run started from the offer would take, and only the watched step knows that.
When the trigger's pin is one of the step's own inputs (``issue:
${vars.task_key}``), a run takes the one item it is started on, so every item
is offered on its own. When the step does not read the pin, a run takes
whatever the step returns - all the tasks ready for a release, say - so that
whole answer is a single offer, listing every item, made whenever any of them
is new. Its key is the items' keys joined by commas; seeing or planning it
sees or plans each of them. The listener adds no selection of its own: the
step's query and its limit are the offer.

**Planned.** An item nobody answered about in time - or that somebody chose to
look at later - is *planned*: kept in the same record with its title and when,
and no longer offered, until somebody starts it or removes it. Either of those
marks it seen, which is also what takes it off the plan. A whole-answer offer
planned replaces the one planned before it: the latest answer is what a run
would take now.
"""

import time


from cycle import memory, model, registry as default_registry, secrets as secret_store
from cycle import variables


class WatchError(Exception):
    """A trigger that cannot be checked: unknown, misconfigured, or Jira said no."""


def record_key(cycle_id, trigger_id):
    return "watch/%s/%s" % (cycle_id, trigger_id)


def trigger_of(cycle, trigger_id=""):
    """The named trigger, or the only one. Raises :class:`WatchError`."""
    if not cycle.triggers:
        raise WatchError("%s has no triggers" % cycle.id)
    if not trigger_id:
        if len(cycle.triggers) > 1:
            raise WatchError("%s has several triggers; name one" % cycle.id)
        return cycle.triggers[0]
    for one in cycle.triggers:
        if one.id == trigger_id:
            return one
    raise WatchError("%s has no trigger %r" % (cycle.id, trigger_id))


def plugin_for(cycle, trigger, registry=None):
    """The watched step and its plugin. Raises :class:`WatchError`."""
    registry = registry or default_registry
    step = cycle.step(trigger.watch)
    if step is None:
        raise WatchError("trigger %s watches %r, which is not a step"
                         % (trigger.id, trigger.watch))
    plugin = registry.get(step.plugin)
    if plugin is None or not plugin.metadata.watchable:
        raise WatchError("trigger %s watches %s, whose plugin %s cannot be watched"
                         % (trigger.id, step.id, step.plugin))
    return step, plugin


def settings_for(cycle, trigger, secrets_path=None, env=None, registry=None):
    """The watched step's settings, resolved as a run would resolve them.

    Only variables, secrets and the environment are available - no step has
    run - which is what a queue query reads anyway. A reference to another
    step's outputs is refused rather than left as text in the query.
    """
    step, _plugin = plugin_for(cycle, trigger, registry)
    for path in variables.references(step.settings):
        if path.startswith("steps."):
            raise WatchError("%s reads %s, which a listener cannot know before a "
                             "run" % (step.id, path))
    values = {name: secret_store.get(cycle.id, name, secrets_path)
              for name in cycle.secrets}
    # A secret the query reads and nobody has set is refused here, before the
    # plugin is asked anything. Resolved, it would be an empty string, and the
    # listener would send a request with a blank credential every few minutes
    # to whatever the file names - a template's placeholder site included.
    unset = sorted({path.split(".")[1] for path in variables.references(step.settings)
                    if path.startswith("vars.") and len(path.split(".")) > 1
                    and path.split(".")[1] in cycle.secrets
                    and not values.get(path.split(".")[1])})
    if unset:
        raise WatchError("%s has no value set for %s; set it in Settings -> "
                         "Cycle secrets. Nothing was asked until then."
                         % (cycle.id, ", ".join(unset)))
    scope = variables.scope(None, cycle, {}, env=env or {},
                            secrets={k: v for k, v in values.items() if v is not None})
    return variables.resolve(step.settings, scope)


def each_on_its_own(cycle, trigger):
    """True when a run takes the one item it is started on: the trigger's pin
    is an input of the watched step. False when the step does not read it, and
    a run takes everything the step returns."""
    pin = model.trigger_pin(cycle, trigger)
    step = cycle.step(trigger.watch)
    if not pin or step is None:
        return False
    return any(path == "vars." + pin or path.startswith("vars.%s." % pin)
               for path in variables.references(step.settings))


def keys_of(key):
    """The keys in an offer's key: one, or several joined by commas."""
    return [one.strip() for one in str(key).split(",") if one.strip()]


def whole(found):
    """One offer for everything a step returned: the run takes all of it."""
    keys = [one["key"] for one in found]
    if len(found) == 1:
        return dict(found[0], whole=True, items=list(found))
    return {"key": ",".join(keys), "title": "%d tasks" % len(found), "url": "",
            "whole": True, "items": list(found)}


def check(cycle, trigger_id="", secrets_path=None, memory_path=None, env=None,
          registry=None):
    """``{"trigger", "first", "new": [{key, title, ...}], "pin"}``.

    ``first`` is true on a trigger's very first check, when everything found is
    recorded as seen and nothing is reported new. For a step that does not
    read the pin, ``new`` holds at most one offer: everything the step returned,
    with ``whole`` true and the items under ``items``.
    """
    trigger = trigger_of(cycle, trigger_id)
    _step, plugin = plugin_for(cycle, trigger, registry)
    settings = settings_for(cycle, trigger, secrets_path, env, registry)
    each = each_on_its_own(cycle, trigger)
    try:
        found = [one for one in plugin.peek(settings, each=each) if one.get("key")]
    except Exception as exc:  # noqa: BLE001 - whatever the plugin raised, said
        raise WatchError(str(exc) or type(exc).__name__)
    key = record_key(cycle.id, trigger.id)
    first = not memory.known(key, memory_path)
    fields = memory.recall(key, memory_path)
    seen = set(fields.get("seen") or [])
    for one in fields.get("planned") or []:
        seen.update(keys_of(one.get("key", "")))
    if first:
        memory.remember(key, {"seen": sorted(one["key"] for one in found)},
                        memory_path)
        new = []
    elif each:
        new = [one for one in found if one["key"] not in seen]
    else:
        new = [whole(found)] if any(one["key"] not in seen for one in found) else []
    return {"cycle": cycle.id, "trigger": trigger.id, "first": first,
            "every": trigger.every, "pin": model.trigger_pin(cycle, trigger),
            "new": new}


def mark_seen(cycle, key, trigger_id="", memory_path=None):
    """Remember ``key`` as seen, so it is not offered again - and take it off
    the plan, since seen is what starting or removing a planned item means.
    A whole-answer key sees each of its items. Returns how many are seen."""
    trigger = trigger_of(cycle, trigger_id)
    record = record_key(cycle.id, trigger.id)
    fields = memory.recall(record, memory_path)
    seen = set(fields.get("seen") or [])
    seen.update(keys_of(key))
    planned = [one for one in fields.get("planned") or [] if one.get("key") != str(key)]
    memory.remember(record, {"seen": sorted(seen), "planned": planned}, memory_path)
    return len(seen)


def plan(cycle, key, title="", url="", trigger_id="", memory_path=None, when=None,
         items=None):
    """Keep ``key`` to be looked at later. Returns the plan for this trigger.

    Planned is not seen: it is not offered again, and it stays until somebody
    starts or removes it (:func:`mark_seen`). Planning one twice keeps the
    first time it was planned. For a step that does not read the pin, the
    offer planned is everything the step returned, and it replaces whatever
    was planned before: the latest answer is what a run would take.
    """
    trigger = trigger_of(cycle, trigger_id)
    record = record_key(cycle.id, trigger.id)
    planned = list(memory.recall(record, memory_path).get("planned") or [])
    entry = {"key": str(key), "title": str(title or ""),
             "url": str(url or ""), "at": when or time.time()}
    if items:
        entry["items"] = [{"key": str(one.get("key", "")),
                           "title": str(one.get("title", "")),
                           "url": str(one.get("url", ""))} for one in items]
    if not each_on_its_own(cycle, trigger):
        kept = [one for one in planned if one.get("key") == str(key)]
        if not kept:
            # The latest answer replaces the one planned before it - and keeps
            # its start time: a task that turns up at three must not cancel
            # the release somebody scheduled for six.
            when_run = [one["run_at"] for one in planned if one.get("run_at")]
            if when_run:
                entry["run_at"] = when_run[-1]
        planned = kept or [entry]
    elif not any(one.get("key") == str(key) for one in planned):
        planned.append(entry)
    memory.remember(record, {"planned": planned}, memory_path)
    return planned


def planned(memory_path=None):
    """Everything planned, across cycles, oldest first.

    Read from the memory store alone, so a cycle file that no longer exists
    still shows what was planned for it - and can be removed.
    """
    found = []
    for key in memory.keys("watch/", memory_path):
        parts = key.split("/")
        if len(parts) != 3:
            continue
        for one in memory.recall(key, memory_path).get("planned") or []:
            found.append(dict(one, cycle=parts[1], trigger=parts[2]))
    return sorted(found, key=lambda one: one.get("at") or 0)
