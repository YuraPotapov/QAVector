"""cms_gui.services: starting, stopping and watching what a stack is made of.

Real processes, not doubles - the interesting behaviour is entirely in how
QProcess, a detached ``Popen`` and a polled probe differ from one another, and a
fake would only re-state the assumptions being tested. Everything spawned here is
a short ``python -c`` and is stopped before the test ends.

The three shapes under test:

* supervised and attached - ours, on a pipe, exit code and all;
* supervised and detached - ours, but told to outlive the window;
* managed - somebody else's, so its state has to be asked for.
"""

import os
import sys
import time

import pytest

from cms_gui import runnertypes, services
from cms_gui import servicesfile as sf

RUNNING = runnertypes.RUNNING
STOPPED = runnertypes.STOPPED
FAILED = runnertypes.FAILED


def _pump(qapp, predicate, timeout=15.0):
    """Turn the event loop until something becomes true, or give up."""
    deadline = time.time() + timeout
    while time.time() < deadline and not predicate():
        qapp.processEvents()
        time.sleep(0.02)
    qapp.processEvents()
    return predicate()


def _python(body):
    return "%s -u -c \"%s\"" % (sys.executable, body)


def _shell_row(name, body, detach=False):
    return sf.RunnerRow(name=name, type="shell", detach=detach,
                        settings={"command": _python(body)})


@pytest.fixture
def state(tmp_path):
    return services.DetachedState(str(tmp_path / "services-state.json"))


@pytest.fixture
def stopper():
    """Whatever a test started, stopped before the test is over."""
    started = []
    yield started
    for service in started:
        try:
            service.stop()
            service.wait_for_stop(5000)
            if service._pid:
                services.terminate_pid(service._pid)
        except Exception:
            pass
        service.dispose()


# ---------------------------------------------------------------------- slugs
def test_a_service_gets_a_filename_safe_name_of_its_own():
    assert services.slug("Claim", "Odoo Local") == "Claim-Odoo_Local"
    assert "/" not in services.slug("a/b", "c:d")
    assert services.slug("", "") == "project-service"


# -------------------------------------------------------------------- the tail
def test_the_tail_reads_only_what_is_new(tmp_path):
    path = tmp_path / "s.log"
    path.write_text("one\ntwo\n")
    tail = services.LogTail(str(path))
    assert tail.read() == ["one", "two"]
    assert tail.read() == []
    path.write_text("one\ntwo\nthree\n")
    assert tail.read() == ["three"]


def test_a_half_written_line_waits_for_the_rest_of_itself(tmp_path):
    path = tmp_path / "s.log"
    path.write_text("comp")
    tail = services.LogTail(str(path))
    assert tail.read() == []
    with open(path, "a") as handle:
        handle.write("lete\n")
    assert tail.read() == ["complete"]


def test_a_file_that_shrank_is_read_again_from_its_top(tmp_path):
    # Rotated or truncated underneath us. Seeking past the end forever is how a
    # console goes silent and stays silent.
    path = tmp_path / "s.log"
    path.write_text("old and long\n")
    tail = services.LogTail(str(path))
    tail.read()
    path.write_text("new\n")
    assert tail.read() == ["new"]


def test_seek_end_ignores_what_a_previous_run_left(tmp_path):
    path = tmp_path / "s.log"
    path.write_text("from last time\n")
    tail = services.LogTail(str(path))
    tail.seek_end()
    with open(path, "a") as handle:
        handle.write("from this one\n")
    assert tail.read() == ["from this one"]


def test_a_missing_file_tails_as_nothing(tmp_path):
    assert services.LogTail(str(tmp_path / "gone.log")).read() == []


# ------------------------------------------------------------------ rotation
def test_an_oversized_log_is_moved_aside_keeping_one(tmp_path):
    path = tmp_path / "s.log"
    path.write_text("x" * 50)
    assert services.rotate(str(path), limit=10) is True
    assert (tmp_path / "s.log.1").read_text() == "x" * 50
    assert not path.exists()


def test_a_small_log_is_left_alone(tmp_path):
    path = tmp_path / "s.log"
    path.write_text("x")
    assert services.rotate(str(path), limit=10) is False
    assert path.exists()


# ---------------------------------------------------------------- pid liveness
def test_our_own_process_reads_as_alive():
    assert services.pid_alive(os.getpid()) is True


def test_nothing_and_nonsense_read_as_not_alive():
    assert services.pid_alive(0) is False
    assert services.pid_alive(None) is False
    assert services.pid_alive("not a pid") is False


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="needs /proc")
def test_a_reused_pid_is_not_mistaken_for_our_service():
    # The pid is alive, but it is not running what we started. Reporting somebody
    # else's shell as a running Odoo is worse than reporting nothing.
    assert services.pid_alive(os.getpid(), "definitely-not-this-program") is False


# ------------------------------------------------------ supervised + attached
def test_an_attached_service_runs_reports_and_prints(qapp, tmp_path, state, stopper):
    row = _shell_row("tick", "print('hello');import time;time.sleep(30)")
    service = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    stopper.append(service)
    seen = []
    service.status_changed.connect(seen.append)

    assert service.start() is True
    assert _pump(qapp, lambda: service.status == RUNNING)
    assert _pump(qapp, lambda: service.console() == ["hello"])
    assert seen[:2] == [runnertypes.STARTING, RUNNING]
    assert service.detained() is True        # ours, and not allowed to detach


def test_stopping_an_attached_service_is_not_reported_as_a_failure(
        qapp, tmp_path, state, stopper):
    # It is signalled, so it exits non-zero. That is the stop working, not the
    # service falling over.
    row = _shell_row("tick", "import time;time.sleep(30)")
    service = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    stopper.append(service)
    service.start()
    _pump(qapp, lambda: service.status == RUNNING)
    service.stop()
    assert _pump(qapp, lambda: service.status == STOPPED)


def test_a_service_that_falls_over_says_so_with_its_exit_code(
        qapp, tmp_path, state, stopper):
    row = _shell_row("bad", "import sys;sys.exit(3)")
    service = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    stopper.append(service)
    service.start()
    assert _pump(qapp, lambda: service.status == FAILED)
    assert "code 3" in service.detail


