"""What a plugin is, what describes one, and the table of the built-in ones.

A **plugin** is one kind of thing a cycle step can do: run a command, start a
service, run a scenario, write a report. What differs between them is small and
regular - which settings they take, what they produce, and what they are allowed
to touch - so each is a class with a :class:`PluginMetadata`, and the built-in
ones are one entry each in :data:`PLUGINS`.

**Not discovered by scanning.** That is the shape the rest of this application
already uses for things that grow - ``gui/cms_gui/runnertypes.py`` says it
plainly for service kinds, and ``engine/assertions.py``, ``commands.FLAGS``,
``icons.DRAWINGS`` and ``serverlog.FORMATS`` are the same idea. Adding a plugin
is adding a class and a name to the tuple at the bottom; nothing imports itself
into existence, and the set is greppable. :func:`register` exists so an external
plugin can be added later without that changing, but nothing calls it today and
nothing scans a directory looking for something to call it with.

**Forms are generated, not written.** Each plugin declares :attr:`~CyclePlugin.
metadata`'s ``inputs`` as :class:`Field` entries, the same namedtuple shape
``runnertypes`` uses, and a front-end builds its configuration form from them.
So the form and :meth:`~CyclePlugin.problems` read one table and cannot drift
apart, and the Inspector needs no per-plugin widget.

**Blocking, not async - a deliberate departure from the design document.** That
document writes ``async def``. There is no asyncio anywhere in this repository;
Playwright's sync API is bound to the greenlet of the thread that created it;
``engine.services.request`` blocks on a ``threading.Event``; ``engine/runner.py``
is a ``ThreadPoolExecutor`` throughout. An event loop here would mean either
driving all of that through ``run_in_executor`` - every cost of async, none of
its benefit - or rewriting the existing engine, which is explicitly not what
this feature is for. So a plugin blocks, and the executor runs plugins on
threads. Do not "fix" this back to async without rewriting the engine first.

**Permissions are declared, not enforced.** :attr:`PluginMetadata.permissions`
records what a plugin says it needs, so that the day a sandbox exists there is
something to enforce and every plugin already answers the question. Nothing
sandboxes anything today. A permission list here is documentation, and must
never be read as a guarantee.
"""

import time
from collections import namedtuple
# Aliased, because this module has a `field` of its own - the form-field helper,
# named to match ``runnertypes.field`` so the two read as one idea.
from dataclasses import dataclass, field as dataclass_field

from domain.cycle import FAILED, SKIPPED, SUCCESS, WAITING

#: The shortest a step can ask to wait. A plugin that keeps returning zero -
#: a bug, or a condition it believes is about to change - then spins at four a
#: second instead of as fast as the machine allows.
MIN_WAIT = 0.25

#: One field of a generated form. The same shape as ``runnertypes.Field``, on
#: purpose: somebody who has met one should recognise the other.
#:
#: ``kind`` is what a front-end builds:
#:   ``text``      a line edit
#:   ``multiline`` a text area
#:   ``number``    a line edit that only takes a number
#:   ``check``     a checkbox
#:   ``args``      a line edit split the way a shell would split it
#:   ``env``       a small name/value grid
#:   ``file``      a line edit with a file chooser
#:   ``dir``       a line edit with a directory chooser
#:   ``choice``    a drop-down; ``options`` says of what
#:   ``scenario``  a scenario id, offered from the inventory
#:   ``service``   a "Project/Service" reference, offered from services.json
Field = namedtuple("Field", "key label kind hint required default options")

#: One thing a plugin produces, for ``${steps.<id>.outputs.<key>}``. Declared
#: rather than merely returned so the Inspector can show what a step offers
#: before it has ever run - which is when somebody is writing the step that
#: reads it.
Output = namedtuple("Output", "key type hint")

#: Something a plugin can be asked to do *outside* a run - check whether it is
#: signed in, sign in, test a connection, install what it needs.
#:
#: This exists so that setting a plugin up belongs to the plugin. The
#: alternative - a row in the application's own Settings - reads fine for the
#: two plugins that ship today and is wrong the moment somebody adds a third:
#: a plugin is meant to be replaceable without touching Core, and a plugin that
#: needs a setting only Core can render is not. So a plugin declares what it can
#: be asked, exactly as it declares its fields, and a front-end offers whatever
#: it finds without knowing what any of it means.
#:
#: ``kind`` is what the front-end does with it:
#:   ``status``   a question. The plugin answers; the answer is shown.
#:   ``command``  something to run. The plugin says what; the front-end runs it
#:                and can watch it. Used where the work is interactive - a
#:                browser sign-in is not something to run headless and wait on.
Action = namedtuple("Action", "key label kind hint")

ACTION_KINDS = ("status", "command")

