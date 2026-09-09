"""The GUI half of a scenario's service step: what it does, and what it says back."""

import os
import sys
import time

import pytest
from PySide6.QtCore import QObject, Signal

from cms_gui import criteria as criteria_mod
from cms_gui.runnertypes import FAILED, RUNNING, STARTING, STOPPED, STOPPING
from cms_gui.servicebridge import ServiceBridge

# The core lives one directory up and is not installed; the GUI never imports it
# at runtime, but a test may - to check the two agree about the wire.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))


def _pump(qapp, predicate, timeout=15.0):
    """Turn the event loop until something becomes true, or give up."""
    deadline = time.time() + timeout
    while time.time() < deadline and not predicate():
        qapp.processEvents()
        time.sleep(0.02)
    qapp.processEvents()
    return predicate()


class FakeService:
    def __init__(self, status=STOPPED, detail=""):
        self.status = status
        self.detail = detail


class FakeSupervisor(QObject):
    """The three signals and the four readers the bridge uses, and nothing else."""

    status_changed = Signal(str, str, str)
    output = Signal(str, str, str)
    criteria_changed = Signal(str, str)

    def __init__(self, services=None):
        super().__init__()
        self._services = dict(services or {("Storefront", "Web"): FakeService()})
        self._criteria = {}
        self.calls = []

    # -- what the bridge reads
    def service(self, project, name):
        return self._services.get((project, name))

    def keys(self):
        return list(self._services)

    def criteria_state(self, project, name):
        return self._criteria.get((project, name), [])

    # -- what the bridge does
    def start(self, project, name):
        self.calls.append(("start", project, name))
        return True

    def stop(self, project, name):
        self.calls.append(("stop", project, name))
        return True

    def restart(self, project, name):
        self.calls.append(("restart", project, name))
        return True

    # -- test helpers
    def set_status(self, key, status):
        self._services[key].status = status
        self.status_changed.emit(key[0], key[1], status)

    def say(self, key, line):
        self.output.emit(key[0], key[1], line)

    def light(self, key, name, lit=True, outstanding=()):
        self._criteria[key] = [(name, "green", lit, list(outstanding))]
        self.criteria_changed.emit(key[0], key[1])


class Replies:
    """Collects the control commands the bridge writes back."""

    def __init__(self):
        self.sent = []

    def __call__(self, **command):
        self.sent.append(command)
        return True

    @property
    def last(self):
        return self.sent[-1]


@pytest.fixture
def bridge(qapp):
    supervisor = FakeSupervisor()
    replies = Replies()
    return ServiceBridge(supervisor, replies), supervisor, replies


def request(bridge, op, project="Storefront", service="Web", pattern=None,
            request_id=1, timeout_ms=0):
    bridge.handle({"kind": "service.request", "id": request_id, "op": op,
                   "project": project, "service": service, "pattern": pattern,
                   "timeout_ms": timeout_ms})


KEY = ("Storefront", "Web")


# -- the imperative ops ------------------------------------------------------

@pytest.mark.parametrize("op,call", [("service_start", "start"),
                                     ("service_stop", "stop"),
                                     ("service_restart", "restart")])
def test_an_imperative_op_is_done_and_answered_at_once(bridge, op, call):
    bridge, supervisor, replies = bridge
    request(bridge, op)
    assert supervisor.calls == [(call, "Storefront", "Web")]
    assert replies.last["command"] == "service.result"
    assert replies.last["ok"] is True


def test_a_configuration_that_cannot_run_is_answered_as_a_failure(bridge):
    bridge, supervisor, replies = bridge

    def fail(project, name):
        supervisor._services[(project, name)].status = FAILED
        supervisor._services[(project, name)].detail = "Nothing to run"

    supervisor.start = fail
    request(bridge, "service_start")
    assert replies.last["ok"] is False
    assert "Nothing to run" in replies.last["message"]


# -- resolving the reference -------------------------------------------------