def test_a_program_that_is_not_there_names_itself(qapp, tmp_path, state, stopper):
    row = sf.RunnerRow(name="nope", type="shell",
                       settings={"command": "definitely-not-installed-anywhere"})
    service = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    stopper.append(service)
    service.start()
    assert _pump(qapp, lambda: service.status == FAILED)
    assert "definitely-not-installed-anywhere" in service.detail


def test_a_working_directory_that_is_not_there_is_caught_before_spawning(
        qapp, tmp_path, state):
    row = _shell_row("x", "pass")
    row.settings["dir"] = str(tmp_path / "no-such-place")
    service = services.ServiceProcess("Claim", row, "", state=state)
    assert service.start() is False
    assert service.status == FAILED and "does not exist" in service.detail


def test_a_service_prints_into_a_file_of_its_own(qapp, tmp_path, state, stopper):
    row = _shell_row("writer", "print('on disk');import time;time.sleep(30)")
    service = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    stopper.append(service)
    service.start()
    assert _pump(qapp, lambda: service.console() == ["on disk"])
    # The console keeps 2000 lines; the file keeps the rest, and outlives the
    # window that was watching.
    assert _pump(qapp, lambda: "on disk" in _read(service.log_path))


def _read(path):
    try:
        with open(path) as handle:
            return handle.read()
    except OSError:
        return ""


def test_an_edit_while_it_runs_is_flagged_rather_than_applied(
        qapp, tmp_path, state, stopper):
    row = _shell_row("tick", "import time;time.sleep(30)")
    service = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    stopper.append(service)
    service.start()
    _pump(qapp, lambda: service.status == RUNNING)
    service.update(_shell_row("tick", "import time;time.sleep(1)"), str(tmp_path))
    assert service.stale is True
    assert service.status == RUNNING          # what is up is still what was started


# ------------------------------------------------------ supervised + detached
def test_a_detached_service_is_remembered_so_it_can_be_found_again(
        qapp, tmp_path, state, stopper):
    row = _shell_row("det", "print('alive');import time;time.sleep(30)", detach=True)
    service = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    stopper.append(service)
    service.start()
    assert service.status == RUNNING and service._pid
    assert state.get(service.slug, "Claim", "det")["pid"] == service._pid
    # Not ours to lose on the way out - which is the whole point of the flag.
    assert service.detained() is False


def test_a_detached_services_output_is_read_off_its_file(
        qapp, tmp_path, state, stopper):
    row = _shell_row("det", "print('from the file');import time;time.sleep(30)",
                     detach=True)
    service = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    stopper.append(service)
    service.start()
    assert _pump(qapp, lambda: (service.poll(), service.console())[1]
                 == ["from the file"])


def test_a_new_window_picks_up_the_service_the_last_one_left_running(
        qapp, tmp_path, state, stopper):
    row = _shell_row("det", "import time;time.sleep(30)", detach=True)
    first = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    stopper.append(first)
    first.start()

    again = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    again.reattach()
    assert again.status == RUNNING
    assert again._pid == first._pid
    assert "before this window was opened" in again.detail


def test_a_pid_that_died_while_nobody_watched_is_forgotten(qapp, tmp_path, state):
    row = _shell_row("gone", "pass", detach=True)
    service = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    service.start()
    assert _pump(qapp, lambda: not services.pid_alive(service._pid, service._marker))
    service.poll()
    assert service.status == STOPPED
    assert state.get(service.slug, "Claim", "gone") is None


def test_stopping_a_detached_service_ends_it(qapp, tmp_path, state, stopper):
    row = _shell_row("det", "import time;time.sleep(30)", detach=True)
    service = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    stopper.append(service)
    service.start()
    pid = service._pid
    service.stop()
    assert _pump(qapp, lambda: (service.poll(), service.status)[1] == STOPPED, 20.0)
    assert not services.pid_alive(pid, service._marker)


# ------------------------------------------------------------------- managed
class _Managed(runnertypes.RunnerType):
    """A container, near enough: a flag file the start and stop commands move.

    A managed service is defined by its command exiting immediately and its state
    living somewhere else - which a file models exactly, and without docker.
    """

    id = "test-managed"
    label = "Test Managed"
    mode = runnertypes.MANAGED
    detach_choice = False
    detach_default = True
    detach_note = "the daemon's"
    fields = ()

    def __init__(self, flag):
        self.flag = flag

    def command(self, settings, cwd):
        return [sys.executable, "-c", "open(%r, 'w').write('1')" % self.flag]

    def stop_command(self, settings):
        return [sys.executable, "-c",
                "import os;os.path.exists(%r) and os.remove(%r)"
                % (self.flag, self.flag)]

    def probe_command(self, settings):
        return [sys.executable, "-c",
                "import os,sys;sys.stdout.write('up' if os.path.exists(%r) else 'down')"
                % self.flag]

    def probe_reads(self, stdout, code):
        return RUNNING if stdout.strip() == "up" else STOPPED


@pytest.fixture
def managed(tmp_path, monkeypatch):
    runner = _Managed(str(tmp_path / "flag"))
    monkeypatch.setitem(runnertypes.BY_ID, runner.id, runner)
    return runner


def test_a_managed_service_is_asked_rather_than_assumed(
        qapp, tmp_path, state, managed, stopper):
    row = sf.RunnerRow(name="pg", type=managed.id)
    assert row.detach is True              # forced by the kind, not by anyone
    service = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    stopper.append(service)
    assert service.is_managed() and service.needs_poll()

    service.start()
    assert _pump(qapp, lambda: service.status == RUNNING)
    assert os.path.exists(managed.flag)

    service.stop()
    assert _pump(qapp, lambda: service.status == STOPPED)
    assert not os.path.exists(managed.flag)


