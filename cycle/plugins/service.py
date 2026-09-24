"""``service.start`` / ``service.stop`` / ``service.wait`` - the GUI's services.

Thin on purpose. The services on the Services & Logs page are owned by the GUI
process - an attached one is literally its child - so this engine cannot start
one itself without leaving the GUI watching a second copy it has never heard of.
``engine/services.py`` already solves that: a request goes out on the event
stream, the GUI acts, the answer comes back on stdin. These plugins are three
calls into it.

That is the whole of §25 of the design document - "do not rewrite the existing
service system; create adapters" - and it is worth noticing how little code it
turned out to be. Nothing new was needed at either end of the pipe.

**Without a GUI they fail immediately rather than waiting.** ``request`` checks
whether both halves of the pipe are live and says "service steps need the GUI"
in the first millisecond, so a cycle run from a terminal reports the truth
straight away instead of sitting out a two-minute timeout per step. That is what
makes a cycle with service steps safe to run headless: it stops, and says why.

``service.start`` is a :class:`~cycle.registry.ManagedPlugin`, so the run holds
what it started and stops it at the end - in reverse order, which is why a
service started after the database it needs is stopped before it. A step that
should leave something running for later - the ordinary case in a validation
cycle, where the report step wants the app still up - says ``keep: true``.
"""

from cycle import registry
from cycle.registry import CyclePlugin, ManagedPlugin, PluginMetadata, field, output


def _services():
    """``engine.services``, imported lazily.

    A cycle with no service steps must not pay for the engine package, and
    ``--describe`` reads this module's metadata on every start.
    """
    from engine import services
    return services


def _ref(plugin, step):
    """The "Project/Service" this step names."""
    return str(plugin.setting(step.settings, "service") or "").strip()


def _timeout_ms(plugin, step, fallback):
    """The step's own timeout in milliseconds, or the sensible default.

    A step's ``timeout:`` is the executor's deadline and is in seconds; this is
    the request's own, in milliseconds, because that is what the wire speaks.
    When a step sets the executor's deadline and not this one, the request is
    given the same number so it gives up at the same moment rather than being
    cut off mid-wait by a token it was not watching.
    """
    own = plugin.setting(step.settings, "timeout")
    if own not in (None, ""):
        return int(float(own) * 1000)
    if step.timeout:
        return int(float(step.timeout) * 1000)
    return fallback


_SERVICE_FIELD = field(
    "service", "Service", "service", required=True,
    hint='Which service, as "Project/Service" - the names on the Services & '
         "Logs page. A bare name works when only one project has one.")


class ServiceStart(ManagedPlugin):
    """Ask the GUI to start a service, and hold it for the rest of the run."""

    metadata = PluginMetadata(
        id="service.start",
        asks_when_overdue=False,
        name="Start Service",
        category=registry.SERVICE,
        summary="Start one of the services the application manages.",
        permissions=("service.control",),
        background=True,
        inputs=(
            _SERVICE_FIELD,
            field("wait", "Wait until it is running", "check", default=True,
                  hint="Off returns as soon as the application has taken the "
                       "job, which is not the same as the service being up."),
            field("timeout", "Timeout", "number",
                  hint="Seconds to wait. Blank uses two minutes when waiting, "
                       "fifteen seconds when not."),
            field("keep", "Leave it running", "check", default=False,
                  hint="On leaves the service up after the run. Off stops it "
                       "again at the end, which is what makes a cycle repeatable."),
        ),
        outputs=(
            output("service", "string", "The service this step acted on."),
            output("running", "boolean", "Whether it was up when this finished."),
        ),
    )

    def problems(self, settings):
        found = super(ServiceStart, self).problems(settings)
        ref = str((settings or {}).get("service") or "").strip()
        if ref and ref.endswith("/"):
            found.append("Service: %r names a project but no service." % ref)
        return found

    def execute(self, context, step):
        services = _services()
        ref = _ref(self, step)

        ok, message = services.request(
            services.START, ref,
            timeout_ms=_timeout_ms(self, step, services.DEFAULT_ACK_MS))
        if not ok:
            return registry.failed(message, service=ref, running=False)

        if self.setting(step.settings, "wait", True):
            ok, message = services.request(
                services.WAIT_RUNNING, ref,
                timeout_ms=_timeout_ms(self, step, services.DEFAULT_WAIT_MS))
            if not ok:
                return registry.failed(message, service=ref, running=False)

        if not self.setting(step.settings, "keep", False):
            # Held, so the run stops it whatever happens next - a failed step,
            # a cancelled run, an exception in a plugin three steps later.
            context.hold(step.id, self, ref)
        return registry.succeeded(message or "running", service=ref, running=True)

    def stop(self, context, step, handle):
        """Called by the run's cleanup pass for anything still held."""
        services = _services()
        ok, message = services.request(services.STOP, handle,
                                       timeout_ms=services.DEFAULT_ACK_MS)
        if not ok:
            # Logged by the executor. Raising would be no more useful: the run
            # is already over and there is nothing left to abandon.
            raise RuntimeError(message)


