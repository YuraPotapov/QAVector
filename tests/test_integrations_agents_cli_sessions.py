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
from pathlib import Path

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_cli_fixture import fake_agent, starts                    # noqa: E402
from codex_cli_fixture import fake_codex                             # noqa: E402
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


@pytest.fixture(autouse=True)
def _track_changes(monkeypatch):
    """These tests cover what a turn changed, off by default in the app."""
    monkeypatch.setenv("QAVECTOR_TRACK_CHANGES", "1")


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


def test_send_hears_follow_ups_on_stdin_only_when_asked(agents, monkeypatch):
    import io
    session = agents.new()
    said = json.dumps({"say": {"id": "f-1", "message": "and the tests too"}}) + "\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(said))
    code, lines = agents.lines("send", session["id"], document={"message": "hello"})
    assert code == 0 and sys.stdin.tell() == 0                  # not asked: stdin left alone
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json\n" + said))
    code, lines = agents.lines("send", session["id"], document={"message": "FOLLOW", "follow_ups": True})
    assert code == 0 and lines[-1]["ok"] and len(starts(agents.log)) == 2
    followed = [line["event"] for line in lines if line.get("event", {}).get("follow_up")]
    assert [(one["id"], one["text"]) for one in followed] == [("f-1", "and the tests too")]
    assert lines[-2]["event"]["text"] == "Done: and the tests too"


def test_tool_patch_reads_the_requested_operation_from_the_saved_turn(agents):
    code, saved = agents("save", document={"connection": {
        "name": "Codex", "kind": "codex", "binary": fake_codex(agents.tmp)}})
    assert code == 0
    session = agents.new(connection=saved["connection"]["id"])
    session_id = session["id"]
    code, _ = agents.lines("send", session_id, document={"message": "hello"})
    assert code == 0
    recorded = Path(agents.sessions) / session_id / "turns" / "0001" / "stdout.log"
    first = {"type": "fileChange", "id": "first", "status": "completed",
             "changes": [{"path": "a.py", "diff": "-before\n+first\n"}]}
    second = dict(first, id="second", changes=[{"path": "a.py", "diff": "-first\n+second\n"}])
    records = [{"method": "item/completed", "params": {"item": item}} for item in (first, second)]
    recorded.write_text("\n".join(map(json.dumps, records)), encoding="utf-8")
    code, answer = agents("tool-patch", session_id, document={"turn": 1, "item": "first"})
    assert code == 0 and answer["files"] == first["changes"]
    code, answer = agents("tool-patch", session_id, document={"turn": 1, "item": "missing"})
    assert code == 1 and answer["kind"] == "patch_unavailable"


def test_tool_patch_reads_claude_edits_and_created_files(agents):
    session_id = agents.new()["id"]
    code, _ = agents.lines("send", session_id, document={"message": "hello"})
    assert code == 0
    recorded = Path(agents.sessions) / session_id / "turns" / "0001" / "stdout.log"
    patch = [{"oldStart": 2, "oldLines": 1, "newStart": 2, "newLines": 1,
              "lines": ["-old", "+new"]}]
    records = []
    for item_id, result, failed in (
            ("edit", {"filePath": "a.py", "structuredPatch": patch}, False),
            ("create", {"filePath": "b.py", "type": "create", "structuredPatch": [], "content": "new\n"}, False),
            ("failed", {"filePath": "a.py", "structuredPatch": patch}, True)):
        records.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": item_id, "is_error": failed}]}, "tool_use_result": result})
    recorded.write_text("\n".join(map(json.dumps, records)), encoding="utf-8")

    code, answer = agents("tool-patch", session_id, document={"turn": 1, "item": "edit"})
    assert code == 0 and answer["files"] == [{"path": "a.py", "diff": "@@ -2,1 +2,1 @@\n-old\n+new\n"}]
    code, answer = agents("tool-patch", session_id, document={"turn": 1, "item": "create"})
    assert code == 0 and answer["files"] == [{"path": "b.py", "diff": "+new\n"}]
    code, answer = agents("tool-patch", session_id, document={"turn": 1, "item": "failed"})
    assert code == 1 and answer["kind"] == "patch_unavailable"


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
        "file.written", "changes", "completed"]
    assert answer["ok"] is True and answer["turn"]["state"] == "completed"
    assert answer["session"]["title"] == "hello" and answer["session"]["running"] is False