def test_a_managed_service_already_up_is_found_on_load(
        qapp, tmp_path, state, managed, stopper):
    open(managed.flag, "w").write("1")
    service = services.ServiceProcess(
        "Claim", sf.RunnerRow(name="pg", type=managed.id), str(tmp_path), state=state)
    stopper.append(service)
    service.reattach()
    assert _pump(qapp, lambda: service.status == RUNNING)


def test_a_managed_service_is_never_detained(qapp, tmp_path, state, managed):
    service = services.ServiceProcess(
        "Claim", sf.RunnerRow(name="pg", type=managed.id), str(tmp_path), state=state)
    service._set_status(RUNNING)
    assert service.detained() is False


def test_a_failing_start_command_reports_what_it_printed(
        qapp, tmp_path, state, monkeypatch, stopper):
    class Broken(_Managed):
        id = "test-broken"
        def command(self, settings, cwd):
            return [sys.executable, "-c",
                    "import sys;print('no such container');sys.exit(1)"]
    broken = Broken(str(tmp_path / "flag"))
    monkeypatch.setitem(runnertypes.BY_ID, broken.id, broken)
    service = services.ServiceProcess(
        "Claim", sf.RunnerRow(name="pg", type=broken.id), str(tmp_path), state=state)
    stopper.append(service)
    service.start()
    assert _pump(qapp, lambda: service.status == FAILED)
    assert "no such container" in service.detail


# ---------------------------------------------------------------- unknown type
def test_a_type_this_build_does_not_have_cannot_be_started(qapp, tmp_path, state):
    service = services.ServiceProcess(
        "Claim", sf.RunnerRow(name="x", type="quantum"), str(tmp_path), state=state)
    assert service.start() is False
    assert service.status == FAILED and "Unknown runner type" in service.detail


# ------------------------------------------------------------- the supervisor
def _stack(*runners):
    return [sf.ProjectRow(name="Claim", dir="", runners=list(runners))]


@pytest.fixture
def supervisor(qapp, state):
    sup = services.ServiceSupervisor(state=state)
    yield sup
    sup.shutdown(4000)


def test_syncing_builds_one_service_per_configured_runner(supervisor):
    supervisor.sync(_stack(_shell_row("a", "pass"), _shell_row("b", "pass")))
    assert supervisor.service("Claim", "a") is not None
    assert supervisor.counts("Claim") == (0, 2)


def test_a_re_sync_keeps_the_service_that_is_already_there(supervisor):
    rows = _stack(_shell_row("a", "pass"))
    supervisor.sync(rows)
    first = supervisor.service("Claim", "a")
    supervisor.sync(rows)
    assert supervisor.service("Claim", "a") is first


def test_a_runner_removed_from_the_configuration_goes_with_it(supervisor):
    supervisor.sync(_stack(_shell_row("a", "pass")))
    supervisor.sync([sf.ProjectRow(name="Claim")])
    assert supervisor.service("Claim", "a") is None


def test_a_runner_deleted_while_attached_and_running_is_stopped(qapp, supervisor):
    supervisor.sync(_stack(_shell_row("a", "import time;time.sleep(30)")))
    service = supervisor.service("Claim", "a")
    supervisor.start("Claim", "a")
    assert _pump(qapp, lambda: service.status == RUNNING)
    pid = int(service._proc.processId())
    # Nothing would be able to stop it once its row is gone.
    supervisor.sync([sf.ProjectRow(name="Claim")])
    assert not services.pid_alive(pid)


def test_the_running_tally_is_what_the_block_header_says(qapp, supervisor):
    supervisor.sync(_stack(_shell_row("a", "import time;time.sleep(30)"),
                           _shell_row("b", "import time;time.sleep(30)")))
    supervisor.start("Claim", "a")
    assert _pump(qapp, lambda: supervisor.counts("Claim") == (1, 2))


def test_starting_a_stack_starts_everything_in_it(qapp, supervisor):
    supervisor.sync(_stack(_shell_row("a", "import time;time.sleep(30)"),
                           _shell_row("b", "import time;time.sleep(30)")))
    supervisor.start_all("Claim")
    assert _pump(qapp, lambda: supervisor.counts("Claim") == (2, 2))
    supervisor.stop_all("Claim")
    assert _pump(qapp, lambda: supervisor.counts("Claim") == (0, 2))


def test_naming_services_acts_on_those_and_leaves_the_rest(qapp, supervisor):
    # What the page's selection means: Start acts on the rows that were picked.
    supervisor.sync(_stack(_shell_row("a", "import time;time.sleep(30)"),
                           _shell_row("b", "import time;time.sleep(30)")))
    supervisor.start_all("Claim", ["b"])
    assert _pump(qapp, lambda: supervisor.status("Claim", "b") == RUNNING)
    assert supervisor.status("Claim", "a") == STOPPED
    supervisor.stop_all("Claim", ["b"])
    assert _pump(qapp, lambda: supervisor.counts("Claim") == (0, 2))


def test_a_named_service_still_brings_up_what_it_waits_for(qapp, supervisor):
    # Starting one of three selected services without its database would only
    # look like it had failed.
    rows = _stack(_shell_row("db", "import time;time.sleep(30)"),
                  _shell_row("app", "import time;time.sleep(30)"))
    rows[0].runners[1].depends = ["db"]
    supervisor.sync(rows)
    supervisor.start_all("Claim", ["app"])
    assert _pump(qapp, lambda: supervisor.counts("Claim") == (2, 2))


def test_a_subset_is_still_started_in_the_projects_own_order(supervisor):
    supervisor.sync(_stack(_shell_row("a", "pass"), _shell_row("b", "pass"),
                           _shell_row("c", "pass")))
    ordered = [key[1] for key, _service in supervisor._ordered("Claim",
                                                               ["c", "a"])]
    assert ordered == ["a", "c"]


def test_only_what_cannot_detach_is_counted_as_lost_on_close(qapp, supervisor):
    supervisor.sync(_stack(
        _shell_row("mine", "import time;time.sleep(30)"),
        _shell_row("theirs", "import time;time.sleep(30)", detach=True)))
    supervisor.start_all("Claim")
    assert _pump(qapp, lambda: supervisor.counts("Claim") == (2, 2))
    assert [s.name for s in supervisor.detained()] == ["mine"]


