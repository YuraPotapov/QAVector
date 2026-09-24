"""The service adapters: three calls into the bridge that already exists.

The bridge itself - the request/reply over the launcher's pipe - is covered by
tests/test_services.py. These check that the right op goes out with the right
arguments, that what comes back is reported honestly, and that the run stops
what it started.
"""

import pytest

from cycle import registry, workspace
from cycle.context import RunContext
from cycle.plugins.service import (ServiceRestart, ServiceStart, ServiceStop,
                                   ServiceWait)
from domain.cycle import CycleRun, CycleStep
from engine import services as engine_services


@pytest.fixture
def context(tmp_path):
    path = workspace.create("20260917-000000-demo", str(tmp_path))
    return RunContext(CycleRun(id="r", cycle_id="demo", workspace=path),
                      None, path)


class _Bridge(list):
    """Stands in for the GUI: records every request and answers it.

    A list of the calls, so a test reads ``asked[0]["op"]``, with the answer
    alongside. ``replies`` is for the cases where the two requests a step makes
    have to come back differently - a service that starts and then never comes
    up.
    """

    def __init__(self):
        list.__init__(self)
        self.ok = True
        self.message = "running"
        self.replies = None            # [(ok, message), ...] when order matters

    def request(self, op, ref, pattern=None, timeout_ms=None, session=None):
        self.append({"op": op, "ref": ref, "pattern": pattern,
                     "timeout_ms": timeout_ms})
        if self.replies:
            return self.replies.pop(0)
        return self.ok, self.message


@pytest.fixture
def asked(monkeypatch):
    bridge = _Bridge()
    monkeypatch.setattr(engine_services, "request", bridge.request)
    return bridge


def step(plugin_id, **settings):
    return CycleStep(id="svc", plugin=plugin_id, settings=settings)


# ---------------------------------------------------------------------- start
def test_starting_asks_the_application_to_start_it_then_waits(context, asked):
    result = ServiceStart().execute(context, step("service.start",
                                                  service="Shop/Postgres"))

    assert result.ok
    assert [call["op"] for call in asked] == [engine_services.START,
                                              engine_services.WAIT_RUNNING]
    assert all(call["ref"] == "Shop/Postgres" for call in asked)
    assert result.outputs == {"service": "Shop/Postgres", "running": True}


def test_starting_without_waiting_only_asks_once(context, asked):
    """Taking the job is not the same as the service being up."""
    ServiceStart().execute(context, step("service.start", service="A/B",
                                         wait=False))
    assert [call["op"] for call in asked] == [engine_services.START]


def test_a_service_that_will_not_start_fails_and_says_what_it_was_told(context,
                                                                       asked):
    asked.ok, asked.message = False, "no service named 'A/B'"
    result = ServiceStart().execute(context, step("service.start", service="A/B"))

    assert not result.ok
    assert result.message == "no service named 'A/B'"
    assert result.outputs["running"] is False


def test_a_service_that_starts_but_never_comes_up_fails(context, asked):
    """Taking the job and being up are two answers, and only the second counts."""
    asked.replies = [(True, "starting"), (False, "timed out waiting")]
    result = ServiceStart().execute(context, step("service.start", service="A/B"))

    assert not result.ok
    assert "timed out" in result.message
    assert [call["op"] for call in asked] == [engine_services.START,
                                              engine_services.WAIT_RUNNING]


def test_what_a_step_started_is_held_so_the_run_stops_it(context, asked):
    """A cycle that leaves a database running is a cycle that cannot be repeated."""
    ServiceStart().execute(context, step("service.start", service="Shop/Postgres"))
    assert [handle for _id, _plugin, handle in context.held()] == [
        "Shop/Postgres"]


def test_a_step_can_ask_to_leave_the_service_running(context, asked):
    ServiceStart().execute(context, step("service.start", service="A/B",
                                         keep=True))
    assert context.held() == []


def test_stopping_what_was_held_asks_the_application_to_stop_it(context, asked):
    plugin = ServiceStart()
    plugin.execute(context, step("service.start", service="A/B"))
    del asked[:]

    plugin.stop(context, step("service.start", service="A/B"), "A/B")
    assert [call["op"] for call in asked] == [engine_services.STOP]


def test_a_stop_that_fails_is_raised_so_the_run_logs_it(context, asked):
    asked.ok, asked.message = False, "it would not stop"
    with pytest.raises(RuntimeError):
        ServiceStart().stop(context, step("service.start", service="A/B"), "A/B")


# ----------------------------------------------------------------------- stop
def test_stopping_asks_once_and_reports_what_came_back(context, asked):
    asked.message = "stopped"
    result = ServiceStop().execute(context, step("service.stop", service="A/B"))

    assert result.ok
    assert [call["op"] for call in asked] == [engine_services.STOP]
    assert result.message == "stopped"