#: Field kinds a front-end is expected to know how to draw.
KINDS = ("text", "multiline", "number", "check", "args", "env", "file", "dir",
         "choice", "scenario", "service")

#: Categories, for grouping in a UI. They organise; they constrain nothing.
ACTION = "action"
SERVICE = "service"
TEST = "test"
SCENARIO = "scenario"
ANALYTICS = "analytics"
AGENT = "agent"
REPORT = "report"
TRIGGER = "trigger"
UTILITY = "utility"
CATEGORIES = (ACTION, SERVICE, TEST, SCENARIO, ANALYTICS, AGENT, REPORT,
              TRIGGER, UTILITY)

#: What a plugin may declare it touches. Declarative only - see the module
#: docstring. The names are coarse because a finer vocabulary would imply a
#: precision nothing behind it has.
PERMISSIONS = ("filesystem.read", "filesystem.write", "process.spawn",
               "network", "service.control", "browser")


def field(key, label, kind="text", hint="", required=False, default=None,
          options=()):
    """One input field. Everything but ``key`` and ``label`` has a sane default."""
    return Field(key, label, kind, hint, required, default, tuple(options))


def output(key, kind="string", hint=""):
    return Output(key, kind, hint)


def action(key, label, kind="status", hint=""):
    return Action(key, label, kind, hint)


@dataclass(frozen=True)
class PluginMetadata:
    """Everything about a plugin that is not its behaviour.

    Frozen because this is a description, and a description that could be
    changed at run time would make ``--describe``'s answer a guess about what
    the run will actually do.
    """

    id: str
    name: str
    version: str = "1.0.0"
    category: str = ACTION
    summary: str = ""
    inputs: tuple = ()             # tuple[Field]
    outputs: tuple = ()            # tuple[Output]
    #: What this plugin can be asked outside a run - see :data:`Action`. This is
    #: how setting a plugin up stays the plugin's own business rather than a row
    #: in the application's Settings that only Core could render.
    actions: tuple = ()            # tuple[Action]
    permissions: tuple = ()
    #: True when this holds something open for later steps and must be stopped
    #: at the end of the run - see :class:`ManagedPlugin`.
    background: bool = False
    #: Steps whose plugins share a non-empty group never run at the same time.
    #: The escape hatch for something that is not safe to run twice at once;
    #: ``scenario.run`` uses it because ``engine/runner.py`` keeps its stop flag
    #: and its report directory in module globals.
    concurrency_group: str = ""
    #: Whether a partial run may take this step's result from an earlier run
    #: instead of doing it again (``--cycle-only``, ``--cycle-from``; see
    #: ``executor._borrow``). True for almost everything: the point of running
    #: part of a cycle is not repeating the expensive part.
    #:
    #: False says the result is a fact about *this* run and cannot be inherited.
    #: ``approval.gate`` is the case it exists for - a person's agreement given
    #: an hour ago to a different run is not agreement to this one, and reusing
    #: it silently passed a gate that nobody had been asked about. A step whose
    #: plugin says this runs in every run, selected or not.
    reusable: bool = True
    #: The plugin checkpoints its external calls and may finish local work
    #: after a crash when every call has a complete receipt.
    recoverable: bool = False
    #: Whether a step of this plugin that runs past its ``timeout:`` is put to
    #: the person watching - continue or cancel - rather than stopped outright
    #: (see ``executor._ask_overdue``). False where the deadline is the plugin's
    #: own business: a gate's timeout is how long its question waits, a wait's
    #: is what bounds it, and a service request is given the same number.
    asks_when_overdue: bool = True
    #: Whether a step of this plugin can be listened to: asked, outside any
    #: run, what it would take now - see :meth:`CyclePlugin.peek`. A cycle's
    #: ``triggers:`` may watch only such a step. What "a queue" means is the
    #: plugin's own business; the engine only compares keys.
    watchable: bool = False

    def to_dict(self):
        """What ``--describe`` publishes, so a front-end can build its own form."""
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "category": self.category,
            "summary": self.summary,
            "background": self.background,
            "concurrency_group": self.concurrency_group,
            "reusable": self.reusable,
            "recoverable": self.recoverable,
            "asks_when_overdue": self.asks_when_overdue,
            "watchable": self.watchable,
            "permissions": list(self.permissions),
            "inputs": [{"key": one.key, "label": one.label, "kind": one.kind,
                        "hint": one.hint, "required": one.required,
                        "default": one.default, "options": list(one.options)}
                       for one in self.inputs],
            "outputs": [{"key": one.key, "type": one.type, "hint": one.hint}
                        for one in self.outputs],
            "actions": [{"key": one.key, "label": one.label, "kind": one.kind,
                         "hint": one.hint} for one in self.actions],
        }