def test_shutdown_stops_what_it_must_and_leaves_the_rest(qapp, supervisor):
    supervisor.sync(_stack(
        _shell_row("mine", "import time;time.sleep(30)"),
        _shell_row("theirs", "import time;time.sleep(30)", detach=True)))
    supervisor.start_all("Claim")
    assert _pump(qapp, lambda: supervisor.counts("Claim") == (2, 2))
    detached_pid = supervisor.service("Claim", "theirs")._pid

    assert supervisor.shutdown(8000) == 1
    assert supervisor.service("Claim", "mine").status != RUNNING
    assert services.pid_alive(detached_pid)
    services.terminate_pid(detached_pid)


def test_the_page_hears_about_every_change_of_status(qapp, supervisor):
    heard = []
    supervisor.status_changed.connect(
        lambda project, name, status: heard.append((project, name, status)))
    supervisor.sync(_stack(_shell_row("a", "import time;time.sleep(30)")))
    supervisor.start("Claim", "a")
    assert _pump(qapp, lambda: ("Claim", "a", RUNNING) in heard)


def test_output_is_relayed_with_the_service_it_came_from(qapp, supervisor):
    heard = []
    supervisor.output.connect(
        lambda project, name, line: heard.append((project, name, line)))
    supervisor.sync(_stack(_shell_row("a", "print('mine');import time;time.sleep(30)")))
    supervisor.start("Claim", "a")
    assert _pump(qapp, lambda: ("Claim", "a", "mine") in heard)


def test_a_failure_keeps_its_reason_when_the_probe_next_answers(
        qapp, tmp_path, state, monkeypatch, stopper):
    """The reason a container would not start must outlive the next poll.

    A probe of a container that failed to start says "stopped" - true, and
    useless. Three seconds after `docker start` explained itself, the row would
    otherwise have replaced that with nothing at all.
    """
    class Broken(_Managed):
        id = "test-broken-probe"
        def command(self, settings, cwd):
            return [sys.executable, "-c", "import sys;print('boom');sys.exit(1)"]
    broken = Broken(str(tmp_path / "flag"))
    monkeypatch.setitem(runnertypes.BY_ID, broken.id, broken)
    service = services.ServiceProcess(
        "Claim", sf.RunnerRow(name="pg", type=broken.id), str(tmp_path), state=state)
    stopper.append(service)
    service.start()
    assert _pump(qapp, lambda: service.status == FAILED)
    reason = service.detail

    service.poll(force=True)
    assert _pump(qapp, lambda: service._probe is None)
    assert service.status == FAILED and service.detail == reason


def test_restarting_a_managed_service_brings_it_back(
        qapp, tmp_path, state, managed, stopper):
    service = services.ServiceProcess(
        "Claim", sf.RunnerRow(name="pg", type=managed.id), str(tmp_path), state=state)
    stopper.append(service)
    service.start()
    assert _pump(qapp, lambda: service.status == RUNNING)
    service.restart()
    assert _pump(qapp, lambda: not os.path.exists(managed.flag), 20.0)
    assert _pump(qapp, lambda: service.status == RUNNING, 20.0)


def test_restarting_a_detached_service_brings_it_back(qapp, tmp_path, state, stopper):
    # The only place that ever learns a detached service is down is the poll, so
    # without honouring the restart there it stops and stays stopped.
    row = _shell_row("det", "import time;time.sleep(30)", detach=True)
    service = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    stopper.append(service)
    service.start()
    first = service._pid
    service.restart()
    assert _pump(qapp, lambda: (service.poll(), service._pid)[1] not in (0, first),
                 20.0)
    assert service.status == RUNNING


def test_a_status_check_that_cannot_run_names_what_is_missing(
        qapp, tmp_path, state, monkeypatch, stopper):
    class NoProbe(_Managed):
        id = "test-no-probe"
        def probe_command(self, settings):
            return ["definitely-not-installed-anywhere", "ps"]
    runner = NoProbe(str(tmp_path / "flag"))
    monkeypatch.setitem(runnertypes.BY_ID, runner.id, runner)
    service = services.ServiceProcess(
        "Claim", sf.RunnerRow(name="pg", type=runner.id), str(tmp_path), state=state)
    stopper.append(service)
    service.poll(force=True)
    assert _pump(qapp, lambda: service.status == FAILED)
    assert "definitely-not-installed-anywhere" in service.detail


# --------------------------------------------------------------- dependencies
def _project(*runners):
    return [sf.ProjectRow(name="Claim", dir="", runners=list(runners))]


def _waits(name, body, on, detach=False):
    row = _shell_row(name, body, detach)
    row.depends = list(on)
    return row


def test_a_service_waits_for_what_it_depends_on(qapp, supervisor):
    slow = "import time;time.sleep(0.4);print('up');time.sleep(30)"
    supervisor.sync(_project(
        _waits("web", "import time;time.sleep(30)", ["db"]),
        _shell_row("db", slow)))
    supervisor.start("Claim", "web")

    web = supervisor.service("Claim", "web")
    assert web.status == runnertypes.WAITING
    assert "waiting for db" in web.detail
    # And the thing it waits for was started for it, unasked.
    assert supervisor.service("Claim", "db").is_running()
    assert _pump(qapp, lambda: web.status == RUNNING)


def test_a_waiting_service_counts_as_on_its_way_not_as_stopped(supervisor):
    supervisor.sync(_project(
        _waits("web", "import time;time.sleep(30)", ["db"]),
        _shell_row("db", "import time;time.sleep(30)")))
    supervisor.start("Claim", "web")
    web = supervisor.service("Claim", "web")
    # Start must not be offered twice, and Stop must be offered once.
    assert web.is_running() and web.is_waiting()
    # The supervisor is what refuses a second Start. ServiceProcess.start is not:
    # starting out of WAITING is exactly what releasing a wait does.
    assert supervisor.start("Claim", "web") is False


