"""One turn of a conversation, against a stand-in CLI.

The properties that matter are about what is left behind: every event is on
disk before anybody is told of it, a turn that was started always ends with one
line saying how, a turn that could not be started leaves nothing at all, and
the second turn continues the conversation the first one began.

The CLI is ``tests/agent_cli_fixture.py``: no model is asked anything.
"""

import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_cli_fixture import fake_agent, flag, starts              # noqa: E402
from integrations.agents import runner, sessions, store             # noqa: E402

posix_only = pytest.mark.skipif(os.name == "nt", reason="the stand-in CLI is a script")
pytestmark = posix_only


class World:
    """A scratch installation: one connection, one folder, one sessions root."""

    def __init__(self, tmp_path, monkeypatch):
        self.agents = str(tmp_path / "agents.json")
        self.root = str(tmp_path / "agent-sessions")
        self.folder = str(tmp_path / "work")
        os.makedirs(self.folder)
        self.log = tmp_path / "starts.jsonl"
        monkeypatch.setenv("FAKE_AGENT_LOG", str(self.log))
        self.binary = fake_agent(tmp_path)
        self.connection, problems = store.upsert(
            {"name": "Claude", "kind": "claude", "binary": self.binary, "model": "sonnet"},
            self.agents)
        assert problems == []

    def session(self, **more):
        more.setdefault("mode", "read")
        return sessions.create(self.root, self.connection, self.folder, **more)

    def send(self, session, message, **more):
        seen = []
        answer = runner.send(self.root, session["id"], message, agents_path=self.agents,
                             emit=seen.append, **more)
        return answer, seen

    def turn_file(self, session, number, name):
        return os.path.join(sessions.turn_directory(self.root, session["id"], number), name)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


# ------------------------------------------------------------------ a turn taken
def test_a_turn_is_said_answered_and_all_of_it_kept(world):
    session = world.session()
    answer, seen = world.send(session, "Why does the login test fail?")
    turn = answer["turn"]
    assert (turn["n"], turn["state"], turn["error"]) == (1, "completed", "")
    assert (turn["cost_usd"], turn["steps"], turn["mode"], turn["model"]) == (
        0.0125, 2, "read", "sonnet")
    assert turn["ended"] >= turn["started"]
    assert [one["type"] for one in seen] == [
        "user", "started", "thinking", "text", "tool.started", "tool.completed",
        "file.written", "completed"]
    assert seen[6]["path"] == os.path.join(os.path.realpath(world.folder), "notes.md")
    assert seen[0]["text"] == "Why does the login test fail?"
    assert seen[3]["text"] == "You said: Why does the login test fail?"
    assert seen[-1]["text"] == "Done: Why does the login test fail?"
    # What was told is what is on disk, in the same order.
    assert sessions.events(world.root, session["id"]) == seen
    assert [one["seq"] for one in seen] == list(range(1, 9))
    assert open(world.turn_file(session, 1, "prompt.md")).read() == (
        "Why does the login test fail?\n")
    raw = open(world.turn_file(session, 1, "stdout.log")).read().splitlines()
    assert json.loads(raw[0])["subtype"] == "init" and json.loads(raw[-1])["type"] == "result"


def test_attached_files_are_copied_recorded_and_available_on_later_turns(world, tmp_path):
    source = tmp_path / "résumé.txt"
    source.write_text("private data", encoding="utf-8")
    session = world.session()
    answer, events = world.send(session, "Read the report", attachments=[str(source)])
    attachment = events[0]["attachments"][0]
    assert attachment["name"] == "résumé.txt"
    assert answer["turn"]["attachments"] == [attachment]
    copy = os.path.join(sessions.directory(world.root, session["id"]), attachment["path"])
    source.unlink()
    assert open(copy, encoding="utf-8").read() == "private data"
    assert os.stat(copy).st_mode & 0o077 == 0
    assert events[0]["text"] == "Read the report"
    first = starts(world.log)[0]
    assert copy in first["message"]
    assert "private data" not in first["argv"]
    assert flag(first, "--add-dir") == os.path.dirname(os.path.dirname(copy))
    world.send(session, "Read that attachment again")
    assert flag(starts(world.log)[1], "--add-dir") == flag(first, "--add-dir")