def test_an_unknown_service_is_answered_at_once_rather_than_waited_out(bridge):
    bridge, supervisor, replies = bridge
    request(bridge, "wait_for_service", project="Storefront", service="Ghost")
    assert replies.last["ok"] is False
    assert "services.json" in replies.last["message"]


def test_a_bare_name_resolves_when_only_one_project_has_it(bridge):
    bridge, supervisor, replies = bridge
    request(bridge, "service_start", project="", service="Web")
    assert supervisor.calls == [("start", "Storefront", "Web")]


def test_a_bare_name_in_two_projects_is_refused_with_both_named(bridge):
    bridge, supervisor, replies = bridge
    supervisor._services[("Other", "Web")] = FakeService()
    request(bridge, "service_start", project="", service="Web")
    assert replies.last["ok"] is False
    assert "Project/Web" in replies.last["message"]


# -- wait_for_service --------------------------------------------------------

def test_wait_for_service_answers_when_it_comes_up(bridge):
    bridge, supervisor, replies = bridge
    request(bridge, "wait_for_service")
    assert not replies.sent                      # still waiting
    supervisor.set_status(KEY, STARTING)
    assert not replies.sent
    supervisor.set_status(KEY, RUNNING)
    assert replies.last["ok"] is True
    assert replies.last["status"] == RUNNING


def test_wait_for_service_answers_at_once_when_it_is_already_up(bridge):
    bridge, supervisor, replies = bridge
    supervisor._services[KEY].status = RUNNING
    request(bridge, "wait_for_service")
    assert replies.last["ok"] is True
    assert "already running" in replies.last["message"]


def test_a_service_that_fails_ends_the_wait(bridge):
    bridge, supervisor, replies = bridge
    request(bridge, "wait_for_service")
    supervisor.set_status(KEY, FAILED)
    assert replies.last["ok"] is False
    assert "it failed" in replies.last["message"]


# -- wait_for_out ------------------------------------------------------------

def test_wait_for_out_answers_when_a_line_matches_the_regex(bridge):
    bridge, supervisor, replies = bridge
    supervisor.set_status(KEY, STARTING)
    request(bridge, "wait_for_out", pattern=r".+:8069")
    supervisor.say(KEY, "loading module base")
    assert not replies.sent
    supervisor.say(KEY, "HTTP service running on 0.0.0.0:8069")
    assert replies.last["ok"] is True


def test_wait_for_out_sees_what_was_printed_since_it_started(bridge):
    # The line arrives between the restart and the wait: still this run's, so
    # the wait is satisfied rather than hanging on a line it just missed.
    bridge, supervisor, replies = bridge
    supervisor.set_status(KEY, STARTING)
    supervisor.say(KEY, "HTTP service running on 0.0.0.0:8069")
    request(bridge, "wait_for_out", pattern=r".+:8069")
    assert replies.last["ok"] is True
    assert "already printed" in replies.last["message"]


def test_wait_for_out_ignores_what_the_previous_run_printed(bridge):
    # The whole reason the bridge keeps its own buffer: the console's does not
    # clear, so this line would otherwise pass a wait for a boot that has not
    # happened yet.
    bridge, supervisor, replies = bridge
    supervisor.set_status(KEY, STARTING)
    supervisor.say(KEY, "HTTP service running on 0.0.0.0:8069")
    supervisor.set_status(KEY, STOPPED)
    supervisor.set_status(KEY, STARTING)            # a new run
    request(bridge, "wait_for_out", pattern=r".+:8069")
    assert not replies.sent

    supervisor.say(KEY, "HTTP service running on 0.0.0.0:8069")
    assert replies.last["ok"] is True