def test_giving_up_the_wait_is_the_whole_stop(supervisor):
    supervisor.sync(_project(
        _waits("web", "import time;time.sleep(30)", ["db"]),
        _shell_row("db", "import time;time.sleep(30)")))
    supervisor.start("Claim", "web")
    supervisor.stop("Claim", "web")
    assert supervisor.service("Claim", "web").status == STOPPED


def test_a_dependency_that_cannot_start_takes_the_waiter_down_with_it(
        qapp, supervisor):
    # Otherwise the row sits on "Waiting…" forever with nothing to explain it.
    dead = sf.RunnerRow(name="db", type="shell",
                        settings={"command": "definitely-not-installed-anywhere"})
    supervisor.sync(_project(
        _waits("web", "import time;time.sleep(30)", ["db"]), dead))
    supervisor.start("Claim", "web")
    web = supervisor.service("Claim", "web")
    assert _pump(qapp, lambda: web.status == STOPPED)
    assert "db did not start" in web.detail


def test_starting_a_project_starts_what_has_to_be_up_first_first(qapp, supervisor):
    # Listed web-then-db on purpose: the file's order is what was typed, not an
    # instruction about what to run when.
    supervisor.sync(_project(
        _waits("web", "import time;time.sleep(30)", ["db"]),
        _shell_row("db", "import time;time.sleep(0.3);print('up');time.sleep(30)")))
    supervisor.start_all("Claim")
    assert supervisor.service("Claim", "web").status == runnertypes.WAITING
    assert _pump(qapp, lambda: supervisor.counts("Claim") == (2, 2))


def test_a_service_that_waits_for_nothing_starts_straight_away(qapp, supervisor):
    supervisor.sync(_project(_shell_row("solo", "import time;time.sleep(30)")))
    supervisor.start("Claim", "solo")
    assert supervisor.service("Claim", "solo").status != runnertypes.WAITING
    assert _pump(qapp, lambda: supervisor.counts("Claim") == (1, 1))


def test_a_ring_in_a_hand_edited_file_does_not_take_the_window_with_it(supervisor):
    # validate() refuses this, but it is reachable before anyone presses Save.
    supervisor.sync(_project(
        _waits("a", "import time;time.sleep(30)", ["b"]),
        _waits("b", "import time;time.sleep(30)", ["a"])))
    supervisor.start("Claim", "a")          # must return rather than recurse
    assert supervisor.service("Claim", "a").is_waiting()


def test_restarting_goes_back_through_the_waiting(qapp, supervisor):
    supervisor.sync(_project(
        _waits("web", "import time;time.sleep(30)", ["db"]),
        _shell_row("db", "import time;time.sleep(30)")))
    supervisor.start_all("Claim")
    assert _pump(qapp, lambda: supervisor.counts("Claim") == (2, 2))
    supervisor.restart("Claim", "web")
    assert _pump(qapp, lambda: supervisor.service("Claim", "web").status == RUNNING,
                 20.0)


# ------------------------------------------------------------------ criteria
# A criterion says what the service's *log* says. It never moves the status,
# which goes on meaning what the process is doing.
from cms_gui import criteria as cr                                  # noqa: E402


def _watching(name, body, criteria, detach=False):
    row = _shell_row(name, body, detach)
    row.criteria = list(criteria)
    return row


def _lit(service, name):
    return {c[0]: c[2] for c in service.criteria_state()}.get(name)


def test_a_criterion_lights_from_the_services_own_output(qapp, tmp_path, state,
                                                         stopper):
    criterion = cr.CriterionRow(name="start", rules=[
        cr.Rule(cr.MATCH, cr.TEXT, "ready to serve")])
    row = _watching("svc", "print('ready to serve');import time;time.sleep(30)",
                    [criterion])
    service = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    stopper.append(service)
    heard = []
    service.criteria_changed.connect(lambda: heard.append(True))

    assert _lit(service, "start") is False
    service.start()
    assert _pump(qapp, lambda: _lit(service, "start") is True)
    assert heard, "the page is told, rather than having to poll for it"
    # And the process itself was never spoken for.
    assert service.status == RUNNING


def test_a_criterion_can_read_a_file_instead(qapp, tmp_path, state, stopper):
    # What any backend started with a logfile needs: it prints almost nothing to
    # its own console, so watching that would wait forever.
    log = tmp_path / "odoo.log"
    log.write_text("from some earlier run\n")
    criterion = cr.CriterionRow(name="start", source=str(log), rules=[
        cr.Rule(cr.MATCH, cr.TEXT, "started localhost:8069"),
        cr.Rule(cr.EXCLUDE, cr.REGEX, "ERRORS|CRITICAL")])
    row = _watching("odoo", "import time;time.sleep(30)", [criterion])
    service = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    stopper.append(service)

    assert service.needs_poll(), "nothing else would ever go and read that file"
    service.start()
    assert _pump(qapp, lambda: service.status == RUNNING)
    with open(log, "a") as handle:
        handle.write("INFO odoo: started localhost:8069\n")
    service.poll()
    assert _lit(service, "start") is True

    with open(log, "a") as handle:
        handle.write("CRITICAL the database is gone\n")
    service.poll()
    assert _lit(service, "start") is False
    assert service.status == RUNNING       # the log is bad; the process is fine


def test_what_the_file_already_held_belongs_to_the_last_run(qapp, tmp_path, state,
                                                            stopper):
    log = tmp_path / "odoo.log"
    log.write_text("started localhost:8069\n")     # from the run before this one
    criterion = cr.CriterionRow(name="start", source=str(log), rules=[
        cr.Rule(cr.MATCH, cr.TEXT, "started localhost:8069")])
    row = _watching("odoo", "import time;time.sleep(30)", [criterion])
    service = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    stopper.append(service)
    service.start()
    service.poll()
    assert _lit(service, "start") is False


def test_a_detached_services_output_reaches_its_criteria(qapp, tmp_path, state,
                                                         stopper):
    """The regression this feature would otherwise have shipped with.

    A detached service's lines were appended and emitted *beside* _emit_line
    rather than through it, so nothing watching output would have seen one.
    """
    criterion = cr.CriterionRow(name="start", rules=[
        cr.Rule(cr.MATCH, cr.TEXT, "ready to serve")])
    row = _watching("det", "print('ready to serve');import time;time.sleep(30)",
                    [criterion], detach=True)
    service = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    stopper.append(service)
    service.start()
    assert _pump(qapp, lambda: (service.poll(), _lit(service, "start"))[1] is True)