@dataclass
class PluginResult:
    """What one execution of a plugin came to.

    The status vocabulary here is narrower than a step's: a plugin says whether
    what it was asked to do worked. Whether that makes the step ``cancelled``,
    or ``timeout``, or whether the run continues at all, is the executor's to
    decide, and a plugin returning ``running`` or ``cancelled`` would be
    claiming an authority it does not have.

    ``waiting`` is the one that is not a verdict. It says *I am not finished,
    and there is no point asking me again before this time* - which is
    something the plugin genuinely knows and the executor cannot work out. What
    the executor does with it stays the executor's decision: it frees the
    worker, leaves the step unfinished, and comes back at :attr:`resume_at`.
    """

    status: str = SUCCESS              # SUCCESS | FAILED | SKIPPED | WAITING
    outputs: dict = dataclass_field(default_factory=dict)
    artifacts: list = dataclass_field(default_factory=list)   # list[Artifact]
    metrics: dict = dataclass_field(default_factory=dict)
    message: str = ""
    #: Monotonic seconds - ``time.monotonic()``, not wall clock - at which this
    #: step is worth running again. Only read when ``status`` is ``waiting``.
    #: Monotonic because it is a deadline inside one process and a wall clock
    #: that steps back over a DST change would make a wait finish early or run
    #: for an extra hour.
    resume_at: float = None
    #: An explicit human request to revisit a read-only plan before approval.
    revision: dict = dataclass_field(default_factory=dict)

    @property
    def ok(self):
        return self.status == SUCCESS

    @property
    def unfinished(self):
        """Whether this is a plugin asking to be come back to, not a verdict."""
        return self.status == WAITING


def succeeded(message="", **outputs):
    """The common case, written the short way."""
    return PluginResult(SUCCESS, outputs=outputs, message=message)


def failed(message, **outputs):
    return PluginResult(FAILED, outputs=outputs, message=message)


def skipped(message=""):
    return PluginResult(SKIPPED, message=message)


def waiting(seconds, message="", **outputs):
    """Not finished, and not worth asking again for ``seconds``.

    The step keeps its place in the graph and its worker is given back, so a
    cycle waiting an hour for something is not a cycle holding a thread for an
    hour. Everything downstream stays blocked, which is the point: waiting is
    what ``needs`` already means, said about time instead of about a dependency.

    ``seconds`` is how long from now, because that is what a plugin knows -
    "the deploy said 90 seconds", "check again in five minutes". A floor of
    :data:`MIN_WAIT` is applied so a plugin that keeps asking for zero spins
    slowly rather than hot.
    """
    return PluginResult(WAITING, outputs=outputs, message=message,
                        resume_at=time.monotonic() + max(MIN_WAIT,
                                                         float(seconds or 0)))


class CyclePlugin(object):
    """One kind of step: what it takes, what it produces, and what it does."""

    metadata = None

    # -- the form -------------------------------------------------------------
    def problems(self, settings):
        """Everything wrong with these settings, as messages. Never raises.

        The generic half - required fields present, numbers that are numbers,
        choices that are among the choices - is here, so a plugin only writes
        down what is peculiar to it. Exactly what ``RunnerType.problems`` does
        for service kinds.
        """
        found = []
        settings = settings or {}
        for spec in self.metadata.inputs:
            value = settings.get(spec.key)
            missing = value is None or (isinstance(value, str) and not value.strip())
            if spec.required and missing and spec.default is None:
                found.append("%s is required." % spec.label)
                continue
            if missing:
                continue
            if spec.kind == "number":
                try:
                    float(value)
                except (TypeError, ValueError):
                    found.append("%s: %r is not a number." % (spec.label, value))
            elif spec.kind == "choice" and spec.options and value not in spec.options:
                found.append("%s: %r is not one of %s."
                             % (spec.label, value, ", ".join(map(str, spec.options))))
            elif spec.kind == "env" and not isinstance(value, dict):
                found.append("%s must be a mapping of names to values." % spec.label)
        for key in sorted(set(settings) - {one.key for one in self.metadata.inputs}):
            found.append("unknown setting %r for %s."
                         % (key, self.metadata.id))
        return found

    def setting(self, settings, key, fallback=None):
        """One setting, falling back to the field's declared default.

        So a plugin reads ``self.setting(settings, "timeout")`` and gets the
        default it wrote down in its own metadata, rather than repeating it in
        two places that then disagree.
        """
        settings = settings or {}
        if key in settings and settings[key] not in (None, ""):
            return settings[key]
        for spec in self.metadata.inputs:
            if spec.key == key:
                return spec.default if spec.default is not None else fallback
        return fallback

    # -- setting it up --------------------------------------------------------
    def run_action(self, key, settings):
        """Answer one of :attr:`metadata`'s actions. Never raises.

        Always the same shape, so a front-end renders one thing::

            {"ok": bool, "summary": str, "detail": str, "argv": [str]}

        ``argv`` is filled in only by a ``command`` action, and is what the
        front-end runs. A plugin that declares no actions never sees this.
        """
        return {"ok": False, "summary": "Unknown action %r" % key,
                "detail": "", "argv": []}

    # -- being listened to ---------------------------------------------------
    def peek(self, settings):
        """What a step with these settings would take now, without running it.

        Only for a plugin whose metadata says ``watchable``. Returns a list of
        ``{"key", "title", ...}`` - ``key`` is what identifies one item from
        the next and is what an accepted item is run with; anything else is
        shown to the person being asked. Nothing may be written anywhere.
        Raises on failure, with a message a person can read.
        """
        raise NotImplementedError("%s cannot be watched" % self.metadata.id)

    # -- the lifecycle --------------------------------------------------------
    def prepare(self, context, step):
        """Anything needed before :meth:`execute`. Cheap and side-effect-light.

        Once this has been called, :meth:`cleanup` is guaranteed to be called
        too, whatever happens in between.
        """

    def execute(self, context, step):
        """Do the work; return a :class:`PluginResult`.

        Blocks. May take as long as it takes - the executor holds the deadline
        and will set ``context.cancel`` when it expires, so anything that waits
        should wait on that token or pass the remaining time down to whatever it
        is waiting on. Python cannot kill a thread, so a plugin that ignores its
        token cannot be stopped; every built-in one honours it.
        """
        raise NotImplementedError

    def cleanup(self, context, step):
        """Release whatever :meth:`prepare` or :meth:`execute` took.

        Called exactly once for every step that reached :meth:`prepare`, even
        when the run was cancelled or the step raised. Failures here are logged
        and never mask the real result.
        """