def test_a_restart_then_a_wait_does_not_pass_on_the_previous_boots_line(bridge):
    """The sequence a scenario actually writes, at the timing it actually has.

    ``service_restart`` is answered as soon as the supervisor takes the job, so
    the ``wait_for_out`` behind it arrives while the old process is still going
    down - before STARTING. The ready line still in the buffer at that moment is
    the *previous* boot's, and taking it would declare the restart finished
    before it had begun.
    """
    bridge, supervisor, replies = bridge
    supervisor.set_status(KEY, STARTING)
    supervisor.set_status(KEY, RUNNING)
    supervisor.say(KEY, "HTTP service running on 0.0.0.0:8069")

    request(bridge, "service_restart", request_id=1)
    supervisor.set_status(KEY, STOPPING)          # not down yet
    request(bridge, "wait_for_out", pattern=r".+:8069", request_id=2)
    assert [reply["id"] for reply in replies.sent] == [1]

    # And it is still not satisfied by anything the old run said.
    supervisor.set_status(KEY, STOPPED)
    supervisor.set_status(KEY, STARTING)
    assert [reply["id"] for reply in replies.sent] == [1]

    supervisor.say(KEY, "HTTP service running on 0.0.0.0:8069")
    assert replies.last["id"] == 2 and replies.last["ok"] is True


def test_a_restart_under_a_waiting_step_resets_what_it_has_seen(bridge):
    # The same rule from the other side: a matcher seeded before the restart
    # must un-see those lines, because its match rules are sticky.
    bridge, supervisor, replies = bridge
    supervisor.set_status(KEY, STARTING)
    supervisor.say(KEY, "almost ready")
    request(bridge, "wait_for_out", pattern="ready.+now")
    supervisor.say(KEY, "ready")                  # half of it, from this run
    supervisor.set_status(KEY, STOPPING)          # ...and then a restart
    supervisor.set_status(KEY, STARTING)
    supervisor.say(KEY, "now")                    # the other half, from the next
    assert not replies.sent                       # neither run said the whole thing

    supervisor.say(KEY, "ready and serving now")
    assert replies.last["ok"] is True


def test_wait_for_out_is_a_regex_not_a_substring(bridge):
    bridge, supervisor, replies = bridge
    supervisor.set_status(KEY, STARTING)
    request(bridge, "wait_for_out", pattern=r"port \d+ ready")
    supervisor.say(KEY, "port  ready")
    assert not replies.sent
    supervisor.say(KEY, "port 8069 ready")
    assert replies.last["ok"] is True


# -- wait_for_criterion ------------------------------------------------------

def test_wait_for_criterion_answers_when_it_lights(bridge):
    bridge, supervisor, replies = bridge
    supervisor.light(KEY, "start", lit=False, outstanding=["waiting for 'ready'"])
    request(bridge, "wait_for_criterion", pattern="start")
    assert not replies.sent
    supervisor.light(KEY, "start", lit=True)
    assert replies.last["ok"] is True


def test_wait_for_criterion_answers_at_once_when_it_is_already_lit(bridge):
    bridge, supervisor, replies = bridge
    supervisor.light(KEY, "start", lit=True)
    request(bridge, "wait_for_criterion", pattern="start")
    assert replies.last["ok"] is True
    assert "already lit" in replies.last["message"]


def test_an_unknown_criterion_is_refused_rather_than_waited_out(bridge):
    bridge, supervisor, replies = bridge
    supervisor.light(KEY, "start", lit=False)
    request(bridge, "wait_for_criterion", pattern="typo")
    assert replies.last["ok"] is False
    assert "no criterion named" in replies.last["message"]


