"""The Claude Code adapter: what moved down from the cycle, and what the move added.

What the adapter does with a binary and with a stream is tested where it always
was - ``test_cycle_plugins_agent_cli.py`` and ``test_cycle_agent_stream.py``
reach it through the cycle's two thin modules, unchanged, which is the proof
that moving it changed nothing. Here is only what is new: who starts the
process, a caller recognising its own kind of answer, and which way the
dependency points.

A real ``claude`` is never run.
"""

import json
import os
import stat
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.agents import adapters                           # noqa: E402
from integrations.agents.adapters import claude                    # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_model_discovery_timeout_returns_a_failure_without_fallback(monkeypatch):
    monkeypatch.setattr(claude, "resolve", lambda binary: "/fixture/claude")
    def timeout(argv, **kwargs):
        assert "--safe-mode" in argv and kwargs["timeout"] == 0.1
        raise subprocess.TimeoutExpired(argv, 0.1)
    result = claude.models(timeout=0.1, run=timeout)
    assert not result["ok"] and result["models"] == []


def test_model_discovery_ignores_unrelated_frames_and_deduplicates(monkeypatch):
    monkeypatch.setattr(claude, "resolve", lambda binary: "/fixture/claude")
    def answer(argv, **kwargs):
        request = json.loads(kwargs["input"])
        payload = {"type": "control_response", "response": {
            "request_id": request["request_id"], "subtype": "success", "response": {
                "models": [None, {"value": ""}, {"value": "sonnet", "displayName": "Sonnet"},
                           {"value": "sonnet"}]}}}
        raw = ('noise\n[]\n' + json.dumps(payload)).encode()
        return subprocess.CompletedProcess(argv, 0, stdout=raw, stderr=b"")
    assert claude.models(run=answer) == {"ok": True, "models": [{
        "value": "sonnet", "label": "Sonnet", "description": "", "resolved_model": ""}]}


def _cli(directory, body):
    """A script standing in for the CLI."""
    path = os.path.join(str(directory), "claude")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!%s\n%s\n" % (sys.executable, body))
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


def _assistant(*blocks):
    return json.dumps({"type": "assistant", "message": {"content": list(blocks)}})


# ------------------------------------------------------------------ the kinds
def test_the_kinds_are_listed_with_where_each_cli_is(tmp_path, monkeypatch):
    path = _cli(tmp_path, "pass")
    monkeypatch.setenv("PATH", str(tmp_path))
    (found,) = [one for one in adapters.describe() if one["kind"] == "claude"]
    assert found["name"] == "Claude Code"
    assert found["found"] == path
    assert found["efforts"] == ["low", "medium", "high", "xhigh", "max"]


def test_listing_the_kinds_starts_nothing(tmp_path, monkeypatch):
    """Cheap enough for a form to ask every time it opens."""
    marker = tmp_path / "ran"
    _cli(tmp_path, "open(%r, 'w').close()" % str(marker))
    monkeypatch.setenv("PATH", str(tmp_path))
    adapters.describe()
    assert not marker.exists()


def test_a_kind_nobody_wrote_an_adapter_for_is_refused_by_name():
    try:
        adapters.get("telepathy")
    except adapters.UnknownKind as exc:
        assert "telepathy" in str(exc) and "claude" in str(exc)
    else:
        raise AssertionError("an unknown kind was loaded")


# --------------------------------------------------------- who starts the CLI
def test_the_caller_may_start_the_process_itself(tmp_path):
    """A cycle step's processes start inside that step's sandbox, and the
    question of who is signed in is one of them."""
    path = _cli(tmp_path, "pass")
    started = []

    def run(argv, **kwargs):
        started.append((argv, sorted(kwargs)))
        return subprocess.CompletedProcess(argv, 0, json.dumps(
            {"loggedIn": True, "email": "someone@example.invalid"}).encode(), b"")

    found = claude.status(path, run=run)
    assert started == [([path, "auth", "status", "--json"],
                        ["capture_output", "cwd", "env", "stdin", "timeout"])]
    assert found["logged_in"] is True and found["email"] == "someone@example.invalid"


