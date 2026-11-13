"""Service actions through the QCR host (ADR-002 2.3b) - with the real pieces.

The live tests run the real ``ServiceSupervisor``, a throwaway ``python -c``
service and the real ``--qcr-host`` process in the core's own environment, and
read the journal back with the core: the start and the stop are there, asked by
the GUI's actor. The page tests use a stand-in host to check what is routed
and what keeps the page's own path.
"""

import json
import os
import shlex
import subprocess
import sys
import time

import pytest
from PySide6.QtCore import QObject, Signal

from cms_gui import criteria as criteria_mod
from cms_gui import qcrhost
from cms_gui import servicesfile as sf
from cms_gui.services import ServiceSupervisor

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORE_PYTHON = os.path.join(ROOT, ".venv", "bin", "python")
WEB, DB, GROUP = "a" * 32, "b" * 32, "f" * 32

needs_core = pytest.mark.skipif(not os.path.exists(CORE_PYTHON), reason="needs the core's .venv")


def pump(qapp, done, seconds=20):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        if done():
            return True
        time.sleep(0.01)
    return False


def service(name, qcr_id=None, depends=(), criteria=()):
    command = shlex.join([sys.executable, "-u", "-c", "import time; print('ready', flush=True); time.sleep(60)"])
    return sf.RunnerRow(name=name, type="shell", detach=False, settings={"command": command},
                        depends=list(depends), criteria=list(criteria),
                        extra={"qcr_id": qcr_id} if qcr_id else {})


def ready_criterion():
    rule = criteria_mod.Rule(mode=criteria_mod.MATCH, kind=criteria_mod.REGEX, pattern="ready")
    return criteria_mod.CriterionRow(name="ready", rules=[rule])


@pytest.fixture
def live(qapp, tmp_path):
    made = []

    def build(*runners):
        supervisor = ServiceSupervisor()
        supervisor.sync([sf.ProjectRow(name="Group", runners=list(runners), extra={"qcr_id": GROUP})])
        host = qcrhost.QcrHost(supervisor)
        store = qcrhost.store_path(str(tmp_path))
        host.start([CORE_PYTHON, os.path.join(ROOT, "session_launcher.py"), "--qcr-host", "--qcr-socket=off",
                    "--qcr-project=p", "--qcr-store=" + store], "p")
        host.executions = []
        host.execution_changed.connect(host.executions.append)
        made.append((supervisor, host))
        # Nothing is sent by the test: the host greets on its own when it starts.
        assert pump(qapp, lambda: host.state == qcrhost.READY), "host never said ready"
        return supervisor, host, store

    yield build
    for supervisor, host in made:
        host.stop()
        supervisor.shutdown(3000)


def finished(host, op):
    done = [e for e in host.executions if e["capability_id"].endswith("." + op)
            and e["state"] in qcrhost.TERMINAL]
    return done[-1] if done else None