def test_a_restart_then_a_criterion_wait_ignores_the_previous_runs_tag(bridge):
    """The criterion half of the rule wait_for_out already keeps.

    The criteria are cleared by the service's own start(), which for one that is
    already up happens only after the old process is down - so at the moment the
    step behind ``service_restart`` arrives they are still lit from the boot
    being killed. Answering from them there passes the wait in microseconds and
    sends the scenario on into a backend that is still going down.
    """
    bridge, supervisor, replies = bridge
    supervisor.set_status(KEY, RUNNING)
    supervisor.light(KEY, "Main started", lit=True)

    request(bridge, "service_restart", request_id=1)
    supervisor.set_status(KEY, STOPPING)              # not down yet
    request(bridge, "wait_for_criterion", pattern="Main started", request_id=2)
    assert [reply["id"] for reply in replies.sent] == [1]

    # Nor by anything the old run lights on its way out.
    supervisor.light(KEY, "Main started", lit=True)
    assert [reply["id"] for reply in replies.sent] == [1]

    supervisor.set_status(KEY, STOPPED)
    supervisor.set_status(KEY, STARTING)              # cleared for the new run
    supervisor.light(KEY, "Main started", lit=False, outstanding=["waiting"])
    assert [reply["id"] for reply in replies.sent] == [1]

    supervisor.light(KEY, "Main started", lit=True)
    assert replies.last["id"] == 2 and replies.last["ok"] is True


def test_a_start_that_changed_nothing_leaves_the_tag_worth_reading(bridge):
    # The supervisor answers False for a service that is already up: no new run
    # is coming, so what the criterion says is this run's and answers at once.
    # Holding it back here would turn every such step into a 120 s timeout.
    bridge, supervisor, replies = bridge
    supervisor.set_status(KEY, RUNNING)
    supervisor.light(KEY, "Main started", lit=True)
    supervisor.start = lambda project, name: False

    request(bridge, "service_start", request_id=1)
    request(bridge, "wait_for_criterion", pattern="Main started", request_id=2)
    assert replies.last["id"] == 2 and replies.last["ok"] is True
    assert "already lit" in replies.last["message"]


def test_a_restart_then_a_service_wait_waits_for_the_run_it_asked_for(bridge):
    # The same hole in wait_for_service: a stop the supervisor has taken but not
    # carried out yet leaves the status RUNNING, and that is the old process.
    bridge, supervisor, replies = bridge
    supervisor.set_status(KEY, RUNNING)

    request(bridge, "service_restart", request_id=1)
    request(bridge, "wait_for_service", request_id=2)
    assert [reply["id"] for reply in replies.sent] == [1]

    supervisor.set_status(KEY, STOPPING)
    supervisor.set_status(KEY, STARTING)
    supervisor.set_status(KEY, RUNNING)
    assert replies.last["id"] == 2 and replies.last["ok"] is True


def test_a_restart_that_failed_ends_the_wait_behind_it_at_once(bridge):
    # The run the wait is for is not coming. Neither answer from the old run's
    # tag nor two minutes of waiting for a start that has already not happened:
    # the failure itself is the answer, and it arrives immediately.
    bridge, supervisor, replies = bridge
    supervisor.set_status(KEY, RUNNING)
    supervisor.light(KEY, "Main started", lit=True)

    request(bridge, "service_restart", request_id=1)
    supervisor.set_status(KEY, STOPPING)
    supervisor.set_status(KEY, FAILED)
    request(bridge, "wait_for_criterion", pattern="Main started", request_id=2)
    assert replies.last["id"] == 2 and replies.last["ok"] is False
    assert "it failed" in replies.last["message"]


def test_a_criterion_wait_that_fails_says_what_was_outstanding(bridge):
    bridge, supervisor, replies = bridge
    supervisor.light(KEY, "start", lit=False, outstanding=["waiting for 'ready'"])
    request(bridge, "wait_for_criterion", pattern="start")
    supervisor.set_status(KEY, FAILED)
    assert replies.last["ok"] is False
    assert "waiting for 'ready'" in replies.last["message"]


# -- one reply, always -------------------------------------------------------

def test_a_wait_is_answered_exactly_once(bridge):
    bridge, supervisor, replies = bridge
    request(bridge, "wait_for_service")
    supervisor.set_status(KEY, RUNNING)
    supervisor.set_status(KEY, RUNNING)
    supervisor.set_status(KEY, FAILED)
    assert len(replies.sent) == 1