def test_an_adopted_run_answers_for_what_it_printed_before_the_window(
        qapp, tmp_path, state, stopper):
    """A service that was up before this window still has to light its tags.

    Its ready line was written once, before anything here was watching, and it
    will not be written again for as long as the process lives - so a criterion
    that only ever sees what arrives next stays grey forever, and a scenario's
    wait_for_criterion waits out its whole timeout beside a log that plainly
    says otherwise.
    """
    criterion = cr.CriterionRow(name="start", rules=[
        cr.Rule(cr.MATCH, cr.TEXT, "ready to serve")])
    row = _watching("adopted", "print('ready to serve');import time;time.sleep(30)",
                    [criterion], detach=True)
    first = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    stopper.append(first)
    first.start()
    assert _pump(qapp, lambda: (first.poll(), _lit(first, "start"))[1] is True)

    again = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    again.reattach()
    assert again.status == RUNNING
    assert _lit(again, "start") is True


def test_an_adopted_run_is_read_from_where_it_began_and_not_from_the_top(
        qapp, tmp_path, state, stopper):
    # The log is appended to across runs, so its top is the boot before this one.
    # Reading from there would light a criterion from a process that is gone.
    criterion = cr.CriterionRow(name="start", rules=[
        cr.Rule(cr.MATCH, cr.TEXT, "ready to serve")])
    said_it = _watching("adopted2", "print('ready to serve')", [criterion],
                        detach=True)
    quiet = _watching("adopted2", "import time;time.sleep(30)", [criterion],
                      detach=True)

    first = services.ServiceProcess("Claim", said_it, str(tmp_path), state=state)
    first.start()
    assert _pump(qapp, lambda: not services.pid_alive(first._pid, first._marker))

    second = services.ServiceProcess("Claim", quiet, str(tmp_path), state=state)
    stopper.append(second)
    second.start()                      # the same log, a new run, saying nothing

    again = services.ServiceProcess("Claim", quiet, str(tmp_path), state=state)
    again.reattach()
    assert again.status == RUNNING
    assert _lit(again, "start") is False


def test_an_entry_from_a_build_that_recorded_no_offset_claims_nothing(
        qapp, tmp_path, state, stopper):
    # Upgrading with a detached service already up: there is nothing to read
    # *from*, so it is adopted as before rather than answered for from the top.
    criterion = cr.CriterionRow(name="start", rules=[
        cr.Rule(cr.MATCH, cr.TEXT, "ready to serve")])
    row = _watching("adopted3", "print('ready to serve');import time;time.sleep(30)",
                    [criterion], detach=True)
    first = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    stopper.append(first)
    first.start()
    assert _pump(qapp, lambda: (first.poll(), _lit(first, "start"))[1] is True)
    state.remember(first.slug, "Claim", "adopted3", first._pid, first._marker)

    again = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    again.reattach()
    assert again.status == RUNNING
    assert _lit(again, "start") is False


def test_stopping_keeps_what_the_run_lit(qapp, tmp_path, state, stopper):
    """Stopping is when what the log said matters most.

    A service whose whole job was one run has finished by the time anybody looks
    at it, so clearing on stop would throw the answer away at exactly the moment
    it is wanted. The tags describe the last run until the next one starts.
    """
    criterion = cr.CriterionRow(name="start", rules=[
        cr.Rule(cr.MATCH, cr.TEXT, "ready to serve")])
    row = _watching("svc", "print('ready to serve');import time;time.sleep(30)",
                    [criterion])
    service = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    stopper.append(service)
    service.start()
    assert _pump(qapp, lambda: _lit(service, "start") is True)
    service.stop()
    assert _pump(qapp, lambda: service.status == STOPPED)
    assert _lit(service, "start") is True


def test_a_service_that_fell_over_keeps_what_it_lit(qapp, tmp_path, state, stopper):
    # How far it got before it died is the useful half of a crash: this one
    # reached "ready to serve" and then exited, which is a different fault from
    # one that never started at all.
    criterion = cr.CriterionRow(name="start", rules=[
        cr.Rule(cr.MATCH, cr.TEXT, "ready to serve")])
    row = _watching("svc", "print('ready to serve');import sys;sys.exit(3)",
                    [criterion])
    service = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    stopper.append(service)
    service.start()
    assert _pump(qapp, lambda: service.status == FAILED)
    assert _lit(service, "start") is True


def test_the_page_is_told_when_they_clear(qapp, tmp_path, state, stopper):
    # Clearing happens at start, so that is when the row has to be repainted -
    # otherwise the previous run's tags sit there through the whole of the next.
    criterion = cr.CriterionRow(name="start", rules=[
        cr.Rule(cr.MATCH, cr.TEXT, "ready to serve")])
    row = _watching("svc", "print('ready to serve');import time;time.sleep(30)",
                    [criterion])
    service = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    stopper.append(service)
    service.start()
    assert _pump(qapp, lambda: _lit(service, "start") is True)
    service.stop()
    assert _pump(qapp, lambda: service.status == STOPPED)

    heard = []
    service.criteria_changed.connect(lambda: heard.append(service.criteria_state()))
    service.start()
    assert heard and heard[0][0][2] is False


def test_restarting_clears_what_the_last_run_lit(qapp, tmp_path, state, stopper):
    criterion = cr.CriterionRow(name="start", rules=[
        cr.Rule(cr.MATCH, cr.TEXT, "ready to serve")])
    row = _watching("svc", "print('ready to serve');import time;time.sleep(30)",
                    [criterion])
    service = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    stopper.append(service)
    service.start()
    assert _pump(qapp, lambda: _lit(service, "start") is True)
    service.stop()
    assert _pump(qapp, lambda: service.status == STOPPED)
    service.start()
    # Cleared the moment it starts - it describes this run, not the last.
    assert _lit(service, "start") is False
    assert _pump(qapp, lambda: _lit(service, "start") is True)


