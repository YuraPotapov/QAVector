"""``qavector-core --integrations agents ...``: what the page will be answered.

Every command is one JSON object and an exit code, and the failures carry a
``kind`` a page can act on - install it, sign in, or read what it said. Asking
who is signed in is the only thing that starts a CLI, and it is a script
written by the test: a real ``claude`` is never run and no model is asked
anything.
"""

import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations import cli                                       # noqa: E402

SIGNED_IN = {"loggedIn": True, "authMethod": "claude.ai",
             "email": "someone@example.invalid", "orgName": "Someone's Organization",
             "subscriptionType": "pro"}


def _cli(directory, body):
    path = os.path.join(str(directory), "claude")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!%s\n%s\n" % (sys.executable, body))
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


def _answering(directory, payload):
    return _cli(directory, "import json\nprint(json.dumps(%r))" % (payload,))


@pytest.fixture
def agents(tmp_path, capsys):
    """Run one ``agents`` command against a scratch file: ``(exit code, answer)``."""
    path = str(tmp_path / "agents.json")

    def run(*words, document=None):
        argv = ["agents", *words, "--agents-file=" + path]
        if document is not None:
            source = tmp_path / "from.json"
            source.write_text(json.dumps(document), encoding="utf-8")
            argv.append("--from=%s" % source)
        code = cli.main(argv)
        return code, json.loads(capsys.readouterr().out)

    run.path = path
    return run


def _save(agents, **fields):
    code, answer = agents("save", document={"connection": dict(
        {"name": "Claude", "kind": "claude"}, **fields)})
    assert code == 0, answer
    return answer["connection"]


# --------------------------------------------------------------------- kinds
def test_kinds_says_where_claude_is_without_starting_it(agents, tmp_path, monkeypatch):
    marker = tmp_path / "ran"
    path = _cli(tmp_path, "open(%r, 'w').close()" % str(marker))
    monkeypatch.setenv("PATH", str(tmp_path))
    code, answer = agents("kinds")
    assert code == 0
    from integrations.agents.adapters import claude, codex
    assert [one["kind"] for one in answer["kinds"]] == ["claude", "codex"]
    assert answer["kinds"][0] == {"kind": "claude", "name": "Claude Code",
                                "default_binary": "claude", "found": path,
                                "efforts": ["low", "medium", "high", "xhigh", "max"],
                                "modes": ["read", "edit", "full"], "plans": True,
                                "mode_description": claude.MODE_DESCRIPTION,
                                "model_default_label": claude.MODEL_DEFAULT_LABEL,
                                "capabilities": claude.capabilities().document()}
    assert answer["kinds"][1]["capabilities"] == codex.capabilities().document()
    assert not marker.exists()