def test_the_conversation_is_read_back_and_from_where_a_page_left_off(agents):
    session = agents.new()
    agents.lines("send", session["id"], document={"message": "hello"})
    code, answer = agents("session", session["id"])
    assert code == 0 and len(answer["events"]) == 9
    assert answer["session"]["turns"][0]["state"] == "completed"
    code, answer = agents("session", session["id"], document={"after": 6})
    assert [one["seq"] for one in answer["events"]] == [7, 8, 9]
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


def test_a_conversation_opened_for_a_task_is_found_by_its_ref_and_carried_on(agents):
    """Whoever opens a conversation for something of its own - a task handed
    over - gives a ref, and finds that thing's conversations by it alone."""
    task = agents.new(title="Task", source={"label": "Jira QA-1", "detail": "Login fails",
                                            "ref": "jira:work:QA-1"})
    agents.new(title="Chat")
    assert task["source"] == {"label": "Jira QA-1", "detail": "Login fails",
                              "ref": "jira:work:QA-1", "record": False}
    assert task["record"] is False
    code, lines = agents.lines("send", task["id"], document={"message": "go"})
    assert code == 0 and lines[-1]["ok"]
    code, answer = agents("sessions", document={"ref": "jira:work:QA-1"})
    assert code == 0 and [one["id"] for one in answer["sessions"]] == [task["id"]]
    (only,) = answer["sessions"]
    assert only["tokens"]["output"] == 30 and only["context"]["window"] == 200000
    code, answer = agents("activity", document={"ref": "jira:work:QA-1"})
    assert [one["session"] for one in answer["activity"]] == [task["id"]]
    assert agents("sessions")[1]["total"] == 2


def test_what_a_turn_changed_in_the_folder_is_kept_and_read_back(agents):
    """Each turn says what it changed just before it says it ended; the patch,
    the turn's and the conversation's so far, is read back without git."""
    session = agents.new()
    code, lines = agents.lines("send", session["id"], document={"message": "one"})
    events = [line["event"] for line in lines[:-1]]
    assert [one["type"] for one in events][-2:] == ["changes", "completed"]
    assert {key: events[-2][key] for key in ("files", "added", "removed", "kept")} == {
        "files": 1, "added": 1, "removed": 0, "kept": "all"}
    assert lines[-1]["turn"]["changes"]["files"] == 1
    agents.lines("send", session["id"], document={"message": "two"})
    code, answer = agents("changes", session["id"])
    assert code == 0 and [one["path"] for one in answer["files"]] == ["notes.md"]
    assert answer["added"] == 2 and "+x" in answer["patch"]
    code, answer = agents("changes", session["id"], document={"turn": 2})
    assert (answer["added"], answer["turn"]) == (1, 2)
    code, answer = agents("changes", session["id"], document={"turn": 3})
    assert code == 1 and answer["kind"] == "invalid"


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
                                               "detail": "Run 7", "ref": "", "record": False}
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
        "user", "started", "thinking", "changes", "stopped"]
    assert (lines[-1]["ok"], lines[-1]["kind"]) == (False, "stopped")
    assert lines[-1]["turn"]["state"] == "stopped"
    assert _gone(starts(agents.log)[0]["pid"])
    code, answer = agents("session", session["id"])
    assert (answer["session"]["state"], answer["session"]["running"]) == ("stopped", False)


