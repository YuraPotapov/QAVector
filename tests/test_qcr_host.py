"""The QCR host (ADR-002 2.3a): the GUI's side of the pipe, played by the test.

Most tests drive :class:`qcr.host.Host` in-process with deadlines fired by hand;
the last ones run the real ``session_launcher.py --qcr-host`` process and talk
to it over its pipes, kill it, and start it again on the same journal.
"""

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from qcr.host import Host

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB, DB, OLD = "a" * 32, "b" * 32, "c" * 32
GROUP = "f" * 32
USER = [{"permission": p} for p in ("services.read", "services.start", "services.stop", "services.restart",
                                    "executions.read", "executions.cancel")]


def row(resource_id, name, status="stopped", depends_on=(), unmigrated=0):
    one = {"resource_id": resource_id, "group_id": GROUP, "alias": "Stand/" + name, "display_name": name,
           "properties": {"status": status, "runner_type": "python", "detached": False,
                          "configuration_stale": False}, "depends_on": list(depends_on)}
    if unmigrated:
        one["unmigrated_dependencies"] = unmigrated
    return one


def snapshot(*rows):
    return {"schema_version": 1, "project_id": "p", "observed_at": time.time(), "ttl_seconds": 5,
            "resources": list(rows), "legacy_count": 0}


class Timers:
    def __init__(self):
        self.made = []

    def __call__(self, seconds, work):
        timer = type("T", (), {"cancelled": False, "seconds": seconds})()
        timer.work = work
        timer.cancel = lambda: setattr(timer, "cancelled", True)
        self.made.append(timer)
        return timer

    def fire(self, seconds):
        for timer in list(self.made):
            if timer.seconds == seconds and not timer.cancelled:
                timer.work()
                return
        raise AssertionError("no live timer of %s s" % seconds)


class Gui:
    """The test as the GUI: what it sent, what it got back."""

    def __init__(self, tmp_path, rows=None, actors=None):
        self.out = []
        self.timers = Timers()
        self.host = Host("p", str(tmp_path / ".qcr" / "executions.sqlite"),
                         lambda line: self.out.append(json.loads(line)), schedule=self.timers)
        self.send({"type": "snapshot", "snapshot": snapshot(*(rows or [row(WEB, "web"), row(DB, "db")]))})
        self.send({"type": "hello", "protocol": 1, "actors": actors if actors is not None else {"gui-user": USER}})
        self._id = 0

    def send(self, message):
        self.host.feed(json.dumps(message))

    def ask(self, kind, **fields):
        self._id += 1
        before = len(self.out)
        self.send(dict({"type": kind, "id": self._id, "actor": "gui-user"}, **fields))
        replies = [m for m in self.out[before:] if m.get("type") == "reply" and m.get("id") == self._id]
        assert len(replies) == 1, self.out[before:]
        return replies[0]

    def invoke(self, op="start", resource=WEB, **fields):
        return self.ask("invoke", capability="services.%s.%s" % (resource, op), **fields)

    def owner(self, execution_id, event, **fields):
        self.send(dict({"type": "owner", "execution_id": execution_id, "event": event}, **fields))

    def last(self, kind):
        return [m for m in self.out if m["type"] == kind][-1]

    def of(self, kind):
        return [m for m in self.out if m["type"] == kind]

    def state(self, execution_id):
        return self.ask("get", execution_id=execution_id)["execution"]["state"]


@pytest.fixture
def gui(tmp_path):
    one = Gui(tmp_path)
    yield one
    one.host.close()


def test_hello_comes_first_and_is_answered_with_ready(tmp_path):
    early = Gui.__new__(Gui)
    early.out, early.timers, early._id = [], Timers(), 0
    early.host = Host("p", str(tmp_path / "e.sqlite"), lambda line: early.out.append(json.loads(line)),
                      schedule=early.timers)
    assert early.ask("invoke", capability="services.%s.start" % WEB)["error"]["code"] == "not_ready"
    early.send({"type": "hello", "protocol": 2})
    assert early.out[-1]["error"]["code"] == "unsupported_protocol"
    early.send({"type": "hello", "protocol": 1, "actors": {}})
    assert early.out[-1] == {"type": "ready", "protocol": 1, "recovered": [], "socket": None}
    early.host.close()


