"""``--integrations agents`` for conversations: what a chat page is answered.

A turn is the one command that prints as it goes - a line per event and the
answer as the last line - and the one a page stops by ending the process. Both
are tested the way a page uses them: read line by line, and signalled.

The CLI is ``tests/agent_cli_fixture.py``: no model is asked anything.
"""

import json
import os
import signal
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_cli_fixture import fake_agent, starts                    # noqa: E402
from integrations import cli                                        # noqa: E402

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the stand-in CLI is a script")


class Agents:
    """``agents ...`` against a scratch installation with one connection."""

    def __init__(self, tmp_path, capsys, monkeypatch):
        self.tmp, self.capsys = tmp_path, capsys
        self.file = str(tmp_path / "agents.json")
        self.sessions = str(tmp_path / "agent-sessions")
        self.folder = str(tmp_path / "work")
        os.makedirs(self.folder)
        self.log = tmp_path / "starts.jsonl"
        monkeypatch.setenv("FAKE_AGENT_LOG", str(self.log))
        code, answer = self("save", document={"connection": {
            "name": "Claude", "kind": "claude", "binary": fake_agent(tmp_path)}})
        assert code == 0, answer

    def argv(self, *words, document=None):
        argv = ["agents", *words, "--agents-file=" + self.file,
                "--agent-sessions-dir=" + self.sessions]
        if document is not None:
            source = self.tmp / ("from-%d.json" % time.monotonic_ns())
            source.write_text(json.dumps(document), encoding="utf-8")
            argv.append("--from=%s" % source)
        return argv

    def lines(self, *words, document=None):
        """``(exit code, every line printed)`` - for the command that streams."""
        code = cli.main(self.argv(*words, document=document))
        return code, [json.loads(line) for line in
                      self.capsys.readouterr().out.splitlines() if line.strip()]

    def __call__(self, *words, document=None):
        code = cli.main(self.argv(*words, document=document))
        return code, json.loads(self.capsys.readouterr().out)

    def new(self, **more):
        code, answer = self("session-new", document=dict(
            {"connection": "claude", "folder": self.folder}, **more))
        assert code == 0, answer
        return answer["session"]


@pytest.fixture
def agents(tmp_path, capsys, monkeypatch):
    return Agents(tmp_path, capsys, monkeypatch)


# ------------------------------------------------------------------ sessions
def test_a_session_is_started_without_asking_the_agent_anything(agents):
    session = agents.new(title="Login", model="opus")
    assert (session["mode"], session["model"], session["title"]) == ("read", "opus", "Login")
    assert (session["running"], session["state"], session["turns"]) == (False, "", [])
    assert starts(agents.log) == []
    code, answer = agents("sessions")
    assert code == 0 and answer["path"] == agents.sessions
    assert [one["id"] for one in answer["sessions"]] == [session["id"]]


def test_send_accepts_files_and_history_keeps_their_metadata(agents):
    source = agents.tmp / "report.txt"
    source.write_text("fixture content")
    session = agents.new()
    code, lines = agents.lines("send", session["id"], document={
        "message": "", "attachments": [str(source)]})
    assert code == 0 and lines[-1]["ok"]
    attached = lines[0]["event"]["attachments"]
    assert attached[0]["name"] == "report.txt"
    source.unlink()
    code, history = agents("session", session["id"])
    assert code == 0 and history["events"][0]["attachments"] == attached
    assert os.path.isfile(os.path.join(agents.sessions, session["id"], attached[0]["path"]))


@pytest.mark.parametrize("document, kind, said", [
    ({"connection": "ghost"}, "no_connection", "No agent connection 'ghost'."),
    ({"mode": "godlike"}, "invalid", "Mode must be one of read, edit, full."),
    ({"effort": "extreme"}, "invalid", "Effort must be one of"),
    ({"folder": "/no/such/folder/anywhere"}, "invalid", "is not a folder"),
    ({"restrictions": {"files": "sideways"}}, "invalid", "Files is one of"),
])
def test_a_session_that_cannot_be_is_refused_with_the_reason(agents, document, kind, said):
    code, answer = agents("session-new", document=dict(
        {"connection": "claude", "folder": agents.folder}, **document))
    assert (code, answer["kind"]) == (1, kind)
    assert said in answer["error"]
    assert agents("sessions")[1]["total"] == 0


def test_what_a_session_is_set_to_can_be_changed_one_thing_at_a_time(agents):
    session = agents.new(model="opus")
    code, answer = agents("session-set", session["id"], document={"title": "Renamed"})
    assert code == 0
    assert (answer["session"]["title"], answer["session"]["model"]) == ("Renamed", "opus")
    code, answer = agents("session-set", session["id"], document={
        "mode": "full", "effort": "high", "restrictions": {"files": "read_write"}})
    changed = answer["session"]
    assert (changed["mode"], changed["effort"], changed["title"]) == ("full", "high", "Renamed")
    assert changed["restrictions"] == {"files": "read_write"}
    code, answer = agents("session-set", session["id"], document={"mode": "godlike"})
    assert (code, answer["kind"]) == (1, "invalid")