def test_two_waits_on_one_service_are_both_answered(bridge):
    bridge, supervisor, replies = bridge
    supervisor.set_status(KEY, STARTING)
    request(bridge, "wait_for_out", pattern="ready", request_id=1)
    request(bridge, "wait_for_service", request_id=2)
    supervisor.say(KEY, "ready")
    supervisor.set_status(KEY, RUNNING)
    assert sorted(reply["id"] for reply in replies.sent) == [1, 2]


def test_the_deadline_answers_with_the_last_line_the_service_said(bridge, qapp):
    bridge, supervisor, replies = bridge
    supervisor.set_status(KEY, STARTING)
    request(bridge, "wait_for_out", pattern="never", timeout_ms=10)
    supervisor.say(KEY, "still loading module base")
    assert not replies.sent

    assert _pump(qapp, lambda: bool(replies.sent))
    assert replies.last["ok"] is False
    assert "timed out" in replies.last["message"]
    assert "still loading module base" in replies.last["message"]


def test_cancel_all_drops_everything_pending(bridge):
    bridge, supervisor, replies = bridge
    request(bridge, "wait_for_service", timeout_ms=60000)
    bridge.cancel_all()
    # The launcher has gone: nothing is answered, and nothing is left to fire.
    supervisor.set_status(KEY, RUNNING)
    assert not replies.sent


def test_an_event_that_is_not_a_service_request_is_left_alone(bridge):
    bridge, supervisor, replies = bridge
    assert bridge.handle({"kind": "step.end"}) is False
    assert not replies.sent


# -- the two ends of the wire ------------------------------------------------

def test_what_the_engine_asks_is_what_the_bridge_answers(qapp):
    """The whole round trip, with the real engine module on the other end.

    Each side's own tests use a stand-in for the other, so neither would notice
    the two disagreeing about a field name. This is the one that would.
    """
    import json
    import threading

    from engine import events, services

    class Sink:
        def __init__(self):
            self.lines = []

        def write(self, text):
            self.lines.append(text)

        def flush(self):
            pass

    supervisor = FakeSupervisor()
    supervisor.set_status(KEY, STARTING)
    replies = []

    def send(**command):
        # The GUI writes this line to the launcher's stdin; read_commands turns
        # it back into exactly this call.
        replies.append(command)
        services.deliver(command["id"], ok=command["ok"],
                         status=command["status"], message=command["message"])
        return True

    bridge = ServiceBridge(supervisor, send)
    sink = Sink()
    events._stream = sink
    services.reset()
    services.configure(True)
    try:
        answer = {}

        def ask():
            answer["r"] = services.request(services.WAIT_OUT, "Storefront/Web",
                                           pattern=r".+:8069", timeout_ms=5000,
                                           session="admin")

        thread = threading.Thread(target=ask)
        thread.start()
        assert _pump(qapp, lambda: bool(sink.lines), timeout=5.0)

        # Straight off the stream, unedited - the GUI's own parser does no more.
        bridge.handle(json.loads(sink.lines[-1]))
        assert not replies                    # nothing matches yet
        supervisor.say(KEY, "HTTP service running on 0.0.0.0:8069")
        thread.join(5)

        assert replies and replies[0]["command"] == "service.result"
        assert answer["r"][0] is True
    finally:
        services.reset()
        events._stream = None


# -- the matcher it is built on ---------------------------------------------

def test_it_matches_the_way_a_regex_criterion_does():
    # Not a second matching engine: the same Rule/Matcher the Services page uses.
    rule = criteria_mod.Rule(mode=criteria_mod.MATCH, kind=criteria_mod.REGEX,
                             pattern=r"(?i)READY")
    matcher = criteria_mod.Matcher(
        criteria_mod.CriterionRow(name="x", rules=[rule]))
    matcher.feed("ready to serve")
    assert matcher.lit()