def test_a_criterion_added_to_a_running_service_answers_at_once(qapp, tmp_path,
                                                                state, stopper):
    # Otherwise it would wait for the next line, which on a quiet service could
    # be never - and the whole thing would look broken.
    row = _watching("svc", "print('ready to serve');import time;time.sleep(30)", [])
    service = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    stopper.append(service)
    service.start()
    assert _pump(qapp, lambda: service.console() == ["ready to serve"])

    edited = row.copy()
    edited.criteria = [cr.CriterionRow(name="start", rules=[
        cr.Rule(cr.MATCH, cr.TEXT, "ready to serve")])]
    service.update(edited, str(tmp_path))
    assert _lit(service, "start") is True


def test_a_service_with_no_criteria_reports_none(qapp, tmp_path, state):
    service = services.ServiceProcess(
        "Claim", _shell_row("plain", "pass"), str(tmp_path), state=state)
    assert service.criteria_state() == []
    assert service.needs_poll() is False


def test_the_supervisor_relays_which_service_moved(qapp, supervisor):
    criterion = cr.CriterionRow(name="start", rules=[
        cr.Rule(cr.MATCH, cr.TEXT, "ready to serve")])
    heard = []
    supervisor.criteria_changed.connect(
        lambda project, name: heard.append((project, name)))
    supervisor.sync(_project(_watching(
        "svc", "print('ready to serve');import time;time.sleep(30)", [criterion])))
    supervisor.start("Claim", "svc")
    assert _pump(qapp, lambda: supervisor.criteria_state("Claim", "svc")[0][2])
    assert ("Claim", "svc") in heard


def test_a_watched_path_may_be_written_with_a_tilde(qapp, tmp_path, state,
                                                    monkeypatch, stopper):
    # It is a path somebody typed, and `~/logs/app.log` is not one anything can
    # open - but it is still the key the matcher is found by.
    monkeypatch.setenv("HOME", str(tmp_path))
    log = tmp_path / "app.log"
    log.write_text("")
    criterion = cr.CriterionRow(name="start", source="~/app.log", rules=[
        cr.Rule(cr.MATCH, cr.TEXT, "listening")])
    row = _watching("app", "import time;time.sleep(30)", [criterion])
    service = services.ServiceProcess("Claim", row, str(tmp_path), state=state)
    stopper.append(service)
    service.start()
    with open(log, "a") as handle:
        handle.write("listening on 0.0.0.0:8000\n")
    service.poll()
    assert _lit(service, "start") is True


# ------------------------------------------------- what is up, and what fell over
def test_the_supervisor_separates_what_is_running_from_what_failed(
        qapp, supervisor):
    """Two readers, because the rail asks two questions of the same set.

    ``counts`` answers "how many of these are up", which is the block header's
    question; a failure is not the absence of a success and needs its own.
    """
    supervisor.sync(_stack(_shell_row("up", "import time;time.sleep(30)"),
                           _shell_row("gone", "import sys;sys.exit(3)")))
    supervisor.start("Claim", "up")
    supervisor.start("Claim", "gone")
    assert _pump(qapp, lambda: len(supervisor.failed()) == 1)
    assert [s.name for s in supervisor.running()] == ["up"]
    assert [s.name for s in supervisor.failed()] == ["gone"]
    assert supervisor.counts("Claim") == (1, 2)


def test_a_service_that_was_never_started_is_in_neither_list(supervisor):
    supervisor.sync(_stack(_shell_row("idle", "pass")))
    assert supervisor.running() == []
    assert supervisor.failed() == []


def test_failed_is_about_the_process_and_not_about_criteria(qapp, supervisor):
    """A criterion that has not lit is not a failure. STATUS means the process."""
    criterion = cr.CriterionRow(name="ready", rules=[
        cr.Rule(cr.MATCH, cr.TEXT, "never printed")])
    supervisor.sync(_project(_watching(
        "svc", "import time;time.sleep(30)", [criterion])))
    supervisor.start("Claim", "svc")
    assert _pump(qapp, lambda: supervisor.running())
    assert supervisor.criteria_state("Claim", "svc")[0][2] is False
    assert supervisor.failed() == []


# --------------------------------------------- stopping a server that forks

#: A master that forks a worker and exits without taking it with it - the shape
#: of Odoo in prefork, gunicorn, and anything else with a worker pool. Signalling
#: the master alone leaves the worker holding whatever it holds.
PREFORK = """
import os, signal, sys, time
if os.fork() == 0:
    def stop(*a): sys.exit(0)
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    print('worker up', flush=True)
    while True: time.sleep(0.05)
print('master up', flush=True)
def bye(*a): sys.exit(0)
signal.signal(signal.SIGTERM, bye); signal.signal(signal.SIGINT, bye)
while True: time.sleep(0.05)
"""


def _script(tmp_path, body):
    path = tmp_path / "prefork_service.py"
    path.write_text(body, encoding="utf-8")
    return "%s -u %s" % (sys.executable, path)


def _alive(pid):
    """Is this exact pid still there? Asked by pid, so a dead group cannot lie."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _descendants(pid):
    """Every process still in this pid's group, excluding the leader itself."""
    try:
        group = os.getpgid(pid)
    except OSError:
        return []
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == pid:
            continue
        try:
            if os.getpgid(int(entry)) == group:
                out.append(int(entry))
        except OSError:
            continue
    return out


@pytest.mark.skipif(os.name == "nt", reason="process groups are a POSIX thing")
def test_an_attached_service_gets_its_own_process_group(qapp, tmp_path, state,
                                                        stopper):
    """Without one, the only thing safe to signal is the one process.

    Its group would be *ours*, and killing that would take the application with
    it - so a forking server could only ever lose its master.
    """
    row = sf.RunnerRow(name="web", type="shell", detach=False,
                       settings={"command": _script(tmp_path, PREFORK)})
    service = services.ServiceProcess("p", row, state=state)
    stopper.append(service)
    service.start()
    assert _pump(qapp, lambda: service.status == RUNNING)
    pid = int(service._proc.processId())
    assert services.leads_group(pid)
    assert os.getpgid(pid) != os.getpgid(0)