def journal(store):
    """What the core reads back from the journal - a different interpreter and environment."""
    code = ("import json,sys; sys.path.insert(0, %r); from qcr.executions import ExecutionStore; "
            "s = ExecutionStore(%r); print(json.dumps(s.list('p')))" % (ROOT, store))
    answer = subprocess.run([CORE_PYTHON, "-c", code], capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert answer.returncode == 0, answer.stderr
    return json.loads(answer.stdout)


@needs_core
def test_a_start_and_a_stop_go_through_the_host_and_land_in_the_journal(qapp, live):
    supervisor, host, store = live(service("Web", WEB, criteria=[ready_criterion()]))
    replies = []
    host.invoke("start", "Group", "Web", replies.append)
    assert pump(qapp, lambda: finished(host, "start")), host.executions
    started = finished(host, "start")
    assert replies[0]["ok"] and started["state"] == "succeeded"
    assert supervisor.service("Group", "Web").status == "running"
    assert started["output"]["service_status"] == "running"
    # Criteria are reported as observed, never turned into readiness: they have no
    # sign ("Serving" lit is good, "Crashed" lit is bad) - see qcrhost.
    assert started["output"]["readiness"] == "not_checked"
    assert started["output"]["criteria"][0]["name"] == "ready"
    # The next step named outright: with criteria, wait on them.
    assert started["output"]["has_criteria"] is True
    assert started["output"]["next"] == "services.%s.criteria" % WEB

    host.invoke("stop", "Group", "Web")
    assert pump(qapp, lambda: finished(host, "stop"))
    assert finished(host, "stop")["state"] == "succeeded"
    assert pump(qapp, lambda: supervisor.service("Group", "Web").status == "stopped")

    kept = journal(store)
    assert [(e["capability_id"].rsplit(".", 1)[1], e["state"], e["actor_id"]) for e in kept] == [
        ("stop", "succeeded", "gui-user"), ("start", "succeeded", "gui-user")]


@needs_core
def test_a_second_action_on_a_busy_service_is_refused(qapp, live):
    supervisor, host, _store = live(service("Web", WEB))
    replies = []
    host.invoke("start", "Group", "Web", replies.append)
    host.invoke("restart", "Group", "Web", replies.append)
    assert pump(qapp, lambda: len(replies) == 2)
    assert replies[0]["ok"] and replies[1]["error"]["code"] == "busy"
    assert pump(qapp, lambda: finished(host, "start"))


@needs_core
def test_a_dependency_without_an_identity_refuses_the_start_and_nothing_runs(qapp, live):
    supervisor, host, store = live(service("Web", WEB, depends=["Db"]), service("Db"))
    replies = []
    host.invoke("start", "Group", "Web", replies.append)
    assert pump(qapp, lambda: replies)
    assert replies[0]["error"]["code"] == "dependency_unmigrated"
    pump(qapp, lambda: False, 0.3)
    assert supervisor.service("Group", "Web").status == "stopped"
    assert supervisor.service("Group", "Db").status == "stopped"
    assert journal(store) == []


@needs_core
def test_a_dependency_with_an_identity_is_part_of_the_snapshot(qapp, live):
    supervisor, _host, _store = live(service("Web", WEB, depends=["Db"]), service("Db", DB))
    rows = {row["resource_id"]: row for row in supervisor.qcr_snapshot("p")["resources"]}
    assert rows[WEB]["depends_on"] == [DB] and "unmigrated_dependencies" not in rows[WEB]


@needs_core
def test_a_host_that_dies_is_started_again(qapp, live):
    _supervisor, host, _store = live(service("Web", WEB))
    first = host._proc.processId()
    host._proc.kill()
    assert pump(qapp, lambda: host.state == qcrhost.STARTING)
    # The relaunched host is greeted the way the first was, with nothing from the test.
    assert pump(qapp, lambda: host.state == qcrhost.READY)
    assert host._proc.processId() != first


def test_a_service_without_an_identity_is_not_routed(qapp):
    supervisor = ServiceSupervisor()
    supervisor.sync([sf.ProjectRow(name="Group", runners=[service("Web", WEB), service("Old")])])
    host = qcrhost.QcrHost(supervisor)
    assert not host.handles("Group", "Web"), "off: nothing is routed"
    host.state = qcrhost.READY
    assert host.handles("Group", "Web") and not host.handles("Group", "Old")
    supervisor.shutdown(1000)


# -- the page ------------------------------------------------------------------------
class FakeHost(QObject):
    finished_badly = Signal(dict)

    def __init__(self, routed):
        super().__init__()
        self.state, self.routed, self.asked = qcrhost.READY, set(routed), []

    def handles(self, project, name):
        return name in self.routed

    def invoke(self, op, project, name, done=None, request=None):
        self.asked.append((op, name))
        if done:
            done({"ok": False, "error": {"code": "busy", "message": "x"}})

    def schedule_snapshot(self):
        pass


@pytest.fixture
def page(qapp, tmp_path):
    from cms_gui.pages.services import ServicesPage
    document = {"version": 1, "projects": [{"name": "Group", "dir": "", "qcr_id": GROUP, "runners": [
        {"name": "Web", "type": "shell", "settings": {"command": "true"}, "qcr_id": WEB},
        {"name": "Old", "type": "shell", "settings": {"command": "true"}}]}]}
    (tmp_path / "services.json").write_text(json.dumps(document))
    (tmp_path / "logsources.json").write_text(json.dumps({"connections": [], "logs": []}))
    widget = ServicesPage()
    widget.load(str(tmp_path / "logsources.json"), str(tmp_path / "services.json"),
                str(tmp_path / "logsources.json"))
    started = []
    widget.supervisor.start = lambda project, name, *a: started.append(name)
    widget.supervisor.start_all = lambda project=None, names=None: started.append(("all", names))
    widget.started = started
    yield widget
    widget.shutdown(2000)
    widget.close()


def test_the_page_routes_what_has_an_identity_and_keeps_the_rest(page):
    host = FakeHost({"Web"})
    page.set_qcr(host)
    problems = []
    page._show_problem = problems.append
    page.toggle_runner("Group", "Web")
    page.toggle_runner("Group", "Old")
    assert host.asked == [("start", "Web")] and page.started == ["Old"]
    assert problems and "another action on this service is still running" in problems[0]


def test_start_all_splits_between_the_host_and_the_page(page):
    host = FakeHost({"Web"})
    page.set_qcr(host)
    page._show_problem = lambda text: None
    page.start_project("Group")
    assert host.asked == [("start", "Web")] and page.started == [("all", ["Old"])]


def test_with_the_host_off_nothing_changes(page):
    host = FakeHost({"Web"})
    host.state = qcrhost.OFF
    host.handles = lambda project, name: False
    page.set_qcr(host)
    page.toggle_runner("Group", "Web")
    page.start_project("Group")
    assert host.asked == [] and page.started == ["Web", ("all", None)]


# -- 2.4: runs ask through the host too ---------------------------------------------------
@pytest.fixture
def shared(qapp, tmp_path):
    """A host on the main window's own bridge, whose launcher pipe is a list."""
    from cms_gui.servicebridge import ServiceBridge
    made = []

    def build(*runners):
        supervisor = ServiceSupervisor()
        supervisor.sync([sf.ProjectRow(name="Group", runners=list(runners), extra={"qcr_id": GROUP})])
        launcher = []
        bridge = ServiceBridge(supervisor, lambda **answer: launcher.append(answer))
        host = qcrhost.QcrHost(supervisor, bridge=bridge)
        host.start([CORE_PYTHON, os.path.join(ROOT, "session_launcher.py"), "--qcr-host", "--qcr-socket=off", "--qcr-project=p",
                    "--qcr-store=" + qcrhost.store_path(str(tmp_path))], "p")
        host.executions = []
        host.execution_changed.connect(host.executions.append)
        made.append((supervisor, host))
        return supervisor, host, bridge, launcher

    yield build
    for supervisor, host in made:
        host.stop()
        supervisor.shutdown(3000)


@needs_core
def test_a_run_s_restart_goes_through_the_bridge_its_waits_use(qapp, shared):
    """The restart race itself is the bridge's (test_servicebridge.py); what this
    shows is that a restart made through QCR is made on the SAME bridge - so its
    "a new run is owed" mark is the one a scenario's next wait reads."""
    supervisor, host, bridge, launcher = shared(service("Web", WEB))
    asked = []
    handle = bridge.handle
    bridge.handle = lambda event, reply=None: (asked.append((event["op"], event["id"])), handle(event, reply))[1]
    host.grant_run("cycle:r1")                         # before ready: queued, then greeted with
    assert pump(qapp, lambda: host.state == qcrhost.READY)
    supervisor.start("Group", "Web")
    assert pump(qapp, lambda: supervisor.service("Group", "Web").status == "running")
    statuses = []
    supervisor.status_changed.connect(lambda project, name, status: statuses.append(status))

    ended = []
    host.invoke("restart", "Group", "Web", request={"wait_ready": False}, actor="cycle:r1",
                finished=ended.append)
    assert pump(qapp, lambda: ended)
    assert ended[0]["state"] == "succeeded" and ended[0]["actor_id"] == "cycle:r1"
    assert [op for op, request_id in asked if str(request_id).startswith("qcr-")] == ["service_restart"]
    # The scenario's next step, on the same bridge: answered for the new run only.
    bridge.handle({"kind": "service.request", "id": 1, "op": "wait_for_service", "project": "Group",
                   "service": "Web", "timeout_ms": 15000})
    assert pump(qapp, lambda: launcher)
    assert launcher[0]["ok"] and "starting" in statuses, (launcher, statuses)
    assert [answer["id"] for answer in launcher] == [1], "the launcher got only its own answer"


@needs_core
def test_a_run_s_actor_ends_with_the_run(qapp, shared):
    _supervisor, host, _bridge, _launcher = shared(service("Web", WEB))
    assert pump(qapp, lambda: host.state == qcrhost.READY)
    host.grant_run("run:7")
    replies = []
    host.invoke("start", "Group", "Web", replies.append, request={"wait_ready": False}, actor="run:7")
    assert pump(qapp, lambda: replies) and replies[0]["ok"]
    assert pump(qapp, lambda: finished(host, "start"))
    host.release_runs()
    host.invoke("stop", "Group", "Web", replies.append, actor="run:7")
    assert pump(qapp, lambda: len(replies) == 2)
    assert replies[1]["error"]["code"] == "authorization_denied"


def test_the_launcher_going_away_leaves_the_host_s_waits_alone(qapp):
    from cms_gui.servicebridge import ServiceBridge
    supervisor = ServiceSupervisor()
    supervisor.sync([sf.ProjectRow(name="Group", runners=[service("Web", WEB)])])
    launcher, mine = [], []
    bridge = ServiceBridge(supervisor, lambda **answer: launcher.append(answer))
    bridge.handle({"kind": "service.request", "id": 1, "op": "wait_for_service", "project": "Group",
                   "service": "Web", "timeout_ms": 0})
    bridge.handle({"kind": "service.request", "id": "qcr-1", "op": "wait_for_service", "project": "Group",
                   "service": "Web", "timeout_ms": 0}, reply=lambda **answer: mine.append(answer))
    bridge.cancel_all()
    assert set(bridge._pending) == {"qcr-1"}
    assert bridge.cancel("qcr-1") and bridge._pending == {} and not mine and not launcher
    supervisor.shutdown(1000)


# -- the main window's routing of a launcher's request ---------------------------------------
class RecordingHost:
    state = qcrhost.READY

    def __init__(self, routed=("Web",), reply=None, end=None):
        self.routed, self.reply, self.end = set(routed), reply, end
        self.granted, self.asked = [], []

    def handles(self, project, name):
        return name in self.routed

    def grant_run(self, actor):
        self.granted.append(actor)

    def invoke(self, op, project, name, done=None, request=None, actor=None, finished=None):
        self.asked.append((op, name, request, actor))
        if self.reply is not None:
            done(self.reply)
        if self.end is not None:
            finished(self.end)


def window(host, cycle_run=None, entry=None):
    import functools
    import types
    from cms_gui.main_window import MainWindow
    sent = []
    fake = types.SimpleNamespace(
        qcr_host=host, _QCR_OPS=MainWindow._QCR_OPS, _entry_id=entry,
        run_state=types.SimpleNamespace(cycle={"run_id": cycle_run} if cycle_run else None,
                                        cycle_running=bool(cycle_run)),
        service_bridge=types.SimpleNamespace(resolve=lambda project, name: (
            (object(), ("Group", name)) if name in ("Web", "Old") else (None, "no service"))),
        process=types.SimpleNamespace(send_command=lambda **command: sent.append(command)))
    fake._run_actor = functools.partial(MainWindow._run_actor, fake)
    return functools.partial(MainWindow._service_request_via_qcr, fake), sent


def request(op, name="Web", request_id=5):
    return {"kind": "service.request", "id": request_id, "op": op, "project": "Group", "service": name}


def test_a_cycle_s_restart_goes_to_the_host_as_the_cycle(qapp):
    host = RecordingHost(end={"state": "succeeded", "output": {"service_status": "starting"}})
    route, sent = window(host, cycle_run="r1")
    assert route(request("service_restart")) is True
    assert host.granted == ["cycle:r1"]
    assert host.asked == [("restart", "Web", {"wait_ready": False}, "cycle:r1")]
    assert sent == [{"command": "service.result", "id": 5, "ok": True, "status": "starting", "message": "starting"}]


def test_a_refusal_fails_the_step_with_its_reason(qapp):
    host = RecordingHost(reply={"ok": False, "error": {"code": "busy"}})
    route, sent = window(host, entry="h-3")
    assert route(request("service_stop")) is True
    assert host.asked[0][3] == "run:h-3"
    assert sent[0]["ok"] is False and "another action on this service" in sent[0]["message"]


def test_an_ended_execution_that_did_not_succeed_fails_the_step(qapp):
    host = RecordingHost(end={"state": "failed", "error": {"code": "service_failed", "message": "exited 1"}})
    route, sent = window(host)
    assert route(request("service_start")) is True
    assert host.asked[0][3] == "run:launcher"
    assert sent[0]["ok"] is False and "exited 1" in sent[0]["message"]


@pytest.mark.parametrize("event", [request("wait_for_service"), request("service_start", name="Old"),
                                   request("service_start", name="Nowhere")])
def test_waits_unrouted_services_and_unknown_names_stay_with_the_bridge(qapp, event):
    host = RecordingHost()
    route, sent = window(host)
    assert route(event) is False and host.asked == [] and sent == []


def test_with_the_host_off_the_bridge_answers_everything(qapp):
    host = RecordingHost()
    host.state = qcrhost.OFF
    route, _sent = window(host)
    assert route(request("service_start")) is False


def test_a_service_without_an_identity_is_told_so_not_that_the_host_is_down(qapp):
    supervisor = ServiceSupervisor()
    supervisor.sync([sf.ProjectRow(name="Group", runners=[service("Old")])])
    host = qcrhost.QcrHost(supervisor)
    host.state = qcrhost.READY
    replies = []
    host.invoke("start", "Group", "Old", replies.append)
    host.invoke("start", "Group", "Nowhere", replies.append)
    assert [reply["error"]["code"] for reply in replies] == ["not_routed", "not_routed"]
    assert qcrhost.words(replies[0]["error"]) == "this service has no QCR identity"
    supervisor.shutdown(1000)


def test_another_project_s_inventory_is_never_sent_to_this_project_s_host(qapp, tmp_path):
    """A project switch left the old inventory behind; the new host refused it twice."""
    import json as json_
    import types as types_
    supervisor = ServiceSupervisor()
    host = qcrhost.QcrHost(supervisor)
    sent = []
    host._launch = lambda: setattr(host, "_proc", types_.SimpleNamespace(
        write=lambda data: sent.append(json_.loads(bytes(data))), state=lambda: 0))
    host.start(["core", "--qcr-host"], "claim")
    host.set_inventory({"schema_version": 1, "project_id": "claim", "scenarios": [], "environments": []})
    assert [m["inventory"]["project_id"] for m in sent if m["type"] == "inventory"] == ["claim"]
    host._proc = None
    host.start(["core", "--qcr-host", "demo"], "demo")
    host._push_inventory()
    assert [m["inventory"]["project_id"] for m in sent if m["type"] == "inventory"] == ["claim"]
    host.set_inventory({"schema_version": 1, "project_id": "claim", "scenarios": [], "environments": []})
    assert [m["inventory"]["project_id"] for m in sent if m["type"] == "inventory"] == ["claim"], "late, and not demo's"
    host.set_inventory({"schema_version": 1, "project_id": "demo", "scenarios": [], "environments": []})
    assert [m["inventory"]["project_id"] for m in sent if m["type"] == "inventory"] == ["claim", "demo"]
    supervisor.shutdown(1000)


def test_an_inventory_loaded_before_qcr_was_switched_on_reaches_the_host(qapp):
    import json as json_
    import types as types_
    supervisor = ServiceSupervisor()
    host = qcrhost.QcrHost(supervisor)
    host.set_inventory({"schema_version": 1, "project_id": "demo", "scenarios": [], "environments": []})
    sent = []
    host._launch = lambda: setattr(host, "_proc", types_.SimpleNamespace(
        write=lambda data: sent.append(json_.loads(bytes(data))), state=lambda: 0))
    host.start(["core", "--qcr-host"], "demo")
    host._push_inventory()
    assert [m["inventory"]["project_id"] for m in sent if m["type"] == "inventory"] == ["demo"]
    supervisor.shutdown(1000)


def test_the_snapshot_carries_each_criterion_with_its_colour(qapp):
    crashed = criteria_mod.CriterionRow(name="Crashed", color="red", rules=[
        criteria_mod.Rule(mode=criteria_mod.MATCH, kind=criteria_mod.TEXT, pattern="Traceback")])
    supervisor = ServiceSupervisor()
    supervisor.sync([sf.ProjectRow(name="Group", runners=[service("Web", WEB, criteria=[ready_criterion(), crashed])],
                                   extra={"qcr_id": GROUP})])
    row = supervisor.qcr_snapshot("p")["resources"][0]
    assert row["criteria"] == [{"name": "ready", "color": "green", "met": False},
                               {"name": "Crashed", "color": "red", "met": False}]
    supervisor.shutdown(1000)


def test_the_window_answers_an_output_read_from_the_service_s_console(qapp):
    import json as json_
    import types as types_
    supervisor = ServiceSupervisor()
    supervisor.sync([sf.ProjectRow(name="Group", runners=[service("Web", WEB)], extra={"qcr_id": GROUP})])
    supervisor.service("Group", "Web")._lines.extend(["one", "two", "three"])
    host = qcrhost.QcrHost(supervisor)
    sent = []
    host._proc = types_.SimpleNamespace(write=lambda data: sent.append(json_.loads(bytes(data))))
    host._take({"type": "owner_read", "id": 4, "op": "service.output", "resource_id": WEB, "lines": 2})
    host._take({"type": "owner_read", "id": 5, "op": "service.output", "resource_id": "f" * 32, "lines": 2})
    assert sent[0] == {"type": "owner_read_reply", "id": 4, "result": {"lines": ["two", "three"],
                                                                       "service_status": "stopped"}}
    assert sent[1]["error"]["code"] == "not_found"
    supervisor.shutdown(1000)



def test_a_service_without_criteria_points_at_its_output(qapp):
    supervisor = ServiceSupervisor()
    supervisor.sync([sf.ProjectRow(name="Group", runners=[service("Web", WEB)], extra={"qcr_id": GROUP})])
    host = qcrhost.QcrHost(supervisor)
    host._jobs["e1"] = {"key": ("Group", "Web")}
    output = host._start_output("e1", "running")
    assert output["has_criteria"] is False and output["next"] == "services.%s.output" % WEB
    supervisor.shutdown(1000)
