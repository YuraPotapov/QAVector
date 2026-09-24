"""Holding a branch of the graph until a time, without holding a thread.

``needs:`` says a step goes after another one. This says a step goes after a
*time* - and everything that needs it waits too, which is the whole point: a
cycle that has to let a deploy settle, or start its evening half at nine, says
so in the graph rather than in a `sleep` somebody has to find inside a shell
step.

**Why this is not ``command.shell: sleep 600``.** A sleeping command holds a
worker for ten minutes, so a cycle with four of them and the default four jobs
stops running anything at all. This gives the worker back and is woken when its
time comes, so a run may be waiting on a dozen things at once and still be
working on everything else. It is also visible: the step sits in ``waiting``
with what it is waiting for and how long is left, instead of looking like a
command that has hung.

**It keeps no state, because there is none to keep.** Each turn is a fresh
call, so "how long have I been waiting" is answered from the run record - the
step's first start, which the executor deliberately preserves across waits.
That is also what makes the answer survive being woken early: the step simply
works out what is left and asks again.

**A deadline still wins.** The step's own ``timeout:`` bounds the wait, so
``seconds: 3600`` under ``timeout: 60`` times out after the minute rather than
the hour. Two ways of saying how long something may take, and the smaller one
is the one that means anything.
"""

import time
from datetime import datetime, timedelta

from cycle import registry
from cycle.registry import CyclePlugin, PluginMetadata, field, output

#: Accepted spellings of an absolute time, most complete first. Date and time,
#: then a time today, with and without seconds. Deliberately a short list of
#: unambiguous forms rather than a parser: a cycle file is read by people, and
#: "03/04" meaning two different days in two countries is not worth supporting.
FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
           "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M",
           "%H:%M:%S", "%H:%M")

#: The forms above that carry no date, and so mean "today, or tomorrow if that
#: has already gone by".
TIME_ONLY = ("%H:%M:%S", "%H:%M")


class Wait(CyclePlugin):
    metadata = PluginMetadata(
        id="time.wait", name="Wait", category=registry.UTILITY,
        asks_when_overdue=False,
        summary="Hold this branch until a time. Gives its worker back while it waits.",
        inputs=(
            field("seconds", "Wait for", "number",
                  hint="Seconds from when the step first started - not from "
                       "each time it is looked at again. Ignored when a time "
                       "is given below."),
            field("until", "Wait until", "text",
                  hint="A clock time - 09:00 - or a date and time - "
                       "2026-09-22T09:00. A time on its own means today, or "
                       "tomorrow if it has already gone by. Read in this "
                       "machine's own time zone."),
            field("reason", "What it is waiting for", "text",
                  hint="Shown while it waits. A step that says 'letting the "
                       "deploy settle' is readable a month later; one that "
                       "says nothing is a cycle that appears to have hung."),
        ),
        outputs=(
            output("waited_seconds", "number", "How long it actually waited."),
            output("until", "text", "The moment it was waiting for."),
        ),
    )

    def problems(self, settings):
        settings = settings or {}
        found = super().problems(settings)
        for key in ("until", "reason"):
            if key in settings and not isinstance(settings[key], str):
                found.append("%s must be text." % key)

        until = str(settings.get("until") or "").strip()
        if until and "${" not in until and _format_of(until) is None:
            found.append("Wait until: %r is not a time. Write it as 09:00 or "
                         "2026-09-22T09:00." % until)
        if not until and settings.get("seconds") in (None, ""):
            # Neither given is not a wait of zero, it is a step nobody
            # finished writing - and silently succeeding would hide that.
            found.append("Give either a number of seconds or a time to wait "
                         "until.")
        seconds = settings.get("seconds")
        if (seconds not in (None, "") and "${" not in str(seconds)
                and _number(seconds) is not None and _number(seconds) < 0):
            found.append("Wait for: %r is negative." % seconds)
        return found

    def execute(self, context, step):
        problems = self.problems(step.settings)
        if problems:
            return registry.failed("; ".join(problems))
        context.cancel.raise_if_set()

        began = context.started_at(step.id) or time.time()
        target = self._target(step, began)
        if target is None:
            return registry.failed("Neither a duration nor a time to wait for.")

        left = target - time.time()
        spoken = _spoken(target)
        reason = str(self.setting(step.settings, "reason", "") or "").strip()
        if left > 0:
            context.stage(step.id, {
                "kind": "start", "status": "running",
                "title": "Waiting %s" % _rough(left),
                "detail": reason or "until %s" % spoken})
            return registry.waiting(
                left, message="waiting %s%s" % (_rough(left),
                                                " - " + reason if reason else ""),
                until=spoken)

        context.stage(step.id, {"kind": "result", "status": "done",
                                "title": "Waited until %s" % spoken,
                                "detail": reason})
        return registry.succeeded(
            message="waited %s" % _rough(time.time() - began),
            waited_seconds=round(max(0.0, time.time() - began), 3), until=spoken)

    def _target(self, step, began):
        """The moment this step is waiting for, in epoch seconds, or None.

        ``until`` wins over ``seconds`` when both are written. An absolute time
        is the more specific of the two, and refusing the pair would fail a
        cycle over something with an obvious reading.
        """
        until = str(self.setting(step.settings, "until", "") or "").strip()
        if until:
            return _at(until, began)
        seconds = _number(self.setting(step.settings, "seconds"))
        return None if seconds is None else began + max(0.0, seconds)


# -- reading a time -----------------------------------------------------------
def _format_of(text):
    """Which of :data:`FORMATS` ``text`` is written in, or None."""
    for one in FORMATS:
        try:
            datetime.strptime(text, one)
            return one
        except ValueError:
            continue
    return None


def _at(text, began):
    """``text`` as epoch seconds, relative to when the step started.

    A time with no date means the next time the clock says it - today if that
    is still ahead, tomorrow otherwise. Relative to the step's own start rather
    than to now, so a step woken a moment early does not decide the time it was
    waiting for is tomorrow's.
    """
    shape = _format_of(text)
    if shape is None:
        return None
    read = datetime.strptime(text, shape)
    if shape not in TIME_ONLY:
        return read.timestamp()
    start = datetime.fromtimestamp(began)
    moment = start.replace(hour=read.hour, minute=read.minute,
                           second=read.second, microsecond=0)
    if moment < start:
        moment += timedelta(days=1)
    return moment.timestamp()


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _spoken(target):
    """A moment as somebody would read it back."""
    return datetime.fromtimestamp(target).strftime("%Y-%m-%d %H:%M:%S")


def _rough(seconds):
    """A duration at the precision anybody cares about at that size."""
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return "%ds" % round(seconds)
    if seconds < 5400:
        return "%dm" % round(seconds / 60.0)
    return "%.1fh" % (seconds / 3600.0)