def test_the_folder_can_change_until_the_conversation_begins(agents, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    session = agents.new()
    code, answer = agents("session-set", session["id"], document={"folder": str(other)})
    assert code == 0 and answer["session"]["folder"] == str(other)
    agents.lines("send", session["id"], document={"message": "hello"})
    code, answer = agents("session-set", session["id"], document={"folder": agents.folder})
    assert (code, answer["kind"]) == (1, "invalid")
    assert "cannot change once the conversation has begun" in answer["error"]


def test_a_session_is_deleted_with_everything_kept_of_it(agents):
    session = agents.new()
    assert agents("session-delete", session["id"]) == (0, {"ok": True, "id": session["id"]})
    code, answer = agents("session", session["id"])
    assert (code, answer["kind"]) == (1, "not_found")
    code, answer = agents("session-delete", "../../etc")
    assert (code, answer["kind"]) == (1, "invalid")


# ---------------------------------------------------------------------- a turn
def test_a_turn_prints_a_line_per_event_and_its_answer_last(agents):
    session = agents.new()
    code, lines = agents.lines("send", session["id"], document={"message": "hello"})
    assert code == 0
    *events, answer = lines
    assert all(set(line) == {"event"} for line in events)
    assert [line["event"]["type"] for line in events] == [
        "user", "started", "thinking", "text", "tool.started", "tool.completed",
        "file.written", "completed"]
    assert answer["ok"] is True and answer["turn"]["state"] == "completed"
    assert answer["session"]["title"] == "hello" and answer["session"]["running"] is False


def test_the_conversation_is_read_back_and_from_where_a_page_left_off(agents):
    session = agents.new()
    agents.lines("send", session["id"], document={"message": "hello"})
    code, answer = agents("session", session["id"])
    assert code == 0 and len(answer["events"]) == 8
    assert answer["session"]["turns"][0]["state"] == "completed"
    code, answer = agents("session", session["id"], document={"after": 6})
    assert [one["seq"] for one in answer["events"]] == [7, 8]
    code, answer = agents("session", session["id"], document={"limit": 1})
    assert [one["type"] for one in answer["events"]] == ["completed"]


def test_a_failed_turn_answers_with_how_it_failed_and_exit_1(agents):
    session = agents.new()
    code, lines = agents.lines("send", session["id"], document={"message": "REFUSE"})
    assert code == 1
    assert (lines[-1]["ok"], lines[-1]["kind"], lines[-1]["error"]) == (
        False, "failed", "Not allowed here.")
    assert lines[-1]["turn"]["n"] == 1


def test_a_turn_that_was_not_started_is_one_line_saying_why(agents):
    session = agents.new()
    code, lines = agents.lines("send", session["id"], document={"message": "  "})
    assert code == 1 and len(lines) == 1
    assert (lines[0]["kind"], lines[0]["error"]) == ("invalid", "There is nothing to send.")
    code, lines = agents.lines("send", "20261111-172345-3fa91c", document={"message": "x"})
    assert (code, lines[0]["kind"]) == (1, "not_found")


def test_the_log_lists_every_turn_with_where_and_who(agents):
    first = agents.new(title="First")
    second = agents.new(title="Second", source={"label": "Cycle nightly · step review",
                                                "detail": "Run 7"})
    agents.lines("send", first["id"], document={"message": "one"})
    agents.lines("send", second["id"], document={"message": "REFUSE"})
    agents.lines("send", first["id"], document={"message": "two", "mode": "edit"})
    code, answer = agents("activity")
    assert code == 0
    assert [(one["title"], one["n"], one["state"], one["mode"]) for one in answer["activity"]] == [
        ("First", 2, "completed", "edit"), ("Second", 1, "failed", "read"),
        ("First", 1, "completed", "read")]
    assert answer["activity"][1]["source"] == {"label": "Cycle nightly · step review",
                                               "detail": "Run 7"}
    assert answer["activity"][0]["folder"] == agents.folder
    assert len(agents("activity", document={"limit": 1})[1]["activity"]) == 1


@pytest.mark.parametrize("words", [("session",), ("send",), ("session-new",),
                                   ("session-set", "20261111-172345-3fa91c"),
                                   ("session-delete",)])
def test_a_wrong_command_line_is_answered_with_the_usage(agents, words):
    code = cli.main(agents.argv(*words))
    answer = json.loads(agents.capsys.readouterr().out)
    assert (code, answer["kind"]) == (2, "usage")
    assert "session-new --from=F" in answer["usage"]


# ------------------------------------------------------------------------- stop
def _gone(pid, within=5.0):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.05)
    return False


def test_ending_the_process_stops_the_agent_and_the_turn_is_still_answered(agents):
    """How a page's Stop works: SIGTERM to the core, which ends the CLI, records
    the turn as stopped and says so on its way out."""
    session = agents.new()
    argv = agents.argv("send", session["id"], document={"message": "HANG please"})
    process = subprocess.Popen(
        [sys.executable, "-c",
         "import sys; from integrations.cli import main; sys.exit(main(sys.argv[1:]))", *argv],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines = []
    try:
        for line in process.stdout:
            lines.append(json.loads(line))
            if lines[-1].get("event", {}).get("type") == "thinking":
                process.send_signal(signal.SIGTERM)      # the person presses Stop
                break
        out, err = process.communicate(timeout=20)
    finally:
        if process.poll() is None:
            process.kill()
    lines += [json.loads(line) for line in out.splitlines() if line.strip()]
    assert process.returncode == 1, err
    assert [line["event"]["type"] for line in lines[:-1]] == [
        "user", "started", "thinking", "stopped"]
    assert (lines[-1]["ok"], lines[-1]["kind"]) == (False, "stopped")
    assert lines[-1]["turn"]["state"] == "stopped"
    assert _gone(starts(agents.log)[0]["pid"])
    code, answer = agents("session", session["id"])
    assert (answer["session"]["state"], answer["session"]["running"]) == ("stopped", False)
