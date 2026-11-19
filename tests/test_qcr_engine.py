"""The Execution Engine (ADR-002): checked, written, then started; one honest outcome."""

import threading

import pytest

from qcr import (Authority, CapabilityDescriptor, Grant, QCRError, Registration, Registry,
                 ResourceDescriptor, ResourceRef)
from qcr.engine import Engine
from qcr.executions import ExecutionStore

WEB = ResourceRef("p", "web", "service")
DB = ResourceRef("p", "db", "service")
START = "services.web.start"
INPUT = {"type": "object", "properties": {"wait_ready": {"type": "boolean"}}, "additionalProperties": False}
OUTPUT = {"type": "object", "properties": {"service_status": {"type": "string"}},
          "required": ["service_status"], "additionalProperties": False}


class Invoker:
    """A stand-in owner: records what it was asked and reports when the test says."""

    def __init__(self, needs=(), on_start=None):
        self.needs, self.on_start = list(needs), on_start
        self.started, self.cancelled = [], []

    def requirements(self, capability, request):
        if isinstance(self.needs, Exception):
            raise self.needs
        return self.needs

    def start(self, job):
        self.started.append(job)
        if self.on_start:
            self.on_start(job)

    def cancel(self, job):
        self.cancelled.append(job.execution_id)


class Timers:
    """Deadlines the test fires by hand."""

    def __init__(self):
        self.made = []

    def __call__(self, seconds, work):
        timer = type("T", (), {"cancelled": False, "seconds": seconds, "work": staticmethod(work)})()
        timer.cancel = lambda: setattr(timer, "cancelled", True)
        self.made.append(timer)
        return timer

    def fire_last(self):
        self.made[-1].work()


def capability(name="start", **kwargs):
    args = dict(risk_level="medium", mode="long_running", timeout_seconds=120,
                supports_cancel=True, supports_idempotency=True)
    args.update(kwargs)
    return CapabilityDescriptor("services.web.%s" % name, WEB, 1, "Start web", INPUT, OUTPUT,
                                "services.%s" % name, **args)


@pytest.fixture
def world(tmp_path):
    authority = Authority()
    registry = Registry(authority)
    store = ExecutionStore(str(tmp_path / "e.sqlite"))
    timers = Timers()
    invoker = Invoker(on_start=lambda job: job.dispatched())

    def register(invoker, caps=None):
        registry.register(Registration(
            ResourceDescriptor(WEB, "services", "Web", "services.read"),
            tuple(caps or (capability(), capability("probe", supports_cancel=False, supports_idempotency=False))),
            lambda ref, names: {}, invoker=invoker))
    register(invoker)
    engine = Engine(registry, store, schedule=timers)
    user = authority.issue("gui-user", [Grant("p", "services.read"), Grant("p", "services.start"),
                                        Grant("p", "services.probe")])
    world = type("World", (), dict(authority=authority, registry=registry, store=store, engine=engine,
                                   timers=timers, invoker=invoker, user=user, register=staticmethod(register)))
    yield world
    engine.close()
    store.close()


def invoke(world, caller=None, request=None, **kwargs):
    return world.engine.invoke(caller or world.user, "p", START,
                               {"wait_ready": True} if request is None else request, **kwargs)


def kinds(world, execution_id):
    return [(e["type"], e.get("state")) for e in world.store.events(execution_id)]


def test_an_invoke_is_written_before_the_owner_is_asked(world):
    seen = []
    world.invoker.on_start = lambda job: (seen.append(world.store.get(job.execution_id)["state"]), job.dispatched())
    execution = invoke(world)
    assert seen == ["queued"] and execution["state"] == "dispatched"
    assert execution["actor_id"] == "gui-user" and execution["resource_id"] == "web"


def test_the_owner_reports_and_the_outcome_is_kept(world):
    execution = invoke(world)
    job = world.invoker.started[0]
    assert job.progress("service.status", {"status": "starting"})
    assert job.running()
    assert job.succeed({"service_status": "running"})
    done = world.engine.get(world.user, execution["execution_id"])
    assert done["state"] == "succeeded" and done["output"] == {"service_status": "running"}
    assert kinds(world, done["execution_id"]) == [("state", "queued"), ("state", "dispatched"),
                                                  ("service.status", None), ("state", "running"),
                                                  ("state", "succeeded")]
    assert world.timers.made[0].cancelled and job.finished
    # A late or repeated report changes nothing.
    assert job.fail("service_failed") is False and job.progress("x") is False
    assert world.engine.get(world.user, done["execution_id"])["state"] == "succeeded"