def test_a_start_goes_to_the_owner_and_its_reports_make_the_outcome(gui):
    reply = gui.invoke(input={"wait_ready": True, "timeout_seconds": 60})
    assert reply["ok"] and reply["execution"]["state"] == "queued"
    execution_id = reply["execution"]["execution_id"]
    assert gui.last("dispatch") == {"type": "dispatch", "execution_id": execution_id, "op": "start",
                                    "resource_id": WEB, "actor": "gui-user",
                                    "input": {"wait_ready": True, "timeout_seconds": 60}}
    gui.owner(execution_id, "accepted")
    gui.owner(execution_id, "progress", kind="status", payload={"status": "starting"})
    gui.owner(execution_id, "running")
    gui.owner(execution_id, "succeeded", output={"service_status": "running", "readiness": "ready",
                                                  "criteria": [{"name": "listening", "met": True}]})
    pushed = [m["execution"]["state"] for m in gui.of("execution")]
    assert pushed == ["queued", "dispatched", "dispatched", "running", "succeeded"]
    assert gui.ask("get", execution_id=execution_id)["execution"]["output"]["readiness"] == "ready"
    events = gui.ask("events", execution_id=execution_id)
    assert [e["type"] for e in events["events"]] == ["state", "state", "owner.status", "state", "state"]
    assert gui.of("stop_waiting") == []
    # The deadline asked for: the ack window plus the wait.
    assert sorted(t.seconds for t in gui.timers.made) == [15, 75]


def test_who_asks_is_the_gui_s_word_not_the_message_s(tmp_path):
    gui = Gui(tmp_path, actors={"gui-user": USER, "viewer": [{"permission": "services.read"}]})
    assert gui.ask("invoke", actor="nobody", capability="services.%s.start" % WEB)["error"]["code"] == \
        "authorization_denied"
    viewer = gui.ask("invoke", actor="viewer", capability="services.%s.start" % WEB, grants=USER)
    assert viewer["error"]["code"] == "not_found" and gui.of("dispatch") == []
    gui.ask("actor", actor="viewer", grants=USER)
    assert gui.ask("invoke", actor="viewer", capability="services.%s.start" % WEB)["ok"]
    gui.ask("revoke", actor="viewer")
    assert gui.ask("list", actor="viewer")["error"]["code"] == "authorization_denied"
    gui.host.close()


def test_a_dependency_needs_its_own_grant_and_its_own_identity(tmp_path):
    web_only = [{"permission": "services.read"}, {"permission": "services.start", "resource_id": WEB}]
    gui = Gui(tmp_path, rows=[row(WEB, "web", depends_on=[DB]), row(DB, "db"), row(OLD, "old", unmigrated=1)],
              actors={"gui-user": USER, "web-only": web_only})
    denied = gui.ask("invoke", actor="web-only", capability="services.%s.start" % WEB)
    assert denied["error"]["code"] == "authorization_denied"
    assert gui.invoke(resource=OLD)["error"]["code"] == "dependency_unmigrated"
    assert gui.ask("list")["executions"] == [] and gui.of("dispatch") == []
    assert gui.invoke()["ok"]
    gui.host.close()


def test_one_operation_per_service_at_a_time(gui):
    first = gui.invoke()["execution"]["execution_id"]
    busy = gui.invoke(op="stop")
    assert busy["error"] == {"code": "busy", "message": busy["error"]["message"], "execution_id": first}
    assert len(gui.of("dispatch")) == 1
    assert gui.invoke(resource=DB)["ok"]