def test_attachments_without_a_message_are_a_valid_turn(world, tmp_path):
    source = tmp_path / "picture.png"
    source.write_bytes(b"image fixture")
    session = world.session()
    answer, events = world.send(session, "", attachments=[str(source)])
    assert answer["turn"]["state"] == "completed"
    assert answer["session"]["title"] == "picture.png"
    assert events[0]["text"] == ""
    assert "Please inspect the attached files" in starts(world.log)[0]["message"]


def test_files_with_the_same_name_do_not_overwrite_each_other(world, tmp_path):
    left, right = tmp_path / "a", tmp_path / "b"
    left.mkdir(); right.mkdir()
    (left / "report.txt").write_text("first")
    (right / "report.txt").write_text("second")
    session = world.session()
    _, events = world.send(session, "Compare", attachments=[
        str(left / "report.txt"), str(right / "report.txt")])
    copies = [os.path.join(sessions.directory(world.root, session["id"]), one["path"])
              for one in events[0]["attachments"]]
    assert [open(path).read() for path in copies] == ["first", "second"]


@pytest.mark.parametrize("kind", ["missing", "folder", "fifo", "large", "bad-list"])
def test_invalid_attachments_start_nothing_and_leave_no_partial_copy(world, tmp_path, kind):
    from integrations.agents.attachments import MAX_BYTES
    good = tmp_path / "good.txt"
    good.write_text("data")
    bad = tmp_path / "bad"
    if kind == "folder":
        bad.mkdir()
    elif kind == "fifo":
        os.mkfifo(bad)
    elif kind == "large":
        with open(bad, "wb") as handle:
            handle.truncate(MAX_BYTES + 1)
    paths = "not a list" if kind == "bad-list" else [str(good), str(bad)]
    session = world.session()
    with pytest.raises(runner.Refused) as raised:
        world.send(session, "inspect", attachments=paths)
    assert raised.value.kind == "attachments"
    assert sessions.load(world.root, session["id"])["turns"] == []
    assert not starts(world.log)
    parent = os.path.join(sessions.directory(world.root, session["id"]), "attachments")
    assert not os.path.exists(parent) or os.listdir(parent) == []


def test_attachment_directory_is_mounted_read_only(world, tmp_path, monkeypatch):
    path = tmp_path / "context.txt"
    path.write_text("context")
    captured = []
    original = runner._start
    def start(argv, cwd, env, effective, folders=(), **kwargs):
        captured.extend(folders)
        return original(argv, cwd, env, None, (), **kwargs)
    monkeypatch.setattr(runner, "_start", start)
    session = world.session(restrictions={"files": "read_only"})
    world.send(session, "Read", attachments=[str(path)])
    assert len(captured) == 1 and captured[0]["access"] == "read_only"
    assert captured[0]["path"].endswith("/attachments")


def test_a_write_the_cli_refused_is_not_recorded_as_a_file_written(world):
    """Seen live: in Read mode the agent asked to write, the CLI refused, and
    the record said the file was written."""
    session = world.session()
    _answer, seen = world.send(session, "DENIED please")
    kinds = [one["type"] for one in seen]
    assert "file.written" not in kinds
    completed = seen[kinds.index("tool.completed")]
    assert completed["ok"] is False and "permissions" in completed["detail"]
    # The refusal is one event, the call's own, and says it was a refusal.
    assert "tool.refused" not in kinds
    assert completed["refused"] is True
    assert completed["reason"] == "This command requires approval"


def test_the_session_remembers_the_turn_and_takes_its_title_from_it(world):
    session = world.session()
    answer, _seen = world.send(session, "Why does the login test fail?\nDetails follow.")
    saved = sessions.load(world.root, session["id"])
    assert saved["title"] == "Why does the login test fail?"
    assert saved["state"] == "completed" and saved["cost_usd"] == 0.0125
    assert saved["provider_started"] is True and saved["provider_session"]
    assert len(saved["turns"]) == 1 and saved["updated"] >= saved["created"]
    assert answer["session"]["running"] is False
    assert sessions.held(world.root, session["id"]) is False


def test_everything_a_turn_leaves_is_readable_by_its_owner_only(world):
    """A conversation holds whatever was pasted into it and whatever was read."""
    session = world.session()
    world.send(session, "hello")
    where = sessions.directory(world.root, session["id"])
    seen = []
    for folder, _dirs, files in os.walk(where):
        seen.append(os.path.relpath(folder, where))
        assert os.stat(folder).st_mode & 0o077 == 0, folder
        for name in files:
            seen.append(name)
            assert os.stat(os.path.join(folder, name)).st_mode & 0o077 == 0, name
    assert {"turns", os.path.join("turns", "0001"), "prompt.md", "stdout.log",
            "stderr.log", "events.jsonl", "session.json", ".lock"} <= set(seen)


