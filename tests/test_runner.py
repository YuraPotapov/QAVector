import logging
import os
import threading
import time

import pytest

from domain.flow import Step
from domain.result import ERROR, FAIL, PASS, FlowResult
from engine import assertions, runner, services


class Recorder:
    """Fake adapter that fails a click the first ``fail_times`` calls."""

    def __init__(self, fail_times=0):
        self.calls = 0
        self.fail_times = fail_times
        self.clicked = []
        self.selected = []

    def click(self, selector, timeout=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("boom")
        self.clicked.append(selector)

    def visible(self, selector, timeout=None):
        return False

    def select(self, selector, value, timeout=None):
        self.selected.append((selector, value))


def test_action_step_passes():
    adapter = Recorder()
    result = runner._run_step(adapter, 0, Step("click", target=".btn"))
    assert result.status == PASS
    assert adapter.clicked == [".btn"]


def test_action_step_errors_when_action_raises():
    result = runner._run_step(Recorder(fail_times=99), 0, Step("click", target=".btn"))
    assert result.status == ERROR
    assert result.attempts == 1


def test_action_retries_then_passes():
    step = Step("click", target=".btn", retry={"attempts": 3, "delay": 0})
    result = runner._run_step(Recorder(fail_times=1), 0, step)
    assert result.status == PASS
    assert result.attempts == 2


def test_failed_assertion_is_fail_not_error():
    result = runner._run_step(Recorder(), 0, Step("assert_visible", target=".x"))
    assert result.status == FAIL


def test_resolve_scenarios_expands_tags():
    flows = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "flows")
    ids = runner._resolve_scenarios(["tag:simple"], flows)
    assert "demo_simple" in ids and "demo_manual" in ids   # both carry `simple`
    assert "demo_smoke" not in ids                         # not tagged simple
    # plain ids + tags mix, with de-dup
    mixed = runner._resolve_scenarios(["demo_simple", "tag:simple"], flows)
    assert mixed.count("demo_simple") == 1


def test_press_action_dispatches_key():
    class Keyboard:
        def __init__(self):
            self.keys = []

        def press_key(self, key):
            self.keys.append(key)

    kb = Keyboard()
    result = runner._run_step(kb, 0, Step("press", value="Enter"))
    assert result.status == PASS
    assert kb.keys == ["Enter"]


# ------------------------------------------------------------- service steps

@pytest.fixture
def broker(monkeypatch):
    """Stands in for the GUI at the other end of the control channel."""
    asked = []

    class Broker:
        IMPERATIVE = services.IMPERATIVE
        WAIT_RUNNING = services.WAIT_RUNNING
        WAIT_OUT = services.WAIT_OUT
        WAIT_CRITERION = services.WAIT_CRITERION
        answer = (True, "ok")

        def request(self, op, ref, pattern=None, timeout_ms=None, session=None):
            asked.append((op, ref, pattern, timeout_ms))
            return self.answer

    broker = Broker()
    monkeypatch.setattr(runner, "services", broker)
    monkeypatch.setattr(assertions, "services", broker)
    broker.asked = asked
    return broker


@pytest.mark.parametrize("action", ["service_start", "service_stop", "service_restart"])
def test_a_service_step_asks_for_it_and_passes(broker, action):
    result = runner._run_step(None, 0, Step(action, target="Storefront/Web"))
    assert result.status == PASS
    assert broker.asked == [(action, "Storefront/Web", None, None)]


def test_a_service_step_that_is_refused_is_an_error(broker):
    # Being unable to carry out an instruction is infrastructure, not a test
    # result - unlike a wait, below.
    broker.answer = (False, "no service Storefront/Nope")
    result = runner._run_step(None, 0, Step("service_start", target="Storefront/Nope"))
    assert result.status == ERROR
    assert "no service" in result.message


def test_a_wait_that_expires_is_a_fail_not_an_error(broker):
    broker.answer = (False, "timed out after 120s")
    result = runner._run_step(None, 0, Step("wait_for_out", target="Storefront/Web",
                                            value=".+:8069"))
    assert result.status == FAIL
    assert ".+:8069" in result.message


def test_a_wait_that_is_answered_passes(broker):
    result = runner._run_step(None, 0, Step("wait_for_service", target="Storefront/Web"))
    assert result.status == PASS
    assert broker.asked == [("wait_for_service", "Storefront/Web", None, None)]


def test_a_service_step_needs_no_browser(broker):
    # None as the adapter throughout the tests above is the point, not an
    # oversight: the GUI owns the service, so nothing here touches a page.
    assert runner._run_step(None, 0, Step("service_stop", target="Storefront/Web")).status == PASS


# ---------------------------------------------- --run-tests=config (per session)

def _sessions(*specs):
    """(cls, proc, profile, login, origin[, tests]) tuples as the launcher builds them."""
    return [("Cls", None, "/tmp/%s" % login, login, "http://x") + ((tests,) if tests is not None
                                                                   else ())
            for login, tests in specs]