def test_an_owner_that_does_not_answer_fails_it_and_is_told_to_stop_waiting(gui):
    execution_id = gui.invoke()["execution"]["execution_id"]
    gui.timers.fire(15)
    after = gui.ask("get", execution_id=execution_id)["execution"]
    assert after["state"] == "failed" and after["error"]["code"] == "owner_unavailable"
    assert gui.last("stop_waiting") == {"type": "stop_waiting", "execution_id": execution_id}
    gui.owner(execution_id, "accepted")
    gui.owner(execution_id, "succeeded", output={"service_status": "running", "readiness": "ready"})
    assert gui.state(execution_id) == "failed"


def test_the_deadline_and_cancel_end_waiting_and_never_stop_the_service(gui):
    first = gui.invoke(input={"timeout_seconds": 30})["execution"]["execution_id"]
    gui.owner(first, "accepted")
    gui.timers.fire(45)
    assert gui.state(first) == "timed_out" and gui.last("stop_waiting")["execution_id"] == first

    second = gui.invoke()["execution"]["execution_id"]
    gui.owner(second, "accepted")
    cancelled = gui.ask("cancel", execution_id=second, reason="wrong service")["execution"]
    assert cancelled["state"] == "canceled" and gui.last("stop_waiting")["execution_id"] == second
    assert [d["op"] for d in gui.of("dispatch")] == ["start", "start"], "nothing was stopped"


def test_a_start_that_does_not_wait_gets_a_short_deadline(gui):
    gui.invoke(input={"wait_ready": False})
    assert sorted(t.seconds for t in gui.timers.made) == [15, 30]


def test_the_owner_s_failure_is_the_outcome_with_its_code(gui):
    execution_id = gui.invoke()["execution"]["execution_id"]
    gui.owner(execution_id, "accepted")
    gui.owner(execution_id, "failed", error={"code": "service_failed", "message": "exited with 1"},
              output={"service_status": "failed", "readiness": "not_ready"})
    after = gui.ask("get", execution_id=execution_id)["execution"]
    assert after["state"] == "failed" and after["error"] == {"code": "service_failed", "message": "exited with 1"}
    assert after["output"]["service_status"] == "failed"


def test_an_unusable_owner_result_is_not_a_success(gui):
    execution_id = gui.invoke()["execution"]["execution_id"]
    gui.owner(execution_id, "succeeded", output={"service_status": "up"})
    after = gui.ask("get", execution_id=execution_id)["execution"]
    assert after["state"] == "failed" and after["error"]["code"] == "invalid_output"


def test_bad_lines_are_answered_and_the_host_goes_on(gui):
    for line, code in (("not json", "invalid_message"), ("[1]", "invalid_message"),
                       (json.dumps({"type": "teleport", "id": 1}), "invalid_message"),
                       ("x" * (1024 * 1024 + 1), "too_large")):
        gui.host.feed(line)
        assert gui.out[-1]["error"]["code"] == code
    assert gui.ask("list", filters={"colour": "red"})["error"]["code"] == "invalid_message"
    assert gui.invoke()["ok"]


def test_list_and_events_page_by_cursor(gui):
    first = gui.invoke()["execution"]["execution_id"]
    gui.invoke(resource=DB)
    assert len(gui.ask("list")["executions"]) == 2
    assert [e["execution_id"] for e in gui.ask("list", filters={"resource_id": WEB})["executions"]] == [first]
    page = gui.ask("events", execution_id=first)
    assert gui.ask("events", execution_id=first, after=page["cursor"])["events"] == []