def test_continue_across_providers_then_join_every_branch_once(agents, tmp_path):
    from pathlib import Path
    from codex_cli_fixture import fake_codex

    code, _ = agents("save", document={"connection": {
        "name": "Codex", "kind": "codex", "binary": fake_codex(tmp_path)}})
    assert code == 0
    original = agents.new(title="Original", model="sonnet")
    attachment = tmp_path / "example.txt"
    attachment.write_text("context attachment")
    code, _ = agents.lines("send", original["id"], document={"message": "original request",
                                                             "attachments": [str(attachment)]})
    assert code == 0
    follow = agents.new(previous=original["id"])
    code, switched = agents("session-set", follow["id"], document={"connection": "codex", "model": "chosen-codex"})
    assert code == 0 and switched["session"]["kind"] == "codex"
    follow = switched["session"]
    assert follow["lineage"]["previous"] == original["id"] and not follow["provider_session"]
    code, turns = agents.lines("send", follow["id"], document={"message": "continue from there"})
    assert code == 0 and turns[-1]["turn"]["model"] == "chosen-codex"
    code, refusal = agents("session-set", follow["id"], document={"connection": "claude"})
    assert code != 0 and refusal["kind"] == "invalid"
    base = Path(agents.sessions) / follow["id"]
    history = base / "attachments" / "chat-history"
    assert str(history / "index.json") in (base / "turns/0001/prompt.md").read_text()
    previous_events = [json.loads(line) for line in (history / original["id"] / "events.jsonl").read_text().splitlines()]
    assert previous_events[0]["text"] == "original request"
    assert (history / original["id"] / previous_events[0]["attachments"][0]["path"]).read_text() == "context attachment"
    branch = agents.new(previous=original["id"], model="opus")
    code, _ = agents.lines("send", branch["id"], document={"message": "alternative approach"})
    assert code == 0
    joined = agents.new(previous=follow["id"], join=True, model="other-model")
    def visible():
        return [item["id"] for item in agents("sessions", document={"sidebar": True})[1]["sessions"]]
    assert visible() == [joined["id"]]
    originals = {original["id"], follow["id"], branch["id"]}
    assert {entry["id"] for entry in joined["lineage"]["sources"]} == originals
    assert not joined["turns"] and joined["cost_usd"] == 0
    for sid in originals:
        assert agents("session", sid)[0] == 0
    code, transcript = agents("session", joined["id"])
    events = transcript["events"]
    assert {e["id"] for e in events if e["type"] == "history.session"} == originals
    assert [e["text"] for e in events if e["type"] == "user"] == [
        "original request", "continue from there", "alternative approach"]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert agents("session", joined["id"], document={"limit": 3})[1]["events"] == events[-3:]
    code, rows = agents.lines("send", joined["id"], document={"message": "joined next step"})
    assert code == 0 and rows[0]["event"]["seq"] == len(events) + 1
    after = agents("session", joined["id"], document={"after": events[-1]["seq"]})[1]["events"]
    assert after[0]["text"] == "joined next step"
    later = agents.new(previous=joined["id"], connection="codex")
    rejoined = agents.new(previous=later["id"], join=True)
    assert visible() == [rejoined["id"]]
    events = agents("session", rejoined["id"])[1]["events"]
    assert [e["text"] for e in events if e["type"] == "user"].count("original request") == 1
    assert [e["text"] for e in events if e["type"] == "user"].count("joined next step") == 1
    assert agents("session-delete", rejoined["id"])[0] == 0
    assert set(visible()) == {joined["id"], later["id"]}


def test_join_uses_every_member_beyond_the_sidebar_page_and_keeps_unrelated_chats(agents):
    original = agents.new(title="Related")
    other = agents.new(title="Unrelated")
    for _ in range(201):
        agents.new(previous=original["id"])
    code, page = agents("sessions", document={"sidebar": True, "limit": 200})
    assert code == 0 and page["more"] and len(page["sessions"]) == 200
    latest = page["sessions"][0]
    assert latest["can_join"] and latest["continuation_group"]
    joined = agents.new(previous=latest["id"], join=True)
    assert len(joined["lineage"]["sources"]) == 202
    code, page = agents("sessions", document={"sidebar": True})
    assert code == 0 and {s["id"] for s in page["sessions"]} == {joined["id"], other["id"]}
    assert not next(s for s in page["sessions"] if s["id"] == joined["id"])["can_join"]
    assert agents("session-delete", joined["id"])[0] == 0
    code, restored = agents("sessions", document={"sidebar": True})
    assert code == 0 and len(restored["sessions"]) == 203
    assert original["id"] in {s["id"] for s in restored["sessions"]}