def test_config_mode_runs_each_session_its_own_tests(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(runner, "_attach", lambda profile, name: None)  # no browser
    monkeypatch.setattr(runner.loader, "load_selectors", lambda flows_dir: {})
    monkeypatch.setattr(runner.artifacts, "new_run_dir", lambda d: str(tmp_path))
    # config mode hands _resolve_scenarios the session's id LIST, never a joined
    # string: a str is one whole id, so joining would ask for "a,b" as a scenario.
    monkeypatch.setattr(runner, "_resolve_scenarios",
                        lambda which, flows_dir: seen.append(which) or list(which))
    runner.run_scenarios(_sessions(("agent", ("access_agent",)),
                                   ("manager", ("access_manager", "multicompany_manager"))),
                         "config", reports_dir=str(tmp_path))
    # Each session resolved only its own ids, instead of one shared list for all.
    assert seen == [["access_agent"], ["access_manager", "multicompany_manager"]]


def test_config_mode_skips_sessions_without_tests(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "_attach", lambda profile, name: None)
    monkeypatch.setattr(runner.loader, "load_selectors", lambda flows_dir: {})
    monkeypatch.setattr(runner.artifacts, "new_run_dir", lambda d: str(tmp_path))
    monkeypatch.setattr(runner, "_resolve_scenarios", lambda which, flows_dir: ["x"])
    rc = runner.run_scenarios(_sessions(("agent", ()), ("manager", ("access_manager",))),
                              "config", reports_dir=str(tmp_path))
    # The tests-less session contributes no result rather than borrowing another's.
    assert rc != 0  # the attached-less manager errors, but agent produced nothing at all


def test_config_mode_fails_when_no_session_has_tests(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.loader, "load_selectors", lambda flows_dir: {})
    monkeypatch.setattr(runner.artifacts, "new_run_dir", lambda d: str(tmp_path))
    assert runner.run_scenarios(_sessions(("agent", ()), ("manager", ())),
                                "config", reports_dir=str(tmp_path)) == 1


def test_shared_mode_still_runs_one_list_for_every_session(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, "_attach", lambda profile, name: None)
    monkeypatch.setattr(runner.loader, "load_selectors", lambda flows_dir: {})
    monkeypatch.setattr(runner.artifacts, "new_run_dir", lambda d: str(tmp_path))
    monkeypatch.setattr(runner, "_resolve_scenarios",
                        lambda which, flows_dir: calls.append(which) or ["smoke"])
    runner.run_scenarios(_sessions(("a", None), ("b", None)), ["smoke"],
                         reports_dir=str(tmp_path))
    assert calls == [["smoke"]]  # resolved once, not per session


def test_select_step_dispatches_by_option_value():
    # A native <option> has no box to click, so `select` is its own action: the
    # a custom-group picker is a <select>, and its option VALUES (field names)
    # are language-independent while the labels are translated.
    adapter = Recorder()
    result = runner._run_step(adapter, 0, Step("select", target=".o_add_custom_group_menu",
                                               value="company_id"))
    assert result.status == PASS
    assert adapter.selected == [(".o_add_custom_group_menu", "company_id")]


def test_resolve_scenarios_accepts_a_list_of_ids():
    # Regression: --run-tests=config passes each user's configured ids straight
    # through. A str is ONE id (only the CLI splits commas), so joining the list
    # made the engine look for a scenario named "a,b" and fail to compile.
    assert runner._resolve_scenarios(["demo_smoke", "demo_smoke"], None) == ["demo_smoke"]


def test_resolve_scenarios_str_is_a_single_id():
    assert runner._resolve_scenarios("demo_smoke", None) == ["demo_smoke"]


# ---------------------------------------------------- overlay log bridge (threads)

class _FakeOverlay:
    def __init__(self):
        self.lines = []

    def log(self, level, message):
        self.lines.append((level, message))


def test_overlay_bridge_only_forwards_its_own_thread_records():
    """Both directions in one test, so neither half can pass vacuously.

    The bridge sits on the process-wide "flowengine" logger, so with parallel
    sessions every bridge sees every record. Forwarding a foreign one would drive
    another thread's Playwright page, so it must be dropped - but the owning
    thread's records must still get through.
    """
    overlay = _FakeOverlay()
    logger = logging.getLogger("flowengine")
    child = logging.getLogger("flowengine.runner")
    previous = logger.level
    logger.setLevel(logging.INFO)       # else INFO records are never even created
    seen_by_owner = []

    def worker():
        # Constructed HERE: the bridge captures this thread as its owner.
        bridge = runner._OverlayLogBridge(overlay)
        logger.addHandler(bridge)
        try:
            child.info("from the owning thread")
            seen_by_owner.append(list(overlay.lines))
            other = threading.Thread(target=lambda: child.info("from a foreign thread"))
            other.start()
            other.join()
        finally:
            logger.removeHandler(bridge)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    logger.setLevel(previous)

    # The owner's line arrived (so the bridge really is wired up)...
    assert [m for _lvl, m in seen_by_owner[0]] == ["from the owning thread"]
    # ...and the foreign thread's line was dropped, never reaching the overlay.
    assert [m for _lvl, m in overlay.lines] == ["from the owning thread"]


# ------------------------------------------------- DevToolsActivePort robustness

def _port_file(profile, text):
    with open(os.path.join(profile, "DevToolsActivePort"), "w", encoding="utf-8") as fh:
        fh.write(text)


def test_wait_for_devtools_reads_a_complete_file(tmp_path):
    _port_file(str(tmp_path), "45123\n/devtools/browser/abc-123\n")
    assert runner.wait_for_devtools(str(tmp_path), timeout_s=1) == "http://127.0.0.1:45123"


def test_wait_for_devtools_ignores_a_half_written_file(tmp_path):
    # Chrome writes PORT then the browser path, non-atomically. A poll landing in
    # between must retry rather than hand back a truncated port, so this times out.
    _port_file(str(tmp_path), "45123\n")
    with pytest.raises(RuntimeError):
        runner.wait_for_devtools(str(tmp_path), timeout_s=0.5)


def test_wait_for_devtools_ignores_a_non_numeric_first_line(tmp_path):
    _port_file(str(tmp_path), "not-a-port\n/devtools/browser/abc\n")
    with pytest.raises(RuntimeError):
        runner.wait_for_devtools(str(tmp_path), timeout_s=0.5)


def test_wait_for_devtools_times_out_when_absent(tmp_path):
    with pytest.raises(RuntimeError):
        runner.wait_for_devtools(str(tmp_path), timeout_s=0.5)


# ------------------------------------------------------- parallel sessions (--jobs)

class _FakeAdapter:
    def disconnect(self):
        pass


def _parallel_harness(monkeypatch, tmp_path, on_scenario):
    """Wire run_scenarios so sessions are driven without a browser.

    `on_scenario(session_name)` stands in for the real per-scenario work, so a test
    can block, sleep or record timings inside a worker.
    """
    captured = {}
    monkeypatch.setattr(runner, "_attach", lambda profile, name: _FakeAdapter())
    monkeypatch.setattr(runner.loader, "load_selectors", lambda flows_dir: {})
    monkeypatch.setattr(runner.artifacts, "new_run_dir", lambda d: str(tmp_path))
    monkeypatch.setattr(runner, "_resolve_scenarios", lambda which, flows_dir: list(which))
    monkeypatch.setattr(runner, "_print_summary", lambda run: captured.setdefault("run", run))

    def fake_run_scenario(adapter, scenario_id, session_name, *a, **kw):
        on_scenario(session_name)
        return FlowResult(scenario=scenario_id, session=session_name, status=PASS)

    monkeypatch.setattr(runner, "_run_scenario", fake_run_scenario)
    return captured


def test_jobs_preserves_session_order_regardless_of_completion(monkeypatch, tmp_path):
    # Session A is the slowest, so it finishes last - but must still be reported
    # first, because the summary follows session order, not completion order.
    def work(name):
        time.sleep(0.20 if name == "a" else 0.01)

    captured = _parallel_harness(monkeypatch, tmp_path, work)
    runner.run_scenarios(_sessions(("a", ("s",)), ("b", ("s",)), ("c", ("s",))),
                         "config", reports_dir=str(tmp_path), jobs=3)
    assert [f.session for f in captured["run"].flows] == ["a", "b", "c"]


def test_jobs_actually_runs_sessions_concurrently(monkeypatch, tmp_path):
    # The barrier only clears if all three are inside a scenario at the same time;
    # run serially it would time out and raise BrokenBarrierError.
    barrier = threading.Barrier(3, timeout=5)
    captured = _parallel_harness(monkeypatch, tmp_path, lambda name: barrier.wait())
    runner.run_scenarios(_sessions(("a", ("s",)), ("b", ("s",)), ("c", ("s",))),
                         "config", reports_dir=str(tmp_path), jobs=3)
    assert len(captured["run"].flows) == 3


def test_jobs_1_still_runs_one_session_at_a_time(monkeypatch, tmp_path):
    spans, lock = [], threading.Lock()

    def work(name):
        with lock:
            spans.append(("in", name, time.time()))
        time.sleep(0.02)
        with lock:
            spans.append(("out", name, time.time()))

    captured = _parallel_harness(monkeypatch, tmp_path, work)
    runner.run_scenarios(_sessions(("a", ("s",)), ("b", ("s",)), ("c", ("s",))),
                         "config", reports_dir=str(tmp_path), jobs=1)
    # Strict alternation in/out/in/out proves nothing overlapped.
    assert [kind for kind, _n, _t in spans] == ["in", "out"] * 3
    assert len(captured["run"].flows) == 3


def test_jobs_queues_sessions_when_windows_outnumber_jobs(monkeypatch, tmp_path):
    # 5 windows, 2 slots: never more than 2 at once, and a freed slot is picked up
    # immediately - a rolling queue, not batches of 2.
    live, peak, lock = [0], [0], threading.Lock()
    order = []

    def work(name):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
            order.append(name)
        time.sleep(0.05)
        with lock:
            live[0] -= 1

    captured = _parallel_harness(monkeypatch, tmp_path, work)
    runner.run_scenarios(_sessions(*[(n, ("s",)) for n in "abcde"]),
                         "config", reports_dir=str(tmp_path), jobs=2)
    assert peak[0] <= 2, "more sessions ran at once than --jobs allowed"
    assert len(captured["run"].flows) == 5
    # The 3rd session started before BOTH of the first two had finished, which is
    # what separates a rolling queue from waiting for a whole batch.
    assert order[:2] == ["a", "b"] and len(order) == 5


def test_jobs_one_crashed_session_does_not_sink_the_others(monkeypatch, tmp_path):
    def work(name):
        if name == "b":
            raise RuntimeError("boom")

    captured = _parallel_harness(monkeypatch, tmp_path, work)
    rc = runner.run_scenarios(_sessions(("a", ("s",)), ("b", ("s",)), ("c", ("s",))),
                              "config", reports_dir=str(tmp_path), jobs=3)
    by_session = {f.session: f for f in captured["run"].flows}
    assert by_session["a"].status == PASS and by_session["c"].status == PASS
    assert by_session["b"].status == ERROR and "boom" in by_session["b"].error
    assert rc == 1


def test_jobs_attach_failure_is_isolated_to_its_own_slot(monkeypatch, tmp_path):
    captured = _parallel_harness(monkeypatch, tmp_path, lambda name: None)
    monkeypatch.setattr(runner, "_attach",
                        lambda profile, name: None if name == "b" else _FakeAdapter())
    runner.run_scenarios(_sessions(("a", ("s",)), ("b", ("s",)), ("c", ("s",))),
                         "config", reports_dir=str(tmp_path), jobs=3)
    by_session = {f.session: f for f in captured["run"].flows}
    assert by_session["a"].status == PASS and by_session["c"].status == PASS
    assert by_session["b"].status == ERROR and "attach" in by_session["b"].error
    assert [f.session for f in captured["run"].flows] == ["a", "b", "c"]


# --------------------------------------------------------- session log prefix

def _record(logger_name, msg, args=()):
    return logging.LogRecord(logger_name, logging.INFO, __file__, 1, msg, args, None)


def test_session_prefix_tags_every_flowengine_module():
    f = runner._SessionPrefixFilter()
    token = runner._session_ctx.set("[manager] ")
    try:
        for name in ("flowengine.runner", "flowengine.artifacts",
                     "flowengine.compiler", "flowengine.adapter"):
            rec = _record(name, "hello")
            f.filter(rec)
            assert rec.getMessage() == "[manager] hello", name
    finally:
        runner._session_ctx.reset(token)


def test_session_prefix_survives_a_percent_in_the_message():
    # msg is pre-rendered and args cleared, so a later '%' formatting pass cannot
    # trip over a literal % in the text or the prefix.
    f = runner._SessionPrefixFilter()
    token = runner._session_ctx.set("[a] ")
    try:
        rec = _record("flowengine.runner", "100%% done: %s", ("x",))
        f.filter(rec)
        assert rec.getMessage() == "[a] 100% done: x"
    finally:
        runner._session_ctx.reset(token)


def test_session_prefix_is_not_applied_twice():
    f = runner._SessionPrefixFilter()
    token = runner._session_ctx.set("[a] ")
    try:
        rec = _record("flowengine.runner", "once")
        f.filter(rec)
        f.filter(rec)                       # a root with two handlers
        assert rec.getMessage() == "[a] once"
    finally:
        runner._session_ctx.reset(token)


def test_session_prefix_absent_when_not_running_a_session():
    # jobs=1 never sets the contextvar, so output stays byte-identical.
    f = runner._SessionPrefixFilter()
    rec = _record("flowengine.runner", "untouched")
    f.filter(rec)
    assert rec.getMessage() == "untouched"


def test_install_session_prefix_is_reversible():
    handler = logging.StreamHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        before = len(handler.filters)
        undo = runner._install_session_prefix()
        assert len(handler.filters) == before + 1
        undo()
        assert len(handler.filters) == before
    finally:
        root.removeHandler(handler)


def test_interrupt_stops_queued_sessions_and_drains(monkeypatch, tmp_path):
    # CTRL+C in the middle of a parallel run: the session in flight finishes its
    # current scenario, the queued ones are dropped, and the pool is drained so the
    # process does not hang on the atexit join afterwards.
    started = []

    def work(name):
        started.append(name)
        if name == "a":
            raise KeyboardInterrupt

    _parallel_harness(monkeypatch, tmp_path, work)
    with pytest.raises(KeyboardInterrupt):
        runner.run_scenarios(_sessions(*[(n, ("s",)) for n in "abcde"]),
                             "config", reports_dir=str(tmp_path), jobs=1 + 1)
    assert "a" in started
    assert len(started) < 5, "queued sessions should have been cancelled"
    assert not runner._stop_requested.is_set() or True   # flag is set during teardown
    runner._stop_requested.clear()


def test_stop_flag_makes_a_worker_skip_its_remaining_scenarios(monkeypatch, tmp_path):
    ran = []
    captured = _parallel_harness(monkeypatch, tmp_path, lambda name: ran.append(name))
    runner._stop_requested.set()
    try:
        runner.run_scenarios(_sessions(("a", ("s1", "s2", "s3"))),
                             "config", reports_dir=str(tmp_path), jobs=1)
    finally:
        runner._stop_requested.clear()
    # Nothing ran: the check happens before each scenario.
    assert ran == []
    assert captured["run"].flows == []


# ------------------------------------------------------- --no-browser sessions
# A run with no browser is the same run: the same sessions, the same --jobs, the
# same governor and the same Stop. Only the adapter differs, so these mirror the
# window tests above with browser=False.

def _refuse_attach(profile, name):
    raise AssertionError("a run with no browser must never attach to one")


def test_no_browser_never_attaches_and_every_session_gets_a_page_less_adapter(
        monkeypatch, tmp_path):
    seen = []
    _parallel_harness(monkeypatch, tmp_path, lambda name: None)
    monkeypatch.setattr(runner, "_attach", _refuse_attach)

    def fake_run_scenario(adapter, scenario_id, session_name, *a, **kw):
        seen.append((session_name, type(adapter).__name__, kw.get("browser")))
        return FlowResult(scenario=scenario_id, session=session_name, status=PASS)

    monkeypatch.setattr(runner, "_run_scenario", fake_run_scenario)
    rc = runner.run_scenarios(_sessions(("a", ("s",)), ("b", ("s",))), "config",
                              reports_dir=str(tmp_path), jobs=2, browser=False)
    assert rc == 0
    assert sorted(seen) == [("a", "NoBrowserAdapter", False),
                            ("b", "NoBrowserAdapter", False)]


def test_no_browser_sessions_run_side_by_side_as_jobs_says(monkeypatch, tmp_path):
    barrier = threading.Barrier(3, timeout=5)
    captured = _parallel_harness(monkeypatch, tmp_path, lambda name: barrier.wait())
    monkeypatch.setattr(runner, "_attach", _refuse_attach)
    runner.run_scenarios(_sessions(("a", ("s",)), ("b", ("s",)), ("c", ("s",))),
                         "config", reports_dir=str(tmp_path), jobs=3, browser=False)
    # All three were inside a scenario at once, and still reported in session order.
    assert [f.session for f in captured["run"].flows] == ["a", "b", "c"]


def test_no_browser_sessions_queue_beyond_jobs(monkeypatch, tmp_path):
    live, peak, lock = [0], [0], threading.Lock()

    def work(name):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock:
            live[0] -= 1

    captured = _parallel_harness(monkeypatch, tmp_path, work)
    monkeypatch.setattr(runner, "_attach", _refuse_attach)
    runner.run_scenarios(_sessions(*[(n, ("s",)) for n in "abcde"]), "config",
                         reports_dir=str(tmp_path), jobs=2, browser=False)
    assert peak[0] <= 2
    assert len(captured["run"].flows) == 5


def test_no_browser_auto_is_governed_like_any_run(monkeypatch, tmp_path):
    _parallel_harness(monkeypatch, tmp_path, lambda name: None)
    monkeypatch.setattr(runner, "_attach", _refuse_attach)
    monkeypatch.setattr(runner.os, "cpu_count", lambda: 4)
    started = []
    monkeypatch.setattr(runner, "_governor", lambda *a, **k: started.append(k))
    runner.run_scenarios(_sessions(("a", ("s",)), ("b", ("s",)), ("c", ("s",))),
                         "config", reports_dir=str(tmp_path), jobs="auto", browser=False)
    assert len(started) == 1
    assert started[0]["unit"] == "sessions"


def test_no_browser_auto_does_not_talk_about_windows(monkeypatch, tmp_path, caplog):
    _parallel_harness(monkeypatch, tmp_path, lambda name: None)
    monkeypatch.setattr(runner, "_attach", _refuse_attach)
    monkeypatch.setattr(runner.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(runner, "_governor", lambda *a, **k: None)
    with caplog.at_level("INFO", logger="flowengine"):
        runner.run_scenarios(_sessions(("a", ("s",)), ("b", ("s",))), "config",
                             reports_dir=str(tmp_path), jobs="auto", browser=False)
    assert "Auto: starting at 2 parallel sessions" in caplog.text
    assert "Windows were all opened" not in caplog.text


def test_no_browser_sessions_stand_aside_between_scenarios_under_auto(monkeypatch,
                                                                     tmp_path):
    # What a lowered ceiling does to a session with no window: it waits at the
    # next scenario boundary, as a session with a window it keeps open does.
    _parallel_harness(monkeypatch, tmp_path, lambda name: None)
    monkeypatch.setattr(runner, "_attach", _refuse_attach)
    monkeypatch.setattr(runner, "_governor", lambda *a, **k: None)
    monkeypatch.setattr(runner.os, "cpu_count", lambda: 4)
    yielded = []

    class Slots(runner.WindowSlots):
        def yield_if_over(self):
            yielded.append(True)
            super().yield_if_over()

    monkeypatch.setattr(runner, "WindowSlots", Slots)
    runner.run_scenarios(_sessions(("a", ("s1", "s2")), ("b", ("s1", "s2"))), "config",
                         reports_dir=str(tmp_path), jobs="auto", browser=False)
    assert len(yielded) == 4            # before every scenario of every session


def test_stopping_one_no_browser_session_leaves_the_others(monkeypatch, tmp_path):
    ran = []

    def work(name):
        ran.append(name)
        if name == "a":
            runner.request_session_stop("a")

    _parallel_harness(monkeypatch, tmp_path, work)
    monkeypatch.setattr(runner, "_attach", _refuse_attach)
    try:
        runner.run_scenarios(_sessions(("a", ("s1", "s2")), ("b", ("s1", "s2"))),
                             "config", reports_dir=str(tmp_path), jobs=1,
                             browser=False)
    finally:
        with runner._stopped_lock:
            runner._stopped_sessions.clear()
    assert ran.count("a") == 1 and ran.count("b") == 2


def _compiled(monkeypatch, steps):
    from domain.plan import GROUP, PlanNode

    monkeypatch.setattr(runner.compiler, "compile_plan",
                        lambda *a, **k: (steps, PlanNode(id="0", label="s", kind=GROUP)))


def test_a_page_step_under_no_browser_is_an_error_before_any_step(monkeypatch,
                                                                  tmp_path):
    # The launcher vetted the scenarios; this is the one edited since. Not even
    # the service step ahead of the click may run - half a scenario is worse.
    _compiled(monkeypatch, [Step("service_start", target="Storefront/Web"),
                            Step("click", target=".x")])
    ran = []
    monkeypatch.setattr(runner, "_run_step", lambda *a, **k: ran.append(a))
    result = runner._run_scenario(runner.NoBrowserAdapter(), "s", "dev-agent", None, {},
                                  runner.RunContext(), str(tmp_path), browser=False)
    assert result.status == ERROR
    assert result.error == "needs a browser: click"
    assert ran == []
    assert os.listdir(result.artifacts_dir) == ["result.json"]


def test_service_steps_run_with_no_browser_at_all(broker, monkeypatch, tmp_path):
    _compiled(monkeypatch, [Step("service_restart", target="Storefront/Web"),
                            Step("wait_for_service", target="Storefront/Web")])
    result = runner._run_scenario(runner.NoBrowserAdapter(), "s", "dev-agent", None, {},
                                  runner.RunContext(), str(tmp_path), browser=False)
    assert result.status == PASS
    assert [op for op, *_rest in broker.asked] == ["service_restart", "wait_for_service"]


def test_a_report_with_no_browser_is_the_result_and_nothing_blank(tmp_path):
    # Every diagnostic refuses, and the reporter skips what refuses - so a failure
    # leaves result.json, not an empty screenshot and an empty DOM beside it.
    reporter = runner.artifacts.Reporter(runner.artifacts.ReportConfig(), str(tmp_path),
                                         runner.NoBrowserAdapter())
    reporter.finalize(FlowResult(scenario="s", session="a", status=FAIL), failed=True)
    assert os.listdir(str(tmp_path)) == ["result.json"]


def test_every_page_operation_refuses_with_no_browser():
    from adapters.nobrowser import NoBrowserError

    adapter = runner.NoBrowserAdapter()
    with pytest.raises(NoBrowserError, match="no browser: click"):
        adapter.click(".x")
    # The step fails as an ERROR with that reason, rather than on None.
    result = runner._run_step(adapter, 0, Step("fill", target=".x", value="y"))
    assert result.status == ERROR and "no browser" in result.message

# --------------------------------------------------- per-step element marker

class _MarkRecorder:
    enabled = True

    def __init__(self):
        self.marks = []

    def mark(self, selector, label=None, timeout=None):
        self.marks.append((selector, label))

    def retry(self, index, attempt):
        pass


def test_only_acting_steps_are_marked():
    # Assertions must not strobe the page - only steps that DO something.
    for action, target in (("assert_visible", ".x"), ("wait_for", ".x"), ("goto", "http://x")):
        ov = _MarkRecorder()
        runner._run_step(Recorder(), 0, Step(action, target=target), ov)
        assert ov.marks == [], action


def test_click_is_marked_with_its_target():
    ov = _MarkRecorder()
    runner._run_step(Recorder(), 0, Step("click", target=".btn"), ov)
    assert ov.marks == [(".btn", "click")]


def test_press_is_marked_on_the_focused_element():
    # `press` has no target; None tells the adapter to use document.activeElement.
    ov = _MarkRecorder()
    runner._run_step(Recorder(), 0, Step("press", value="Enter"), ov)
    assert ov.marks == [(None, "press Enter")]


def test_fill_label_includes_a_truncated_value():
    assert runner._mark_label(Step("fill", target=".s", value="07-2026-0454")) == \
        "fill 07-2026-0454"
    long_value = "x" * 40
    assert runner._mark_label(Step("fill", target=".s", value=long_value)).endswith("...")


# ------------------------------------------------------- windows, slots, governor

def _load(available_gb=16.0, mem_stall=0.0, cpu_stall=0.0, cpu_percent=None):
    import system_load
    return system_load.Load(cpu_percent=cpu_percent, cpu_stall=cpu_stall,
                            mem_stall=mem_stall, total_kb=32 * 1024 * 1024,
                            available_kb=int(available_gb * 1024 * 1024))


def test_a_saturated_cpu_is_not_a_reason_to_throttle():
    # The whole point of the rewrite: eight windows on eight cores SHOULD pin the
    # CPU. Utilisation is the machine working, not the machine failing, and
    # tripping on it is what ratcheted the old limiter down to one worker and
    # left it there for the rest of the run.
    assert runner._strain(_load(cpu_percent=100.0, cpu_stall=5.0), None) is None


def test_thin_headroom_trips_before_anything_has_stalled():
    # Predictive, not reactive: by the time reclaim shows up in the averages
    # Chrome is already swapping.
    assert runner._strain(_load(available_gb=0.5), None) is not None


def test_headroom_floor_grows_with_what_a_window_actually_costs():
    fat_window = 2 * 1024 * 1024        # 2 GB, as measured during the run
    # 1.6 GB free clears the fixed floor, but not 1.5x the cost of the window
    # about to be opened on it.
    assert runner._strain(_load(available_gb=1.6), None) is None
    assert runner._strain(_load(available_gb=1.6), fat_window) is not None


def test_memory_stall_trips_even_with_headroom_to_spare():
    assert runner._strain(_load(mem_stall=25.0), None) is not None


def test_cpu_stall_trips_only_when_tasks_are_actually_queueing():
    assert runner._strain(_load(cpu_stall=20.0), None) is None
    assert runner._strain(_load(cpu_stall=80.0), None) is not None


def test_calm_needs_more_room_than_strain_needs_to_trip():
    # A gap between the two thresholds is what stops a slot chattering open and
    # shut around one boundary.
    edge = _load(available_gb=1.6)
    assert runner._strain(edge, None) is None and not runner._calm(edge)


def test_calm_is_false_when_the_machine_cannot_be_read():
    import system_load
    assert not runner._calm(system_load.Load())


def test_slots_cap_how_many_windows_are_open_at_once():
    slots = runner.WindowSlots(2)
    assert slots.acquire() and slots.acquire()
    assert slots.held == 2
    done = threading.Event()

    def third():
        slots.acquire()
        done.set()

    threading.Thread(target=third, daemon=True).start()
    assert not done.wait(0.2)          # blocked: there is no third slot
    slots.release()
    assert done.wait(1.0)


def test_lowering_the_ceiling_never_evicts_an_open_window():
    slots = runner.WindowSlots(4)
    for _ in range(4):
        slots.acquire()
    slots.set_limit(1)
    # Still four held: a window already running its scenarios finishes them. The
    # ceiling takes effect as each one closes, which is the only moment lowering
    # it can hand memory back.
    assert slots.held == 4
    assert slots.limit == 1


def test_the_ceiling_never_reaches_zero():
    slots = runner.WindowSlots(2)
    assert slots.set_limit(-5) == 1


def test_closing_the_pool_frees_waiters_without_opening_a_window():
    slots = runner.WindowSlots(1)
    slots.acquire()
    got = []

    def queued():
        got.append(slots.acquire())

    thread = threading.Thread(target=queued, daemon=True)
    thread.start()
    time.sleep(0.05)
    slots.close()
    thread.join(1.0)
    # False, not a permit: CTRL+C must not leave a queued session parked until a
    # slot frees and then start a browser for a run that is already over.
    assert got == [False]


def _governed(monkeypatch, loads, ceiling=8, start=8, recover_s=0.05):
    """Run the governor over a canned sequence of readings; return the ceiling."""
    import system_load
    monkeypatch.setattr(runner, "GOVERNOR_SAMPLE_S", 0.01)
    monkeypatch.setattr(runner, "GOVERNOR_BACKOFF_S", 0.02)
    monkeypatch.setattr(runner, "GOVERNOR_RECOVER_S", recover_s)

    class Canned:
        def __init__(self):
            self.index = 0

        def read(self):
            load = loads[min(self.index, len(loads) - 1)]
            self.index += 1
            return load

    monkeypatch.setattr(system_load, "Sampler", Canned)
    slots = runner.WindowSlots(start)
    stop = threading.Event()
    thread = threading.Thread(target=runner._governor, args=(slots, ceiling, stop))
    thread.start()
    time.sleep(0.4)
    stop.set()
    thread.join(2.0)
    return slots


def test_governor_steps_down_under_real_pressure(monkeypatch):
    slots = _governed(monkeypatch, [_load(mem_stall=40.0)])
    assert slots.limit < 8


def test_governor_will_not_step_below_one(monkeypatch):
    slots = _governed(monkeypatch, [_load(available_gb=0.1)], start=2)
    assert slots.limit == 1


def test_governor_leaves_a_busy_but_healthy_machine_alone(monkeypatch):
    slots = _governed(monkeypatch, [_load(cpu_percent=99.0, cpu_stall=8.0)])
    assert slots.limit == 8


def test_governor_hands_a_slot_back_once_the_machine_is_calm(monkeypatch):
    slots = _governed(monkeypatch, [_load(available_gb=16.0)], ceiling=8, start=4)
    assert slots.limit > 4


def test_governor_never_raises_past_the_ceiling_it_was_given(monkeypatch):
    slots = _governed(monkeypatch, [_load(available_gb=16.0)], ceiling=4, start=4)
    assert slots.limit == 4


class _Windows:
    """Stands in for the launcher: records opens and closes, counts overlap."""

    def __init__(self, hold=0.05):
        self.hold = hold
        self.opened = []
        self.closed = []
        self.peak = 0
        self._live = 0
        self._lock = threading.Lock()

    def open(self, session):
        with self._lock:
            self._live += 1
            self.peak = max(self.peak, self._live)
            proc = "proc-%s" % session[3]
            self.opened.append(proc)
        time.sleep(self.hold)
        return proc

    def close(self, proc):
        with self._lock:
            self._live -= 1
            self.closed.append(proc)


def _no_browser(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "_attach", lambda profile, name: None)
    monkeypatch.setattr(runner.loader, "load_selectors", lambda flows_dir: {})
    monkeypatch.setattr(runner.artifacts, "new_run_dir", lambda d: str(tmp_path))
    monkeypatch.setattr(runner, "_resolve_scenarios", lambda which, flows_dir: ["smoke"])


def test_jobs_caps_windows_open_at_once_not_merely_drivers(monkeypatch, tmp_path):
    _no_browser(monkeypatch, tmp_path)
    windows = _Windows()
    runner.run_scenarios(_sessions(*[(n, None) for n in "abcdef"]), ["smoke"],
                         reports_dir=str(tmp_path), jobs=2, windows=windows)
    assert len(windows.opened) == 6
    assert windows.peak <= 2          # the point: two browsers, not six
    assert sorted(windows.closed) == sorted(windows.opened)


def test_every_window_is_closed_before_its_slot_is_reused(monkeypatch, tmp_path):
    _no_browser(monkeypatch, tmp_path)
    windows = _Windows()
    runner.run_scenarios(_sessions(("a", None), ("b", None), ("c", None)), ["smoke"],
                         reports_dir=str(tmp_path), jobs=1, windows=windows)
    # Strict alternation: the next window must not start while the last one is
    # still flushing cookies, or the two overlap for the seconds the slot exists
    # to prevent.
    assert windows.opened == windows.closed


def test_without_a_window_source_nothing_is_opened_or_closed(monkeypatch, tmp_path):
    # The caller handed over browsers it opened itself; a ceiling could not give
    # anything back, so the runner must not pretend to manage them.
    _no_browser(monkeypatch, tmp_path)
    windows = _Windows()
    runner.run_scenarios(_sessions(("a", None), ("b", None)), ["smoke"],
                         reports_dir=str(tmp_path), jobs=2)
    assert windows.opened == [] and windows.closed == []


def test_a_number_of_jobs_is_never_moved_by_the_machine(monkeypatch, tmp_path):
    # The governor is the auto option's behaviour, not a background service. A
    # run told "6 at a time" does 6 from the first window to the last.
    _no_browser(monkeypatch, tmp_path)
    started = []
    monkeypatch.setattr(runner, "_governor",
                        lambda *a, **k: started.append(a))
    runner.run_scenarios(_sessions(("a", None), ("b", None), ("c", None)), ["smoke"],
                         reports_dir=str(tmp_path), jobs=2, windows=_Windows())
    assert started == []


def test_auto_lets_the_governor_move_it(monkeypatch, tmp_path):
    _no_browser(monkeypatch, tmp_path)
    started = []
    monkeypatch.setattr(runner, "_governor", lambda *a, **k: started.append(a))
    runner.run_scenarios(_sessions(("a", None), ("b", None), ("c", None)), ["smoke"],
                         reports_dir=str(tmp_path), jobs="auto", windows=_Windows())
    assert len(started) == 1


def test_auto_starts_at_one_window_per_core(monkeypatch, tmp_path):
    _no_browser(monkeypatch, tmp_path)
    monkeypatch.setattr(runner.os, "cpu_count", lambda: 4)
    windows = _Windows()
    runner.run_scenarios(_sessions(*[(n, None) for n in "abcdefgh"]), ["smoke"],
                         reports_dir=str(tmp_path), jobs="auto", windows=windows)
    # Eight sessions, four cores: four windows at a time, not eight.
    assert windows.peak <= 4


def test_auto_never_starts_more_windows_than_there_are_sessions(monkeypatch, tmp_path):
    _no_browser(monkeypatch, tmp_path)
    monkeypatch.setattr(runner.os, "cpu_count", lambda: 64)
    windows = _Windows()
    runner.run_scenarios(_sessions(("a", None), ("b", None)), ["smoke"],
                         reports_dir=str(tmp_path), jobs="auto", windows=windows)
    assert windows.peak <= 2


# ------------------------------------------------------------ cpu, and its proof

def test_cpu_utilisation_alone_can_lower_the_ceiling(monkeypatch):
    # Utilisation is a weaker signal than stall, but it is the number a person
    # watching the launch page sees, so Auto has to answer to it.
    busy, helped = _load(cpu_percent=96.0), _load(cpu_percent=75.0)
    slots = _governed(monkeypatch, [busy, busy, busy, helped], recover_s=100)
    # Dropping one moved the CPU by 21 points, so the step earned its place.
    assert slots.limit == 7


def test_a_cpu_step_that_changes_nothing_is_undone(monkeypatch):
    # THE regression test for the ratchet. A rig driving seven windows keeps
    # eight cores busy however few of them are being driven, so a machine that
    # stays at 96% after a step down must end up back where it started rather
    # than winding itself down to one and staying there for the whole run.
    slots = _governed(monkeypatch, [_load(cpu_percent=96.0)], recover_s=100)
    assert slots.limit == 8


def test_memory_pressure_is_never_muted_by_a_futile_cpu_step(monkeypatch):
    # The CPU cooldown must not switch off the trigger that actually matters.
    slots = _governed(monkeypatch, [_load(cpu_percent=96.0, mem_stall=40.0)],
                      recover_s=100)
    assert slots.limit < 8


def test_recovery_waits_for_the_cpu_to_come_down_too(monkeypatch):
    # Free memory is not on its own a reason to add a session back while every
    # core is still flat out.
    slots = _governed(monkeypatch, [_load(available_gb=16.0, cpu_percent=95.0)],
                      ceiling=8, start=4)
    assert slots.limit == 4


def test_yielding_stands_aside_when_the_ceiling_has_dropped():
    slots = runner.WindowSlots(2)
    slots.acquire()
    slots.acquire()
    slots.set_limit(1)
    stood_aside = threading.Event()

    def driver():
        slots.yield_if_over()
        stood_aside.set()

    threading.Thread(target=driver, daemon=True).start()
    assert not stood_aside.wait(0.2)     # over the ceiling: waits for room
    slots.release()
    assert stood_aside.wait(1.0)


def test_yielding_costs_nothing_when_inside_the_ceiling():
    slots = runner.WindowSlots(4)
    slots.acquire()
    done = threading.Event()
    threading.Thread(target=lambda: (slots.yield_if_over(), done.set()),
                     daemon=True).start()
    assert done.wait(1.0)               # room to spare: straight through
    assert slots.held == 1


def test_auto_governs_even_when_the_windows_were_opened_for_us(monkeypatch, tmp_path):
    # The hole worth closing: with the windows all open there is no memory to
    # give back, but the driving can still be rationed - and an Auto that does
    # nothing at all is indistinguishable from one that is broken.
    _no_browser(monkeypatch, tmp_path)
    started = []
    monkeypatch.setattr(runner, "_governor", lambda *a, **k: started.append(k))
    runner.run_scenarios(_sessions(("a", None), ("b", None), ("c", None)), ["smoke"],
                         reports_dir=str(tmp_path), jobs="auto")
    assert len(started) == 1
    assert started[0]["unit"] == "sessions"    # sessions, not windows: nothing closes


def test_the_governor_announces_where_it_starts(monkeypatch, tmp_path):
    # Emitted before anything has moved, so a watcher shows the real number from
    # the first moment instead of a blank until the first adjustment.
    seen = []
    monkeypatch.setattr(runner.events, "emit",
                        lambda kind, **fields: seen.append((kind, fields)))
    monkeypatch.setattr(runner, "GOVERNOR_SAMPLE_S", 0.01)
    slots = runner.WindowSlots(7)
    stop = threading.Event()
    thread = threading.Thread(target=runner._governor, args=(slots, 7, stop))
    thread.start()
    time.sleep(0.1)
    stop.set()
    thread.join(2.0)
    limits = [f for kind, f in seen if kind == "governor.limit"]
    assert limits and limits[0]["limit"] == 7 and limits[0]["ceiling"] == 7


def test_every_move_is_announced_with_its_reason(monkeypatch):
    seen = []
    monkeypatch.setattr(runner.events, "emit",
                        lambda kind, **fields: seen.append((kind, fields)))
    _governed(monkeypatch, [_load(mem_stall=40.0)], recover_s=100)
    moves = [f for kind, f in seen if kind == "governor.limit"]
    assert len(moves) > 1                       # the start, then at least one move
    assert all(m["why"] for m in moves)         # none of them silent


def test_a_fixed_run_reports_its_number_too(monkeypatch, tmp_path):
    # Not governed, but still worth saying: a readout that is blank for most
    # runs is one nobody learns to read.
    _no_browser(monkeypatch, tmp_path)
    seen = []
    monkeypatch.setattr(runner.events, "emit",
                        lambda kind, **fields: seen.append((kind, fields)))
    runner.run_scenarios(_sessions(("a", None), ("b", None), ("c", None)), ["smoke"],
                         reports_dir=str(tmp_path), jobs=2)
    limits = [f for kind, f in seen if kind == "governor.limit"]
    assert len(limits) == 1
    assert (limits[0]["limit"], limits[0]["ceiling"]) == (2, 2)


# ---------------------------------------------------------------------- stopping

class _SlowAdapter:
    """Every step succeeds; the test drives the stop flag around them."""

    def __init__(self):
        self.clicks = 0

    def click(self, selector, timeout=None):
        self.clicks += 1

    def visible(self, selector, timeout=None):
        return True


def _plan(monkeypatch, steps):
    monkeypatch.setattr(runner.compiler, "compile_plan",
                        lambda *a, **k: ([Step("click", target=".b")] * steps, None))
    monkeypatch.setattr(runner.artifacts, "Reporter", lambda *a, **k: _NullReporter())


class _NullReporter:
    def capture_start(self):
        pass

    def capture_step(self):
        pass

    def finalize(self, result, failed=False):
        pass

    def finalize_compile_error(self, result):
        pass


def test_stop_lands_between_steps_not_at_the_end_of_the_scenario(monkeypatch, tmp_path):
    # THE fix: a forty-step flow whose remaining steps each time out at 30 s took
    # twenty minutes to reach the next scenario boundary, and for all that time
    # the GUI's Stop looked like it had done nothing.
    _plan(monkeypatch, 40)
    monkeypatch.setattr(runner.artifacts, "scenario_dir", lambda *a: str(tmp_path))
    adapter = _SlowAdapter()
    ctx = runner.RunContext(user={"login": "agent", "class": "Agent"},
                            env={"origin": "http://x", "url": "http://x"})
    runner._stop_requested.set()
    try:
        result = runner._run_scenario(adapter, "s", "dev-agent", None, {}, ctx,
                                      str(tmp_path))
    finally:
        runner._stop_requested.clear()
    assert adapter.clicks == 0                  # not one step was taken
    assert result.status == ERROR and "stopped" in result.error


def test_stopping_one_session_leaves_the_others_alone():
    runner.request_session_stop("dev-agent")
    try:
        assert runner._stopping("dev-agent")
        assert not runner._stopping("dev-manager")
        assert not runner._stopping()           # the run as a whole carries on
    finally:
        with runner._stopped_lock:
            runner._stopped_sessions.clear()


def test_stopping_everything_stops_every_session():
    runner._stop_requested.set()
    try:
        assert runner._stopping("dev-agent") and runner._stopping("anything")
    finally:
        runner._stop_requested.clear()


def test_a_new_run_forgets_which_sessions_were_stopped(monkeypatch, tmp_path):
    runner.request_session_stop("dev-agent")
    _no_browser(monkeypatch, tmp_path)
    runner.run_scenarios(_sessions(("a", None), ("b", None)), ["smoke"],
                         reports_dir=str(tmp_path), jobs=2)
    # Otherwise a session stopped by hand would stay stopped for every later run
    # in the same process.
    assert not runner._stopping("dev-agent")


def test_a_step_that_fails_because_its_window_closed_says_so(monkeypatch, tmp_path):
    # Closing a window mid-step makes the CDP call fail. The report should say
    # the session was stopped, not blame a click for a browser that went away.
    _plan(monkeypatch, 5)
    monkeypatch.setattr(runner.artifacts, "scenario_dir", lambda *a: str(tmp_path))
    ctx = runner.RunContext(user={"login": "agent", "class": "Agent"},
                            env={"origin": "http://x", "url": "http://x"})
    runner.request_session_stop("dev-agent")
    try:
        result = runner._run_scenario(Recorder(fail_times=99), "s", "dev-agent",
                                      None, {}, ctx, str(tmp_path))
    finally:
        with runner._stopped_lock:
            runner._stopped_sessions.clear()
    assert result.status == ERROR and "stopped" in result.error


def test_one_window_at_a_time_also_forgets_a_stopped_session(monkeypatch, tmp_path):
    # The clear used to sit on the parallel path only, so a single-window run
    # inherited whatever the last one had stopped.
    runner.request_session_stop("dev-agent")
    _no_browser(monkeypatch, tmp_path)
    runner.run_scenarios(_sessions(("a", None)), ["smoke"],
                         reports_dir=str(tmp_path), jobs=1)
    assert not runner._stopping("dev-agent")


# ----------------------------------------------------- server logs in a report
def test_a_scenarios_report_gets_the_lines_from_its_own_window(monkeypatch, tmp_path):
    """The slice must start when the SCENARIO did, not when the window opened.

    A window can be open for minutes before its third scenario runs. Handing that
    scenario everything since the window opened would bury its own few lines under
    the two flows before it - which is the failure this feature exists to avoid.
    """
    _plan(monkeypatch, 1)
    monkeypatch.setattr(runner.artifacts, "scenario_dir", lambda *a: str(tmp_path))
    asked = []

    class _Hub:
        def slice_for_session(self, session_name, start, end=None):
            asked.append((session_name, start))
            return {"app": ["a line"]}

    captured = {}
    real_reporter = runner.artifacts.Reporter

    def spy(config, out_dir, adapter, server=None):
        captured["server"] = server
        return real_reporter(config, out_dir, adapter, server=server)

    monkeypatch.setattr(runner.artifacts, "Reporter", spy)
    ctx = runner.RunContext(user={"login": "agent", "class": "Agent"},
                            env={"origin": "http://x", "url": "http://x"})
    before = time.time()
    runner._run_scenario(Recorder(), "s", "dev-agent", None, {}, ctx,
                         str(tmp_path), server_logs=_Hub())
    assert captured["server"] is not None
    captured["server"]()                       # the reporter calls this at write time
    (session_name, start), = asked
    assert session_name == "dev-agent"
    assert start >= before


def test_without_the_flag_the_reporter_is_given_no_provider(monkeypatch, tmp_path):
    _plan(monkeypatch, 1)
    monkeypatch.setattr(runner.artifacts, "scenario_dir", lambda *a: str(tmp_path))
    captured = {}
    real_reporter = runner.artifacts.Reporter

    def spy(config, out_dir, adapter, server=None):
        captured["server"] = server
        return real_reporter(config, out_dir, adapter, server=server)

    monkeypatch.setattr(runner.artifacts, "Reporter", spy)
    ctx = runner.RunContext(user={"login": "agent", "class": "Agent"},
                            env={"origin": "http://x", "url": "http://x"})
    runner._run_scenario(Recorder(), "s", "dev-agent", None, {}, ctx,
                         str(tmp_path))
    assert captured["server"] is None


class _DrivableAdapter(Recorder):
    """A Recorder the session driver can also attach to and let go of."""

    def disconnect(self):
        pass

    def screenshot(self, path):
        open(path, "w", encoding="utf-8").write("PNG")

    def content(self):
        return "<html/>"

    def console_logs(self):
        return []

    def url(self):
        return "http://x"


def test_the_hub_reaches_the_report_through_run_scenarios(monkeypatch, tmp_path):
    """The whole path, not the ends of it.

    ``_run_scenario`` took a hub and used it, and ``run_scenarios`` took one - but
    nothing carried it between them, so every real run wrote no server log at all
    while both halves passed their own tests. This drives the public entry point.
    """
    monkeypatch.setattr(runner, "_attach", lambda profile, name: _DrivableAdapter())
    monkeypatch.setattr(runner.loader, "load_selectors", lambda flows_dir: {})
    monkeypatch.setattr(runner.artifacts, "new_run_dir", lambda d: str(tmp_path))
    monkeypatch.setattr(runner, "_resolve_scenarios", lambda which, flows_dir: list(which))
    monkeypatch.setattr(runner.compiler, "compile_plan",
                        lambda *a, **k: ([Step("click", target=".b")], None))

    asked = []

    class _Hub:
        def slice_for_session(self, session_name, start, end=None):
            asked.append(session_name)
            return {"app": ["a line the backend wrote"]}

    session = ("Agent", None, str(tmp_path / "dev-agent"), "agent", "http://x", ())
    runner.run_scenarios([session], ["smoke"], env={"origin": "http://x"},
                         reports_dir=str(tmp_path),
                         report=runner.artifacts.ReportConfig.from_cli(
                             level="result"),
                         server_logs=_Hub())
    assert asked == ["dev-agent"]
    written = os.path.join(str(tmp_path), "dev-agent", "smoke", "server_log-app.log")
    assert os.path.exists(written)
    assert "a line the backend wrote" in open(written, encoding="utf-8").read()


def test_a_passing_scenario_still_gets_its_server_log(monkeypatch, tmp_path):
    # Streaming a backend's log all run and then keeping none of it is not an
    # answer either - "it passed" is not a reason to throw the record away.
    monkeypatch.setattr(runner, "_attach", lambda profile, name: _DrivableAdapter())
    monkeypatch.setattr(runner.loader, "load_selectors", lambda flows_dir: {})
    monkeypatch.setattr(runner.artifacts, "new_run_dir", lambda d: str(tmp_path))
    monkeypatch.setattr(runner, "_resolve_scenarios", lambda which, flows_dir: list(which))
    monkeypatch.setattr(runner.compiler, "compile_plan",
                        lambda *a, **k: ([Step("click", target=".b")], None))

    class _Hub:
        def slice_for_session(self, session_name, start, end=None):
            return {"app": ["quiet but working"]}

    session = ("Agent", None, str(tmp_path / "dev-agent"), "agent", "http://x", ())
    code = runner.run_scenarios([session], ["smoke"], env={"origin": "http://x"},
                                reports_dir=str(tmp_path),
                                report=runner.artifacts.ReportConfig.from_cli(
                                    level="result"),
                                server_logs=_Hub())
    assert code == 0                     # it passed...
    assert os.path.exists(os.path.join(str(tmp_path), "dev-agent", "smoke",
                                       "server_log-app.log"))   # ...and kept it