@pytest.mark.skipif(os.name == "nt", reason="process groups are a POSIX thing")
def test_stopping_a_forking_service_takes_its_workers_with_it(qapp, tmp_path,
                                                              state, stopper):
    # The bug this exists for: the worker went on running, kept the port, and
    # the next start could not bind - while the row said "running".
    row = sf.RunnerRow(name="web", type="shell", detach=False,
                       settings={"command": _script(tmp_path, PREFORK)})
    service = services.ServiceProcess("p", row, state=state)
    stopper.append(service)
    service.start()
    assert _pump(qapp, lambda: service.status == RUNNING)
    assert _pump(qapp, lambda: len(service.console()) >= 2)
    pid = int(service._proc.processId())
    workers = _descendants(pid)
    assert workers, "the fixture should have forked a worker"

    service.stop()
    assert _pump(qapp, lambda: service.status == STOPPED)
    # The worker pids captured before the stop, not the group - once the master
    # is gone there is no group left to ask about, so a test that looked one up
    # afterwards would pass whether or not the worker was still running.
    assert _pump(qapp, lambda: not any(_alive(w) for w in workers), timeout=8), \
        "the worker outlived the service that forked it"


# ------------------------------------------------------- how long it may take

def test_the_stop_timeout_defaults_to_the_module_constant(state):
    row = sf.RunnerRow(name="s", type="shell", settings={"command": "true"})
    service = services.ServiceProcess("p", row, state=state)
    assert service.stop_grace_ms() == services.STOP_GRACE_MS


def test_a_service_can_ask_for_longer(state):
    # The reason this exists: eight seconds is nowhere near a module update.
    row = sf.RunnerRow(name="s", type="shell",
                       settings={"command": "true", "stop_grace": "300"})
    service = services.ServiceProcess("p", row, state=state)
    assert service.stop_grace_ms() == 300000


def test_zero_means_never_kill_it(state):
    row = sf.RunnerRow(name="s", type="shell",
                       settings={"command": "true", "stop_grace": "0"})
    service = services.ServiceProcess("p", row, state=state)
    assert service.stop_grace_ms() is None


def test_a_nonsense_timeout_falls_back_rather_than_raising(state):
    row = sf.RunnerRow(name="s", type="shell",
                       settings={"command": "true", "stop_grace": "soon"})
    service = services.ServiceProcess("p", row, state=state)
    assert service.stop_grace_ms() == services.STOP_GRACE_MS


def test_a_wild_timeout_is_capped(state):
    row = sf.RunnerRow(name="s", type="shell",
                       settings={"command": "true", "stop_grace": "999999"})
    service = services.ServiceProcess("p", row, state=state)
    assert service.stop_grace_ms() == services.STOP_GRACE_MAX_S * 1000


@pytest.mark.skipif(os.name == "nt", reason="process groups are a POSIX thing")
def test_a_service_that_needs_time_is_not_cut_in_half(qapp, tmp_path, state,
                                                      stopper):
    """The whole point of the setting, end to end.

    The fixture asks for four seconds after the signal before it finishes. With
    the default grace it would be killed at eight, which is the same as saying
    a module update may not take longer than eight seconds.
    """
    slow = tmp_path / "slow_service.py"
    slow.write_text(
        "import signal, sys, time\n"
        "asked = {'at': 0.0}\n"
        "def later(*a):\n"
        "    asked['at'] = time.time()\n"
        "signal.signal(signal.SIGTERM, later)\n"
        "signal.signal(signal.SIGINT, later)\n"
        "print('working', flush=True)\n"
        "while True:\n"
        "    if asked['at'] and time.time() - asked['at'] > 4:\n"
        "        print('FINISHED', flush=True); sys.exit(0)\n"
        "    time.sleep(0.05)\n", encoding="utf-8")
    row = sf.RunnerRow(name="s", type="shell", detach=False,
                       settings={"command": "%s -u %s" % (sys.executable, slow),
                                 "stop_grace": "30"})
    service = services.ServiceProcess("p", row, state=state)
    stopper.append(service)
    service.start()
    assert _pump(qapp, lambda: service.status == RUNNING)
    assert _pump(qapp, lambda: any("working" in l for l in service.console()))

    service.stop()
    assert _pump(qapp, lambda: service.status == STOPPED, timeout=25)
    assert any("FINISHED" in line for line in service.console()), \
        "it was killed before it could finish"


def test_a_restart_is_not_killed_by_the_timer_that_stopped_it(qapp, tmp_path,
                                                              state, stopper):
    """The bug: a restarted service died seconds later, for no visible reason.

    stop() arms a timer to kill whatever has not gone down by the grace period.
    A restart stops and starts again in well under that, so the timer came back
    to a running service - the new one - and killed it. From the outside it
    looked like whatever you did next had killed it, because that is what you
    were doing when the timer finally fired.

    A short grace here so the test does not sit through the real eight seconds;
    what is being checked is that the timer knows which process it was for.
    """
    row = sf.RunnerRow(name="s", type="shell", detach=False,
                       settings={"command": _shell_row("x", "").settings["command"],
                                 "stop_grace": "1"})
    row.settings["command"] = _python("import time\nwhile True: time.sleep(0.05)")
    service = services.ServiceProcess("p", row, state=state)
    stopper.append(service)
    service.start()
    assert _pump(qapp, lambda: service.status == RUNNING)

    # Stop and start again inside the grace window, the way restart does.
    service.stop()
    assert _pump(qapp, lambda: service.status == STOPPED, timeout=10)
    service.start()
    assert _pump(qapp, lambda: service.status == RUNNING)
    survivor = int(service._proc.processId())

    # Well past the moment the first stop\'s timer comes back.
    deadline = time.time() + 3.0
    while time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    assert service.status == RUNNING, "the stop timer killed the new process"
    assert int(service._proc.processId()) == survivor