def test_a_title_somebody_gave_is_kept(world):
    session = world.session(title="Login")
    world.send(session, "Something else entirely")
    assert sessions.load(world.root, session["id"])["title"] == "Login"


def test_the_cli_is_started_in_the_folder_with_the_message_on_stdin(world, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SIMPLE", "1")
    session = world.session()
    world.send(session, "hello there")
    (start,) = starts(world.log)
    assert start["cwd"] == os.path.realpath(world.folder)
    assert start["message"] == "hello there"
    assert "hello there" not in start["argv"]
    assert start["argv"][:4] == ["-p", "--output-format", "stream-json", "--verbose"]
    assert flag(start, "--allowedTools") == "Read Glob Grep"
    assert flag(start, "--model") == "sonnet"
    assert "--permission-mode" not in start["argv"]
    # The CLI's bare mode never reads its own sign-in.
    assert start["simple"] is None


def test_a_long_message_goes_through_whole(world):
    session = world.session()
    message = "line of a pasted log\n" * 20000          # far more than a pipe holds
    answer, _seen = world.send(session, message)
    assert answer["turn"]["state"] == "completed"
    assert starts(world.log)[0]["message"] == message


def test_the_second_turn_continues_the_conversation_the_first_began(world):
    session = world.session()
    world.send(session, "first")
    answer, seen = world.send(session, "second")
    first, second = starts(world.log)
    named = flag(first, "--session-id")
    assert named and "--resume" not in first["argv"]
    assert flag(second, "--resume") == named and "--session-id" not in second["argv"]
    assert answer["turn"]["n"] == 2
    assert seen[0]["seq"] == 9 and all(one["turn"] == 2 for one in seen)
    saved = sessions.load(world.root, session["id"])
    assert saved["cost_usd"] == 0.025 and [one["n"] for one in saved["turns"]] == [1, 2]


def test_mode_model_and_effort_given_with_a_turn_are_the_sessions_from_then_on(world):
    session = world.session()
    world.send(session, "first", mode="edit", model="opus", effort="high")
    world.send(session, "second")
    for start in starts(world.log):
        assert flag(start, "--allowedTools") == "Read Glob Grep Edit Write MultiEdit"
        assert flag(start, "--permission-mode") == "acceptEdits"
        assert (flag(start, "--model"), flag(start, "--effort")) == ("opus", "high")
    saved = sessions.load(world.root, session["id"])
    assert (saved["mode"], saved["model"], saved["effort"]) == ("edit", "opus", "high")


def test_full_mode_adds_the_shell_and_nothing_else(world):
    session = world.session(mode="full")
    world.send(session, "run the tests")
    assert flag(starts(world.log)[0], "--allowedTools") == (
        "Read Glob Grep Edit Write MultiEdit Bash")


def test_a_blank_model_on_the_session_takes_the_connections(world):
    session = world.session(model="opus")
    world.send(session, "first")
    world.send(session, "second", model="")
    first, second = starts(world.log)
    assert (flag(first, "--model"), flag(second, "--model")) == ("opus", "sonnet")


# ------------------------------------------------------------- a turn that fails
def test_a_refusal_from_the_agent_is_a_failed_turn_with_its_words(world):
    session = world.session()
    answer, seen = world.send(session, "REFUSE this")
    assert (answer["turn"]["state"], answer["turn"]["error"]) == ("failed", "Not allowed here.")
    assert [one["type"] for one in seen] == ["user", "started", "failed"]
    assert sessions.load(world.root, session["id"])["state"] == "failed"


def test_a_cli_that_dies_says_what_it_wrote_on_its_way_out(world):
    session = world.session()
    answer, seen = world.send(session, "CRASH now")
    turn = answer["turn"]
    assert turn["state"] == "failed"
    assert "ended without an answer (exit 3)" in turn["error"]
    assert "boom: something broke" in turn["error"]
    assert seen[-1]["type"] == "failed" and seen[-1]["error"] == turn["error"]
    assert "boom" in open(world.turn_file(session, 1, "stderr.log")).read()


def test_a_cli_that_ends_without_a_word_is_a_failure_not_an_answer(world):
    session = world.session()
    answer, _seen = world.send(session, "SILENT")
    assert answer["turn"]["state"] == "failed"
    assert answer["turn"]["error"].endswith("ended without an answer (exit 0).")


def test_a_failed_turn_that_reached_the_cli_is_still_continued_not_restarted(world):
    """It may have been acted on: the conversation it belongs to is the same."""
    session = world.session()
    world.send(session, "CRASH now")
    world.send(session, "and then?")
    first, second = starts(world.log)
    assert flag(second, "--resume") == flag(first, "--session-id")


def test_a_cli_that_cannot_be_started_is_a_failed_turn_on_the_record(world):
    session = world.session()
    os.chmod(world.binary, 0o644)
    monkey = store.get("claude", world.agents)
    assert monkey["binary"] == world.binary
    # Resolved a moment ago, gone when it is started: a failure, on the record.
    original = runner.adapters.get("claude").resolve
    try:
        runner.adapters.get("claude").resolve = lambda binary=None: world.binary
        answer, seen = world.send(session, "hello")
    finally:
        runner.adapters.get("claude").resolve = original
    assert answer["turn"]["state"] == "failed"
    assert answer["turn"]["error"].startswith("Cannot start Claude Code:")
    assert [one["type"] for one in seen] == ["user", "failed"]
    assert sessions.load(world.root, session["id"])["provider_started"] is False


# ---------------------------------------------------- a turn that was never started
def _nothing_recorded(world, session):
    saved = sessions.load(world.root, session["id"])
    return (saved["turns"] == [] and sessions.events(world.root, session["id"]) == []
            and starts(world.log) == [])


def test_nothing_to_say_is_not_a_turn(world):
    session = world.session()
    with pytest.raises(runner.Refused) as caught:
        world.send(session, "   \n ")
    assert caught.value.kind == "invalid" and _nothing_recorded(world, session)


def test_a_cli_that_is_not_installed_is_refused_before_anything_is_recorded(world):
    session = world.session()
    os.unlink(world.binary)
    with pytest.raises(runner.Refused) as caught:
        world.send(session, "hello")
    assert caught.value.kind == "not_installed"
    assert "Claude Code is not installed" in str(caught.value)
    assert _nothing_recorded(world, session)


def test_a_connection_that_is_gone_is_named(world):
    session = world.session()
    store.remove("claude", world.agents)
    with pytest.raises(runner.Refused) as caught:
        world.send(session, "hello")
    assert caught.value.kind == "no_connection" and "'Claude'" in str(caught.value)
    assert _nothing_recorded(world, session)


def test_a_folder_that_is_gone_is_named(world):
    session = world.session()
    os.rmdir(world.folder)
    with pytest.raises(runner.Refused) as caught:
        world.send(session, "hello")
    assert caught.value.kind == "invalid" and world.folder in str(caught.value)


@pytest.mark.parametrize("more, said", [({"mode": "godlike"}, "no 'godlike' mode"),
                                        ({"effort": "extreme"}, "Effort must be one of")])
def test_a_mode_or_effort_the_cli_does_not_have_is_refused(world, more, said):
    session = world.session()
    with pytest.raises(runner.Refused) as caught:
        world.send(session, "hello", **more)
    assert caught.value.kind == "invalid" and said in str(caught.value)
    assert _nothing_recorded(world, session)


def test_a_session_already_answering_is_busy(world):
    session = world.session()
    with sessions.lock(world.root, session["id"]):
        with pytest.raises(runner.Refused) as caught:
            world.send(session, "hello")
    assert caught.value.kind == "busy" and _nothing_recorded(world, session)


def test_a_session_that_is_not_there_is_refused(world):
    with pytest.raises(runner.Refused) as caught:
        runner.send(world.root, "20261111-172345-3fa91c", "hello", agents_path=world.agents)
    assert caught.value.kind == "not_found"


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


def test_stop_ends_the_cli_and_the_turn_is_recorded_as_stopped(world):
    session = world.session()
    stop = threading.Event()
    seen = []

    def emit(event):
        seen.append(event)
        if event["type"] == "thinking":
            stop.set()                  # it is working; the person presses Stop

    began = time.monotonic()
    answer = runner.send(world.root, session["id"], "HANG please", agents_path=world.agents,
                         emit=emit, stop=stop)
    assert time.monotonic() - began < 10
    assert (answer["turn"]["state"], answer["turn"]["error"]) == ("stopped", "")
    assert [one["type"] for one in seen] == ["user", "started", "thinking", "stopped"]
    assert _gone(starts(world.log)[0]["pid"])
    saved = sessions.load(world.root, session["id"])
    assert saved["state"] == "stopped" and sessions.held(world.root, session["id"]) is False
    # And the conversation goes on from where it was.
    again, _seen = world.send(session, "carry on")
    assert again["turn"]["state"] == "completed"
    assert flag(starts(world.log)[1], "--resume") == saved["provider_session"]


# ----------------------------------------------------------------- restrictions
def test_no_restrictions_is_nothing_to_hold(world):
    assert runner.effective_restrictions(world.session()) is None
    assert runner.effective_restrictions(world.session(restrictions={"network": "on"})) is None


@pytest.mark.parametrize("raw", [{"network": "off"}, {"switched_off": ["network"]},
                                 {"network": "isolated"}])
def test_a_folder_without_the_network_is_refused_before_the_cli_hangs_on_it(world, raw):
    """Left to try, the CLI retries for minutes and names no cause."""
    session = world.session(restrictions=raw)
    with pytest.raises(runner.Refused) as caught:
        world.send(session, "hello")
    assert caught.value.kind == "restrictions" and "network" in str(caught.value).lower()
    assert _nothing_recorded(world, session)


def test_restrictions_that_make_no_sense_are_refused_in_their_own_words(world):
    session = world.session(restrictions={"files": "sideways"})
    with pytest.raises(runner.Refused) as caught:
        world.send(session, "hello")
    assert caught.value.kind == "restrictions"


def test_a_restricted_folder_starts_the_cli_through_the_launcher(world, monkeypatch):
    from execution_runtime.backends.linux import restricted
    asked, closed = [], []

    class Prepared:
        pass_fds = ()

        def __init__(self, argv, cwd, env):
            self.argv, self.cwd = argv, cwd
            self.env = dict(env, FAKE_SANDBOXED="yes")

        def close(self):
            closed.append(True)

    class Launcher:
        def prepare(self, effective, argv, cwd, env, extra):
            asked.append((effective["files"], effective["project_folder"], argv[0], cwd,
                          [one["path"] for one in extra]))
            return Prepared(argv, cwd, env)

    monkeypatch.setattr(restricted, "launcher", lambda: Launcher())
    session = world.session(restrictions={"files": "read_only"})
    answer, _seen = world.send(session, "hello")
    assert answer["turn"]["state"] == "completed"
    ((files, project, program, cwd, extra),) = asked
    assert (files, project, cwd) == ("read_only", world.folder, world.folder)
    assert os.path.realpath(program) == os.path.realpath(world.binary)
    # The CLI's own code is outside the folder, so it is granted to start at all.
    assert os.path.dirname(world.binary) in extra
    assert starts(world.log)[0]["sandboxed"] == "yes"
    assert closed == [True]


# ------------------------------------------------------------------------ skills
def _library(tmp_path, *names):
    """A skills library with these skills in it; returns its path."""
    root = tmp_path / "agent-skills"
    for name in names:
        (root / name / "scripts").mkdir(parents=True)
        (root / name / "SKILL.md").write_text(
            "---\nname: %s\ndescription: What %s is for.\n---\n" % (name, name))
        (root / name / "scripts" / "check.py").write_text("print('ok')\n")
    return str(root)


def test_a_turn_is_handed_the_skills_chosen_for_it_as_a_copy_of_its_own(world, tmp_path):
    library = _library(tmp_path, "odoo-review", "test-writer")
    session = world.session()
    answer, seen = world.send(session, "review it", skills=["odoo-review", "test-writer"],
                              skills_root=library)
    (start,) = starts(world.log)
    staged = os.path.join(sessions.directory(world.root, session["id"]), "skills")
    assert flag(start, "--plugin-dir") == staged
    # The tool that reads a skill is allowed with the rest, or it would be refused.
    assert flag(start, "--allowedTools") == "Read Glob Grep Skill"
    assert sorted(os.listdir(os.path.join(staged, "skills"))) == ["odoo-review", "test-writer"]
    assert os.path.isfile(os.path.join(staged, "skills", "odoo-review", "scripts", "check.py"))
    manifest = json.load(open(os.path.join(staged, ".claude-plugin", "plugin.json")))
    assert manifest["name"] == "qavector"
    # A copy: neither the library nor the folder the agent works in is touched.
    assert not os.path.islink(os.path.join(staged, "skills", "odoo-review"))
    assert os.listdir(world.folder) == []
    assert answer["turn"]["skills"] == ["odoo-review", "test-writer"]
    assert seen[0]["skills"] == ["odoo-review", "test-writer"]


def test_skills_given_with_a_turn_are_the_conversation_s_until_changed(world, tmp_path):
    library = _library(tmp_path, "odoo-review", "test-writer")
    session = world.session()
    world.send(session, "first", skills=["odoo-review"], skills_root=library)
    assert sessions.load(world.root, session["id"])["skills"] == ["odoo-review"]
    world.send(session, "second", skills_root=library)
    world.send(session, "third", skills=[], skills_root=library)
    first, second, third = starts(world.log)
    assert flag(first, "--plugin-dir") == flag(second, "--plugin-dir") is not None
    assert "--plugin-dir" not in third["argv"]
    assert flag(third, "--allowedTools") == "Read Glob Grep"
    saved = sessions.load(world.root, session["id"])
    assert saved["skills"] == []
    assert [turn["skills"] for turn in saved["turns"]] == [["odoo-review"], ["odoo-review"], []]
    # Nothing of a skill no longer chosen is left where the CLI would find it.
    assert not os.path.exists(os.path.join(sessions.directory(world.root, session["id"]),
                                           "skills"))


def test_a_turn_takes_the_skill_as_the_library_has_it_now(world, tmp_path):
    library = _library(tmp_path, "odoo-review")
    session = world.session(skills=["odoo-review"])
    world.send(session, "first", skills_root=library)
    with open(os.path.join(library, "odoo-review", "SKILL.md"), "a") as handle:
        handle.write("Updated since.\n")
    world.send(session, "second", skills_root=library)
    staged = os.path.join(sessions.directory(world.root, session["id"]), "skills", "skills",
                          "odoo-review", "SKILL.md")
    assert "Updated since." in open(staged).read()


def test_a_skill_the_library_no_longer_has_is_said_before_anything_is_recorded(world,
                                                                             tmp_path):
    library = _library(tmp_path, "odoo-review")
    session = world.session(skills=["odoo-review", "gone"])
    with pytest.raises(runner.Refused) as caught:
        world.send(session, "hello", skills_root=library)
    assert caught.value.kind == "skills" and "'gone'" in str(caught.value)
    assert _nothing_recorded(world, session)


def test_a_restricted_turn_is_shown_its_skills_read_only_and_nothing_of_the_library(
        world, tmp_path, monkeypatch):
    from execution_runtime.backends.linux import restricted
    asked = []

    class Prepared:
        pass_fds = ()

        def __init__(self, argv, cwd, env):
            self.argv, self.cwd, self.env = argv, cwd, env

        def close(self):
            pass

    class Launcher:
        def prepare(self, effective, argv, cwd, env, extra):
            asked.append([(one["path"], one["access"]) for one in extra])
            return Prepared(argv, cwd, env)

    monkeypatch.setattr(restricted, "launcher", lambda: Launcher())
    library = _library(tmp_path, "odoo-review")
    session = world.session(restrictions={"files": "read_only"}, skills=["odoo-review"])
    world.send(session, "hello", skills_root=library)
    staged = os.path.join(sessions.directory(world.root, session["id"]), "skills")
    assert (staged, "read_only") in asked[0]
    assert not any(path.startswith(library) for path, _access in asked[0])


def test_restrictions_this_computer_cannot_enforce_fail_the_turn_not_run_it_open(
        world, monkeypatch):
    from execution_runtime.backends.linux import restricted
    from execution_runtime.errors import RuntimePolicyError

    class Launcher:
        def prepare(self, *_args):
            raise RuntimePolicyError("restrictions_unavailable", "No sandbox on this computer.")

    monkeypatch.setattr(restricted, "launcher", lambda: Launcher())
    session = world.session(restrictions={"files": "read_only"})
    answer, _seen = world.send(session, "hello")
    assert answer["turn"]["state"] == "failed"
    assert "No sandbox on this computer." in answer["turn"]["error"]
    assert starts(world.log) == []
