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

**Planned.** An item nobody answered about in time - or that somebody chose to
look at later - is *planned*: kept in the same record with its title and when,
and no longer offered, until somebody starts it or removes it. Either of those
marks it seen, which is also what takes it off the plan.
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
    scope = variables.scope(None, cycle, {}, env=env or {},
                            secrets={k: v for k, v in values.items() if v is not None})
    return variables.resolve(step.settings, scope)


def check(cycle, trigger_id="", secrets_path=None, memory_path=None, env=None,
          registry=None):
    """``{"trigger", "first", "new": [{key, title, ...}], "pin"}``.

    ``first`` is true on a trigger's very first check, when everything found is
    recorded as seen and nothing is reported new.
    """
    trigger = trigger_of(cycle, trigger_id)
    _step, plugin = plugin_for(cycle, trigger, registry)
    settings = settings_for(cycle, trigger, secrets_path, env, registry)
    try:
        found = [one for one in plugin.peek(settings) if one.get("key")]
    except Exception as exc:  # noqa: BLE001 - whatever the plugin raised, said
        raise WatchError(str(exc) or type(exc).__name__)
    key = record_key(cycle.id, trigger.id)
    first = not memory.known(key, memory_path)
    fields = memory.recall(key, memory_path)
    seen = set(fields.get("seen") or [])
    seen |= {one.get("key") for one in fields.get("planned") or []}
    if first:
        memory.remember(key, {"seen": sorted(one["key"] for one in found)},
                        memory_path)
        new = []
    else:
        new = [one for one in found if one["key"] not in seen]
    return {"cycle": cycle.id, "trigger": trigger.id, "first": first,
            "every": trigger.every, "pin": model.trigger_pin(cycle, trigger),
            "new": new}


def mark_seen(cycle, key, trigger_id="", memory_path=None):
    """Remember ``key`` as seen, so it is not offered again - and take it off
    the plan, since seen is what starting or removing a planned item means.
    Returns how many are seen."""
    trigger = trigger_of(cycle, trigger_id)
    record = record_key(cycle.id, trigger.id)
    fields = memory.recall(record, memory_path)
    seen = set(fields.get("seen") or [])
    seen.add(str(key))
    planned = [one for one in fields.get("planned") or [] if one.get("key") != str(key)]
    memory.remember(record, {"seen": sorted(seen), "planned": planned}, memory_path)
    return len(seen)


def plan(cycle, key, title="", url="", trigger_id="", memory_path=None, when=None):
    """Keep ``key`` to be looked at later. Returns the plan for this trigger.

    Planned is not seen: it is not offered again, and it stays until somebody
    starts or removes it (:func:`mark_seen`). Planning one twice keeps the
    first time it was planned.
    """
    trigger = trigger_of(cycle, trigger_id)
    record = record_key(cycle.id, trigger.id)
    planned = list(memory.recall(record, memory_path).get("planned") or [])
    if not any(one.get("key") == str(key) for one in planned):
        planned.append({"key": str(key), "title": str(title or ""),
                        "url": str(url or ""), "at": when or time.time()})
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