def test_stopping_gives_back_what_an_earlier_step_was_holding(context, asked):
    """Otherwise the cleanup pass stops it again and reports a failure for
    something that already went exactly as asked."""
    ServiceStart().execute(context, step("service.start", service="A/B"))
    assert context.held()

    ServiceStop().execute(context, step("service.stop", service="A/B"))
    assert context.held() == []


def test_stopping_a_service_nobody_held_is_fine(context, asked):
    result = ServiceStop().execute(context, step("service.stop", service="C/D"))
    assert result.ok


# -------------------------------------------------------------------- restart
def test_restarting_asks_to_restart_then_waits(context, asked):
    result = ServiceRestart().execute(context, step("service.restart",
                                                    service="A/B"))
    assert result.ok
    assert [call["op"] for call in asked] == [engine_services.RESTART,
                                              engine_services.WAIT_RUNNING]


# ----------------------------------------------------------------------- wait
def test_waiting_with_nothing_said_waits_for_it_to_be_running(context, asked):
    ServiceWait().execute(context, step("service.wait", service="A/B"))
    assert asked[0]["op"] == engine_services.WAIT_RUNNING
    assert asked[0]["pattern"] is None


def test_waiting_for_a_line_waits_on_the_output(context, asked):
    ServiceWait().execute(context, step("service.wait", service="A/B",
                                        match="HTTP service .+ running"))
    assert asked[0]["op"] == engine_services.WAIT_OUT
    assert asked[0]["pattern"] == "HTTP service .+ running"


def test_waiting_for_a_named_state_waits_on_the_criterion(context, asked):
    ServiceWait().execute(context, step("service.wait", service="A/B",
                                        criterion="Updated/Started"))
    assert asked[0]["op"] == engine_services.WAIT_CRITERION
    assert asked[0]["pattern"] == "Updated/Started"


def test_asking_for_two_different_things_to_wait_for_is_complained_about():
    found = ServiceWait().problems({"service": "A/B", "match": "x",
                                    "criterion": "y"})
    assert found and "not both" in found[0]


def test_a_wait_that_times_out_fails_with_what_it_was_told(context, asked):
    asked.ok, asked.message = False, "timed out after 120s"
    result = ServiceWait().execute(context, step("service.wait", service="A/B"))
    assert not result.ok
    assert "timed out" in result.message


# -------------------------------------------------------------------- timeouts
def test_a_step_s_own_timeout_is_passed_down_in_milliseconds(context, asked):
    ServiceWait().execute(context, step("service.wait", service="A/B",
                                        timeout=30))
    assert asked[0]["timeout_ms"] == 30000


def test_the_executor_s_deadline_is_used_when_the_step_sets_no_timeout_of_its_own(
        context, asked):
    """Otherwise the request waits past the moment the step is cut off, and the
    step reports a timeout without the service ever being asked to hurry."""
    one = CycleStep(id="svc", plugin="service.wait",
                    settings={"service": "A/B"}, timeout=45)
    ServiceWait().execute(context, one)
    assert asked[0]["timeout_ms"] == 45000


def test_with_no_timeout_anywhere_the_sensible_default_is_used(context, asked):
    ServiceWait().execute(context, step("service.wait", service="A/B"))
    assert asked[0]["timeout_ms"] == engine_services.DEFAULT_WAIT_MS

    del asked[:]
    ServiceStop().execute(context, step("service.stop", service="A/B"))
    assert asked[0]["timeout_ms"] == engine_services.DEFAULT_ACK_MS


# ------------------------------------------------------------------- headless
def test_without_a_gui_a_service_step_fails_at_once_rather_than_waiting(context):
    """What makes a cycle with service steps safe to run from a terminal: it
    stops in the first millisecond and says why, instead of sitting out two
    minutes per step for an answer that is not coming."""
    import time

    engine_services.configure(False)
    for plugin in (ServiceStart(), ServiceStop(), ServiceWait(),
                   ServiceRestart()):
        started = time.monotonic()
        result = plugin.execute(context, step(plugin.metadata.id,
                                              service="A/B"))
        assert not result.ok
        assert time.monotonic() - started < 1.0
        assert "need the GUI" in result.message


# -------------------------------------------------------------------- the table
def test_every_service_plugin_is_in_the_table_and_declares_what_it_touches():
    for plugin_id in ("service.start", "service.stop", "service.restart",
                      "service.wait"):
        plugin = registry.get(plugin_id)
        assert plugin is not None
        assert plugin.metadata.category == registry.SERVICE
        assert "service.control" in plugin.metadata.permissions


def test_only_starting_is_a_long_running_step():
    """It is the one that leaves something behind for the run to stop."""
    assert registry.get("service.start").metadata.background is True
    assert registry.get("service.stop").metadata.background is False


def test_a_service_is_required_of_every_one_of_them():
    for plugin_id in ("service.start", "service.stop", "service.restart",
                      "service.wait"):
        assert registry.get(plugin_id).problems({}) == ["Service is required."]


def test_a_reference_with_no_service_in_it_is_complained_about():
    found = ServiceStart().problems({"service": "Shop/"})
    assert found and "no service" in found[0]