class ServiceStop(CyclePlugin):
    """Ask the GUI to stop a service."""

    metadata = PluginMetadata(
        id="service.stop",
        asks_when_overdue=False,
        name="Stop Service",
        category=registry.SERVICE,
        summary="Stop one of the services the application manages.",
        permissions=("service.control",),
        inputs=(
            _SERVICE_FIELD,
            field("timeout", "Timeout", "number",
                  hint="Seconds to wait for the application to take the job. "
                       "Blank uses fifteen."),
        ),
        outputs=(output("service", "string", "The service this step acted on."),),
    )

    def execute(self, context, step):
        services = _services()
        ref = _ref(self, step)
        ok, message = services.request(
            services.STOP, ref,
            timeout_ms=_timeout_ms(self, step, services.DEFAULT_ACK_MS))
        # Whatever this step was asked to stop is no longer the run's to stop
        # at the end - and a second stop would report a failure for something
        # that already went as asked.
        _release(context, ref)
        if not ok:
            return registry.failed(message, service=ref)
        return registry.succeeded(message or "stopped", service=ref)


class ServiceRestart(CyclePlugin):
    """Ask the GUI to restart a service."""

    metadata = PluginMetadata(
        id="service.restart",
        asks_when_overdue=False,
        name="Restart Service",
        category=registry.SERVICE,
        summary="Restart one of the services the application manages.",
        permissions=("service.control",),
        inputs=(
            _SERVICE_FIELD,
            field("wait", "Wait until it is running", "check", default=True),
            field("timeout", "Timeout", "number"),
        ),
        outputs=(output("service", "string", "The service this step acted on."),
                 output("running", "boolean", "Whether it was up when this "
                                              "finished.")),
    )

    def execute(self, context, step):
        services = _services()
        ref = _ref(self, step)
        ok, message = services.request(
            services.RESTART, ref,
            timeout_ms=_timeout_ms(self, step, services.DEFAULT_ACK_MS))
        if not ok:
            return registry.failed(message, service=ref, running=False)
        if self.setting(step.settings, "wait", True):
            ok, message = services.request(
                services.WAIT_RUNNING, ref,
                timeout_ms=_timeout_ms(self, step, services.DEFAULT_WAIT_MS))
            if not ok:
                return registry.failed(message, service=ref, running=False)
        return registry.succeeded(message or "running", service=ref, running=True)


class ServiceWait(CyclePlugin):
    """Wait for a service to be up, to say something, or to reach a criterion.

    One step rather than three, because they are one question asked three ways
    and a cycle reads better for it: what is written decides which. A ``match``
    is a regular expression over the service's output; a ``criterion`` is one of
    the named states configured for it on the Services & Logs page.
    """

    metadata = PluginMetadata(
        id="service.wait",
        asks_when_overdue=False,
        name="Wait For Service",
        category=registry.SERVICE,
        summary="Wait until a service is running, says something, or reaches a "
                "state.",
        permissions=("service.control",),
        inputs=(
            _SERVICE_FIELD,
            field("match", "Output matches", "text",
                  hint="A regular expression over what the service prints, e.g. "
                       '"HTTP service .+ running on". Waits for the line rather '
                       "than for the process, which is what "
                       '"ready" usually means.'),
            field("criterion", "Criterion", "text",
                  hint="One of the named states configured for this service. "
                       "Use this or Output matches, not both."),
            field("timeout", "Timeout", "number",
                  hint="Seconds to wait. Blank uses two minutes."),
        ),
        outputs=(output("service", "string", "The service this step waited on."),
                 output("matched", "string", "What the wait came back with.")),
    )

    def problems(self, settings):
        found = super(ServiceWait, self).problems(settings)
        settings = settings or {}
        if settings.get("match") and settings.get("criterion"):
            found.append("Give either Output matches or Criterion, not both - "
                         "they are two different things to wait for.")
        return found

    def execute(self, context, step):
        services = _services()
        ref = _ref(self, step)
        match = str(self.setting(step.settings, "match") or "").strip()
        criterion = str(self.setting(step.settings, "criterion") or "").strip()

        if match:
            op, pattern = services.WAIT_OUT, match
        elif criterion:
            op, pattern = services.WAIT_CRITERION, criterion
        else:
            op, pattern = services.WAIT_RUNNING, None

        ok, message = services.request(
            op, ref, pattern=pattern,
            timeout_ms=_timeout_ms(self, step, services.DEFAULT_WAIT_MS))
        if not ok:
            return registry.failed(message, service=ref, matched="")
        return registry.succeeded(message or "ready", service=ref,
                                  matched=message or "")


def _release(context, ref):
    """Forget any held start for this service, so cleanup does not repeat it."""
    for step_id, _plugin, handle in context.held():
        if handle == ref:
            context.release(step_id)