def test_kinds_says_when_it_is_nowhere(agents, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert agents("kinds")[1]["kinds"][0]["found"] == ""


def test_models_asks_for_metadata_only_and_discards_account_data(agents, tmp_path):
    binary = _cli(tmp_path, '''
import json, sys
request = json.loads(sys.stdin.readline())
assert request["type"] == "control_request"
assert request["request"]["subtype"] == "initialize"
assert sys.stdin.read() == ""
assert "--safe-mode" in sys.argv
assert "--strict-mcp-config" in sys.argv
assert "--no-session-persistence" in sys.argv
assert sys.argv[sys.argv.index("--tools") + 1] == ""
print(json.dumps({"type": "control_response", "response": {
    "subtype": "success", "request_id": request["request_id"], "response": {
        "account": {"email": "private@example.invalid"},
        "models": [{"value": "opus", "displayName": "Current Opus",
                    "description": "Complex work", "resolvedModel": "claude-opus-test"}]}}}))
''')
    saved = _save(agents, binary=binary)
    code, answer = agents("models", saved["id"])
    assert code == 0 and answer == {"ok": True, "models": [{
        "value": "opus", "label": "Current Opus", "description": "Complex work",
        "resolved_model": "claude-opus-test"}]}
    assert "private@example.invalid" not in json.dumps(answer)
    code, same = agents("models", document={"connection": {"kind": "claude", "binary": binary}})
    assert code == 0 and same == answer


def test_models_failure_does_not_invent_a_static_list(agents, tmp_path):
    binary = _answering(tmp_path, {"old": "CLI"})
    _save(agents, binary=binary)
    code, answer = agents("models", "claude")
    assert code == 1 and answer["models"] == []
    assert answer["kind"] == "unavailable"


def test_models_missing_connection_is_refused(agents):
    code, answer = agents("models", "missing")
    assert code == 1 and answer["kind"] == "not_found"


# ------------------------------------------------------- the connections file
def test_a_connection_is_saved_listed_and_deleted(agents):
    saved = _save(agents, model="sonnet")
    assert saved["id"] == "claude"
    code, answer = agents("list")
    assert code == 0 and answer["path"] == agents.path
    assert [(one["id"], one["model"]) for one in answer["connections"]] == [("claude", "sonnet")]
    assert agents("delete", "claude") == (0, {"ok": True, "id": "claude"})
    assert agents("list")[1]["connections"] == []
    code, answer = agents("delete", "claude")
    assert (code, answer["kind"]) == (1, "not_found")


def test_what_is_wrong_with_a_connection_comes_back_as_problems(agents):
    code, answer = agents("save", document={"connection": {"name": "", "kind": "claude",
                                                           "effort": "extreme"}})
    assert (code, answer["kind"]) == (1, "invalid")
    assert len(answer["problems"]) == 2 and "Name is required." in answer["error"]
    assert not os.path.exists(agents.path)


# ---------------------------------------------------------------------- test
def test_a_check_says_who_is_signed_in_and_is_remembered(agents, tmp_path):
    binary = _answering(tmp_path, SIGNED_IN)
    _save(agents, binary=binary)
    code, answer = agents("test", "claude")
    assert code == 0
    assert answer["result"] == {
        "binary": binary, "account": "someone@example.invalid",
        "organisation": "Someone's Organization", "plan": "pro", "method": "claude.ai",
        "summary": "Signed in as someone@example.invalid (pro)"}
    check = agents("list")[1]["connections"][0]["last_check"]
    assert (check["ok"], check["account"], check["error"]) == (
        True, "someone@example.invalid", "")
    assert check["at"]


def test_not_signed_in_is_something_signing_in_fixes(agents, tmp_path):
    _save(agents, binary=_answering(tmp_path, {"loggedIn": False}))
    code, answer = agents("test", "claude")
    assert (code, answer["kind"]) == (1, "signin")
    assert "auth login" in answer["error"]
    # Remembered with why, so the table can say which of the three it was.
    check = agents("list")[1]["connections"][0]["last_check"]
    assert (check["ok"], check["kind"]) == (False, "signin")
    assert answer["last_check"] == check


def test_a_cli_that_is_not_there_is_something_installing_fixes(agents, tmp_path):
    _save(agents, binary=str(tmp_path / "no-such-claude"))
    code, answer = agents("test", "claude")
    assert (code, answer["kind"]) == (1, "not_installed")


def test_a_cli_that_answers_with_something_else_is_neither(agents, tmp_path):
    _save(agents, binary=_cli(tmp_path, "print('usage: claude [options]')"))
    code, answer = agents("test", "claude")
    assert (code, answer["kind"]) == (1, "error")
    assert "usage: claude" in answer["error"]


def test_a_connection_not_saved_yet_can_be_checked_and_nothing_is_written(agents, tmp_path):
    """What the Add dialog asks before there is anything to save."""
    binary = _answering(tmp_path, SIGNED_IN)
    code, answer = agents("test", document={"connection": {"kind": "claude",
                                                           "binary": binary}})
    assert code == 0 and answer["result"]["account"] == "someone@example.invalid"
    assert answer["connection"] == ""
    assert not os.path.exists(agents.path)


def test_a_check_of_a_kind_nobody_knows_is_refused_before_anything_runs(agents):
    code, answer = agents("test", document={"connection": {"kind": "telepathy"}})
    assert (code, answer["kind"]) == (1, "invalid")
    assert "telepathy" in answer["error"]


def test_a_check_of_a_connection_that_is_not_there(agents):
    code, answer = agents("test", "ghost")
    assert (code, answer["kind"], answer["error"]) == (1, "not_found", "No connection 'ghost'.")


# ------------------------------------------------------------------- sign-in
def test_signing_in_is_a_command_handed_back_never_run_here(agents, tmp_path):
    marker = tmp_path / "ran"
    binary = _cli(tmp_path, "open(%r, 'w').close()" % str(marker))
    _save(agents, binary=binary)
    code, answer = agents("sign-in", "claude")
    assert code == 0
    assert answer["argv"] == [binary, "auth", "login", "--claudeai"]
    assert "Nothing is stored" in answer["detail"]
    assert not marker.exists()


def test_signing_in_to_a_cli_that_is_not_there_says_so(agents, tmp_path):
    _save(agents, binary=str(tmp_path / "no-such-claude"))
    code, answer = agents("sign-in", "claude")
    assert (code, answer["kind"]) == (1, "not_installed")
    assert "Claude Code is not installed" in answer["error"]


# ------------------------------------------------------------ the command line
@pytest.mark.parametrize("words", [(), ("frobnicate",), ("delete",), ("test",),
                                   ("save",), ("sign-in", "a", "b")])
def test_a_wrong_command_line_is_answered_with_the_agents_usage(agents, words):
    code, answer = agents(*words)
    assert (code, answer["kind"]) == (2, "usage")
    assert "kinds" in answer["usage"] and "sign-in ID" in answer["usage"]


def test_a_broken_file_is_an_answer_not_a_traceback(agents):
    with open(agents.path, "w", encoding="utf-8") as handle:
        handle.write("{not json")
    code, answer = agents("list")
    assert (code, answer["kind"]) == (1, "error")
    assert "cannot read" in answer["error"]