def test_the_cycle_asks_from_inside_its_restrictions(tmp_path, monkeypatch):
    from cycle import restrict
    from cycle.plugins import agent_cli
    path = _cli(tmp_path, "pass")
    asked = []

    def run(argv, **kwargs):
        asked.append(argv)
        return subprocess.CompletedProcess(argv, 0, b'{"loggedIn": false}', b"")

    monkeypatch.setattr(restrict, "run", run)
    assert agent_cli.auth_status(path)["logged_in"] is False
    assert asked == [[path, "auth", "status", "--json"]]


def test_not_signed_in_is_told_apart_from_could_not_be_asked(tmp_path):
    """One is fixed by signing in; the other is not, and offering a sign-in
    for it would send somebody round in a circle."""
    said_no = claude.status(_cli(tmp_path, "print('{\"loggedIn\": false}')"))
    assert (said_no["answered"], said_no["logged_in"]) == (True, False)
    other = tmp_path / "old"
    other.mkdir()
    garbled = claude.status(_cli(other, "print('usage: claude [options]')"))
    assert (garbled["answered"], garbled["logged_in"]) == (False, False)
    assert garbled["problem"]


# ------------------------------------------------- a caller's own kind of answer
def test_a_caller_can_show_its_own_row_for_what_the_agent_said():
    seen = []

    def on_text(text):
        seen.append(text)
        return claude.stage("verdict", "Approved", detail=claude.clean(text))

    (row,) = claude.stages(_assistant({"type": "text", "text": "ship it"}), on_text=on_text)
    assert seen == ["ship it"]
    assert (row["kind"], row["title"], row["detail"]) == ("verdict", "Approved", "ship it")


def test_it_is_handed_the_whole_text_not_the_rows_cut_of_it():
    """Half an answer parses as nothing."""
    long = "x" * (claude.DETAIL_CHARS * 3)
    seen = []
    (row,) = claude.stages(_assistant({"type": "text", "text": long}),
                           on_text=lambda text: seen.append(text))
    assert seen == [long]
    assert row["kind"] == "text" and len(row["detail"]) <= claude.DETAIL_CHARS + 3


def test_text_it_does_not_recognise_is_the_ordinary_row():
    (row,) = claude.stages(_assistant({"type": "text", "text": "plain"}),
                           on_text=lambda _text: None)
    assert (row["kind"], row["title"]) == ("text", "Answered")


def test_nothing_said_is_not_offered_to_it():
    seen = []
    assert claude.stages(_assistant({"type": "text", "text": "  "}),
                         on_text=seen.append) == []
    assert seen == []


def test_without_a_caller_json_is_just_what_was_said():
    """A review is the cycle's idea: the integration shows text as text."""
    review = json.dumps({"summary": "fine", "issues": [], "recommendations": [],
                         "risk": "low"})
    (row,) = claude.stages(_assistant({"type": "text", "text": review}))
    assert row["kind"] == "text"
    from cycle.plugins import agent_stream
    (row,) = agent_stream.stages(_assistant({"type": "text", "text": review}))
    assert row["kind"] == "review" and row["body"]["risk"] == "low"


# ------------------------------------------------------------ a turn's command
def test_a_first_turn_names_the_conversation_and_a_later_one_continues_it():
    first = claude.turn_argv("/bin/claude", "read", session="abc")
    assert first == ["/bin/claude", "-p", "--output-format", "stream-json", "--verbose",
                     "--allowedTools", "Read Glob Grep", "--session-id", "abc"]
    later = claude.turn_argv("/bin/claude", "read", session="abc", resume=True)
    assert later[-2:] == ["--resume", "abc"] and "--session-id" not in later


def test_each_mode_names_exactly_the_tools_it_allows():
    """Naming them is what holds: a tool not named is refused, not waited for."""
    def allowed(mode):
        argv = claude.turn_argv("claude", mode)
        return argv[argv.index("--allowedTools") + 1].split()

    assert claude.MODES == ("read", "edit", "full")
    assert allowed("read") == ["Read", "Glob", "Grep"]
    assert allowed("edit") == ["Read", "Glob", "Grep", "Edit", "Write", "MultiEdit"]
    assert allowed("full") == allowed("edit") + ["Bash"]
    assert "--permission-mode" not in claude.turn_argv("claude", "read")
    for mode in ("edit", "full"):
        argv = claude.turn_argv("claude", mode)
        assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"


def test_model_and_effort_are_said_only_when_there_is_something_to_say():
    plain = claude.turn_argv("claude", "read")
    assert "--model" not in plain and "--effort" not in plain
    argv = claude.turn_argv("claude", "read", model=" opus ", effort="high")
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--effort") + 1] == "high"