class ManagedPlugin(CyclePlugin):
    """A plugin that starts something and leaves it running for later steps.

    A service is the obvious case: ``service.start`` has not failed because the
    service is still up afterwards - that is the point of it. So
    :meth:`execute` starts the thing, hands the handle to the run to hold, and
    returns; the run stops everything it holds, in reverse order, when it ends.

    Subclasses implement :meth:`start`, :meth:`stop` and optionally
    :meth:`status`, and leave :meth:`execute` alone.
    """

    def execute(self, context, step):
        handle = self.start(context, step)
        if handle is not None:
            context.hold(step.id, self, handle)
        return succeeded(**(self.status(context, step, handle) or {}))

    def start(self, context, step):
        """Start it; return a handle, or None when there is nothing to hold."""
        raise NotImplementedError

    def stop(self, context, step, handle):
        """Stop what :meth:`start` started. Called by the run's cleanup pass."""
        raise NotImplementedError

    def status(self, context, step, handle):
        """Whatever a later step might want to read, as outputs."""
        return {}


# -- the table ----------------------------------------------------------------
def _builtins():
    """The built-in plugins, imported here rather than at module scope.

    Lazily, because a plugin may reach for something heavy - the scenario
    adapter pulls in the whole engine - and reading a cycle file, which is what
    ``--describe`` does on every start, must not pay for that.
    """
    from cycle.plugins import BUILTIN
    return BUILTIN


_registered = {}          # id -> plugin, for anything added by register()
_loaded = None            # id -> plugin, the built-ins, once asked for


def _table():
    global _loaded
    if _loaded is None:
        _loaded = {}
        for plugin in _builtins():
            _loaded[plugin.metadata.id] = plugin
    combined = dict(_loaded)
    combined.update(_registered)
    return combined


def get(plugin_id):
    """The plugin with this id, or None. Never raises - callers are checking."""
    return _table().get(plugin_id)


def all_plugins():
    """Every plugin, built-in and registered, sorted by id."""
    return [_table()[key] for key in sorted(_table())]


def describe():
    """Every plugin's metadata as plain data, for ``--describe``."""
    return [plugin.metadata.to_dict() for plugin in all_plugins()]


def register(plugin, replace=False):
    """Add a plugin that did not ship with the application.

    The seam an external plugin loader will use one day. Nothing calls it today
    and nothing scans for something to call it with - that stays a deliberate
    decision rather than a thing that happens because a file was in a directory.
    """
    metadata = getattr(plugin, "metadata", None)
    if metadata is None or not getattr(metadata, "id", ""):
        raise ValueError("a plugin needs metadata with an id")
    if not replace and metadata.id in _table():
        raise ValueError("a plugin called %r is already registered"
                         % metadata.id)
    _registered[metadata.id] = plugin
    return plugin


def unregister(plugin_id):
    """Remove something :func:`register` added. Built-ins cannot be removed."""
    return _registered.pop(plugin_id, None)