def test_an_owner_that_answers_at_once_still_went_through_dispatched(world):
    world.invoker.on_start = lambda job: job.succeed({"service_status": "running"})
    execution = invoke(world)
    assert execution["state"] == "succeeded"
    assert [s for _, s in kinds(world, execution["execution_id"])] == ["queued", "dispatched", "succeeded"]


@pytest.mark.parametrize("grants, code", [
    (["services.read"], "not_found"),                      # may see the service, not start it
    ([], "not_found"),                                     # may not even see it
])
def test_without_the_grant_nothing_is_written_or_started(world, grants, code):
    caller = world.authority.issue("other", [Grant("p", g) for g in grants])
    with pytest.raises(QCRError) as caught:
        invoke(world, caller)
    assert caught.value.code == code
    assert world.store.list("p") == [] and world.invoker.started == []


def test_the_whole_closure_is_checked_before_any_effect(world):
    world.invoker.needs = [(DB, "services.start")]
    web_only = world.authority.issue("web-only", [Grant("p", "services.read"),
                                                  Grant("p", "services.start", "web")])
    with pytest.raises(QCRError) as caught:
        invoke(world, web_only)
    assert caught.value.code == "authorization_denied"
    assert world.store.list("p") == [] and world.invoker.started == []
    allowed = world.authority.issue("lead", [Grant("p", "services.read"), Grant("p", "services.start")])
    assert invoke(world, allowed)["state"] == "dispatched"


def test_an_owner_that_cannot_check_refuses_without_effect(world):
    world.invoker.needs = RuntimeError("supervisor gone")
    with pytest.raises(QCRError) as caught:
        invoke(world)
    assert caught.value.code == "owner_unavailable" and world.store.list("p") == []


def test_bad_input_is_refused_before_anything(world):
    with pytest.raises(QCRError) as caught:
        invoke(world, request={"wait_ready": "yes"})
    assert caught.value.code == "invalid_input" and world.store.list("p") == []


def test_a_second_invoke_on_a_busy_resource_names_the_first(world):
    first = invoke(world)
    with pytest.raises(QCRError) as caught:
        invoke(world)
    assert caught.value.code == "busy" and caught.value.execution_id == first["execution_id"]
    assert len(world.invoker.started) == 1


def test_an_idempotent_repeat_returns_the_same_execution_and_starts_nothing(world):
    first = invoke(world, idempotency_key="k")
    again = invoke(world, idempotency_key="k")
    assert again["execution_id"] == first["execution_id"] and len(world.invoker.started) == 1
    with pytest.raises(QCRError) as caught:
        world.engine.invoke(world.user, "p", "services.web.probe", {}, idempotency_key="k2")
    assert caught.value.code == "invalid_input"


def test_the_deadline_ends_it_and_the_owner_stops_waiting(world):
    execution = invoke(world)
    assert world.timers.made[0].seconds == 120
    world.timers.fire_last()
    after = world.engine.get(world.user, execution["execution_id"])
    assert after["state"] == "timed_out" and after["error"]["code"] == "timed_out"
    assert world.invoker.cancelled == [execution["execution_id"]]
    assert world.invoker.started[0].succeed({"service_status": "running"}) is False
    assert world.engine.get(world.user, execution["execution_id"])["state"] == "timed_out"


def test_cancel_ends_waiting_and_is_not_a_stop(world):
    execution = invoke(world)
    cancelled = world.engine.cancel(world.user, execution["execution_id"], reason="changed my mind")
    assert cancelled["state"] == "canceled" and cancelled["error"]["code"] == "canceled"
    assert world.store.events(execution["execution_id"])[-1]["payload"] == {"by": "gui-user",
                                                                             "reason": "changed my mind"}
    assert world.invoker.cancelled == [execution["execution_id"]]
    assert [job.capability.capability_id for job in world.invoker.started] == [START], "no stop was invoked"
    assert world.engine.cancel(world.user, execution["execution_id"])["state"] == "canceled"