# -- the real process ------------------------------------------------------------
class Process:
    def __init__(self, store):
        self.proc = subprocess.Popen(
            # The agent socket in the test's own folder, never the user's runtime directory.
            [sys.executable, os.path.join(ROOT, "session_launcher.py"), "--qcr-host", "--qcr-project=p",
             "--qcr-store=%s" % store, "--qcr-socket-dir=%s" % os.path.join(os.path.dirname(str(store)), "sock"),
             # The test's own integrations file: the host never reads the user's.
             "--integrations-file=%s" % os.path.join(os.path.dirname(str(store)), "integrations.json")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=ROOT)

    def send(self, message):
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def until(self, test, limit=20):
        end = time.time() + limit
        while time.time() < end:
            line = self.proc.stdout.readline()
            if not line:
                raise AssertionError("host ended: %s" % self.proc.stderr.read())
            message = json.loads(line)          # stdout carries nothing but protocol lines
            if test(message):
                return message
        raise AssertionError("no such message in time")

    def start(self, rows):
        self.send({"type": "snapshot", "snapshot": snapshot(*rows)})
        self.send({"type": "hello", "protocol": 1, "actors": {"gui-user": USER}})
        return self.until(lambda m: m["type"] == "ready")


def test_the_real_host_round_trip_and_a_clean_shutdown(tmp_path):
    host = Process(tmp_path / "e.sqlite")
    assert host.start([row(WEB, "web")])["recovered"] == []
    host.send({"type": "invoke", "id": 1, "actor": "gui-user", "capability": "services.%s.start" % WEB,
               "input": {"wait_ready": False}})
    dispatch = host.until(lambda m: m["type"] == "dispatch")
    host.send({"type": "owner", "execution_id": dispatch["execution_id"], "event": "accepted"})
    host.send({"type": "owner", "execution_id": dispatch["execution_id"], "event": "succeeded",
               "output": {"service_status": "starting", "readiness": "not_checked"}})
    done = host.until(lambda m: m["type"] == "execution" and m["execution"]["state"] == "succeeded")
    assert done["execution"]["output"]["readiness"] == "not_checked"
    host.send({"type": "shutdown"})
    assert host.proc.wait(timeout=20) == 0


def test_a_killed_host_leaves_unknown_not_a_retry(tmp_path):
    store = tmp_path / "e.sqlite"
    host = Process(store)
    host.start([row(WEB, "web")])
    host.send({"type": "invoke", "id": 1, "actor": "gui-user", "capability": "services.%s.start" % WEB})
    dispatch = host.until(lambda m: m["type"] == "dispatch")
    host.send({"type": "owner", "execution_id": dispatch["execution_id"], "event": "accepted"})
    host.until(lambda m: m["type"] == "execution" and m["execution"]["state"] == "dispatched")
    host.proc.send_signal(signal.SIGKILL)
    host.proc.wait(timeout=20)

    again = Process(store)
    ready = again.start([row(WEB, "web", status="running")])
    assert ready["recovered"] == [dispatch["execution_id"]]
    again.send({"type": "get", "id": 2, "actor": "gui-user", "execution_id": dispatch["execution_id"]})
    reply = again.until(lambda m: m["type"] == "reply" and m["id"] == 2)
    assert reply["execution"]["state"] == "unknown" and reply["execution"]["error"]["code"] == "interrupted"
    again.send({"type": "events", "id": 3, "actor": "gui-user", "execution_id": dispatch["execution_id"]})
    events = again.until(lambda m: m["type"] == "reply" and m["id"] == 3)["events"]
    assert events[-1]["payload"] == {"reason": "host_restarted", "observed": {"service_status": "running"}}
    again.send({"type": "list", "id": 4, "actor": "gui-user"})
    listed = again.until(lambda m: m["type"] in ("reply", "dispatch"))
    assert listed["type"] == "reply", "a restart must not dispatch anything by itself"
    again.proc.stdin.close()                       # the GUI went away: end of stdin ends the host
    assert again.proc.wait(timeout=20) == 0


def test_without_its_arguments_the_host_refuses_to_start(tmp_path):
    proc = subprocess.run([sys.executable, os.path.join(ROOT, "session_launcher.py"), "--qcr-host"],
                          capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert proc.returncode == 2 and proc.stdout == "" and "--qcr-project" in proc.stderr