def test_a_turn_does_not_inherit_the_bare_mode():
    env = claude.turn_env({"PATH": "/bin", "CLAUDE_CODE_SIMPLE": "1"})
    assert env == {"PATH": "/bin"}


# ------------------------------------------------------- a conversation's record
def test_the_opening_line_says_the_model_and_the_conversation():
    assert claude.events(json.dumps({"type": "system", "subtype": "init",
                                     "model": "claude-sonnet", "session_id": "abc"})) == [
        {"type": "started", "model": "claude-sonnet", "provider_session": "abc"}]
    assert claude.events(json.dumps({"type": "system", "subtype": "hook"})) == []


def test_what_the_agent_says_is_kept_whole():
    long = "x" * (claude.DETAIL_CHARS * 3)
    assert claude.events(_assistant({"type": "thinking", "thinking": " weighing "},
                                    {"type": "text", "text": long})) == [
        {"type": "thinking", "text": "weighing"}, {"type": "text", "text": long}]


def test_a_tool_call_and_its_answer_are_paired_by_id():
    (called,) = claude.events(_assistant({"type": "tool_use", "id": "t1", "name": "Read",
                                          "input": {"file_path": "/repo/src/main.py"}}))
    assert called == {"type": "tool.started", "id": "t1", "name": "Read",
                      "title": "Read main.py", "detail": "file_path: /repo/src/main.py"}
    (answered,) = claude.events(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "the file", "is_error": True}]}}))
    assert answered == {"type": "tool.completed", "id": "t1", "ok": False, "detail": "the file"}


def test_a_write_says_what_it_would_write_and_not_that_it_did():
    """Seen live: in Read mode the CLI refuses the write the agent asked for,
    and a record that said "written" would be wrong about the folder."""
    (found,) = claude.events(_assistant({"type": "tool_use", "id": "t2", "name": "Edit",
                                         "input": {"file_path": "/repo/a.py"}}))
    assert (found["type"], found["writes"]) == ("tool.started", "/repo/a.py")
    (read,) = claude.events(_assistant({"type": "tool_use", "id": "t3", "name": "Read",
                                        "input": {"file_path": "/repo/a.py"}}))
    assert "writes" not in read


def test_a_skill_s_row_says_which_skill():
    (found,) = claude.events(_assistant({"type": "tool_use", "id": "t4", "name": "Skill",
                                         "input": {"skill": "qavector:secret-word"}}))
    assert found["title"] == "Skill qavector:secret-word"


def test_text_handed_back_with_a_tools_answer_is_not_something_the_agent_said():
    assert claude.events(json.dumps({"type": "user", "message": {"content": [
        {"type": "text", "text": "a reminder from the harness"}]}})) == []


def test_the_last_line_says_how_it_ended_and_what_it_cost():
    assert claude.events(json.dumps({
        "type": "result", "is_error": False, "result": " done ", "session_id": "abc",
        "num_turns": 3, "total_cost_usd": 0.04281234})) == [
        {"type": "completed", "text": "done", "provider_session": "abc", "steps": 3,
         "cost_usd": 0.042812}]
    (refused,) = claude.events(json.dumps({"type": "result", "is_error": True,
                                           "result": "", "subtype": "error_max_turns"}))
    assert (refused["type"], refused["error"]) == ("failed", "error_max_turns")


def test_noise_and_shapes_from_a_newer_cli_carry_nothing():
    for line in ("", "not json", "[1]", json.dumps({"type": "rate_limit_event"}),
                 json.dumps({"type": "something_new_entirely"}),
                 json.dumps({"type": "assistant", "message": {"content": [17, None]}})):
        assert claude.events(line) == []


# ------------------------------------------------------ which way it depends
def test_the_integration_imports_nothing_of_what_uses_it():
    code = ("import sys\n"
            "import integrations.agents.cli, integrations.agents.store\n"
            "import integrations.agents.sessions, integrations.agents.runner\n"
            "import integrations.agents.skills\n"
            "import integrations.agents.adapters.claude\n"
            "print(sorted(name for name in sys.modules if name.startswith('cycle.plugins')"
            " or name in ('cycle.restrict', 'cycle.executor', 'cycle.registry')))\n")
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]"