def test_only_the_actor_or_a_granted_caller_sees_or_cancels(world):
    execution = invoke(world)
    stranger = world.authority.issue("stranger", [Grant("p", "services.read")])
    for call in (world.engine.get, world.engine.cancel):
        with pytest.raises(QCRError) as caught:
            call(stranger, execution["execution_id"])
        assert caught.value.code == "not_found"
    assert world.engine.list(stranger, "p") == []
    auditor = world.authority.issue("auditor", [Grant("p", "executions.read"), Grant("p", "executions.cancel")])
    assert [e["execution_id"] for e in world.engine.list(auditor, "p")] == [execution["execution_id"]]
    assert world.engine.cancel(auditor, execution["execution_id"])["state"] == "canceled"
    world.authority.revoke(world.user)
    with pytest.raises(QCRError) as revoked:
        world.engine.get(world.user, execution["execution_id"])
    assert revoked.value.code == "authorization_denied"


def test_what_cannot_be_canceled_says_so(world):
    execution = world.engine.invoke(world.user, "p", "services.web.probe", {})
    with pytest.raises(QCRError) as caught:
        world.engine.cancel(world.user, execution["execution_id"])
    assert caught.value.code == "not_cancelable"


def test_an_unusable_result_or_a_failed_start_is_a_failure_not_a_success(world):
    world.invoker.on_start = lambda job: job.succeed({"service_status": 5})
    first = invoke(world)
    assert first["state"] == "failed" and first["error"]["code"] == "invalid_output"

    def broken(job):
        raise RuntimeError("bridge down")
    world.invoker.on_start = broken
    second = invoke(world)
    assert second["state"] == "failed" and second["error"]["code"] == "dispatch_failed"


def test_a_success_larger_than_the_journal_keeps_is_still_a_success(world):
    # The owner made its change (a Jira comment was posted); calling that a
    # failure invites the agent to make it again.
    big = "x" * (70 * 1024)
    world.invoker.on_start = lambda job: job.succeed({"service_status": big})
    first = invoke(world)
    assert first["state"] == "succeeded", first
    assert first["output"] == {"omitted": ["service_status"]}


def test_a_journal_that_cannot_be_written_means_the_owner_is_never_asked(world, monkeypatch):
    def unavailable(*args, **kwargs):
        from qcr.executions import ExecutionError
        raise ExecutionError("journal_unavailable", "Execution journal is unavailable.")
    monkeypatch.setattr(world.store, "create", unavailable)
    with pytest.raises(QCRError) as caught:
        invoke(world)
    assert caught.value.code == "journal_unavailable" and world.invoker.started == []


def test_wait_wakes_when_the_owner_finishes_on_another_thread(world):
    execution = invoke(world)
    job = world.invoker.started[0]
    assert world.engine.wait(world.user, execution["execution_id"], timeout=0)["state"] == "dispatched"
    threading.Timer(0.05, lambda: job.succeed({"service_status": "running"})).start()
    assert world.engine.wait(world.user, execution["execution_id"], timeout=10)["state"] == "succeeded"


def test_subscribe_follows_a_cursor_and_waits_for_news(world):
    execution = invoke(world)
    eid = execution["execution_id"]
    first = world.engine.subscribe(world.user, eid)
    assert [e.get("state") for e in first["events"]] == ["queued", "dispatched"]
    assert world.engine.subscribe(world.user, eid, first["cursor"])["events"] == []
    job = world.invoker.started[0]
    threading.Timer(0.05, lambda: job.progress("service.status", {"status": "starting"})).start()
    news = world.engine.subscribe(world.user, eid, first["cursor"], timeout=10)
    assert [e["type"] for e in news["events"]] == ["service.status"] and news["state"] == "dispatched"


def test_reports_racing_a_cancel_leave_exactly_one_outcome(world):
    for _ in range(20):
        execution = invoke(world)
        job = world.invoker.started[-1]
        racer = threading.Thread(target=lambda: job.succeed({"service_status": "running"}))
        racer.start()
        world.engine.cancel(world.user, execution["execution_id"])
        racer.join()
        final = world.engine.get(world.user, execution["execution_id"])
        assert final["state"] in ("succeeded", "canceled")
        assert [e.get("state") for e in world.store.events(execution["execution_id"])].count(final["state"]) == 1


def test_recover_marks_a_previous_life_unknown(tmp_path):
    store = ExecutionStore(str(tmp_path / "e.sqlite"))
    store.create("p", START, 1, "gui-user", {}, resource_id="web", exclusive=True)
    engine = Engine(Registry(Authority()), store, schedule=Timers())
    ids = engine.recover(lambda execution: {"service_status": "running"})
    assert len(ids) == 1 and store.get(ids[0])["state"] == "unknown"
    store.close()
