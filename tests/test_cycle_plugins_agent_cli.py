"""Finding the Claude Code CLI, and asking it who it is signed in as.

This is the module that makes an agent step work without an API key, so what is
worth testing is every way it can go wrong: no binary, a binary that is not
executable, one too old to know the subcommand, one that hangs, one that
answers with something unexpected. Every one of them has to come back as the
same shape with a sentence in ``problem`` - a settings row and a step both
render that, and neither may be handed an exception.

A real `claude` is never run. The binary is a script written by the test, which
is what lets the unhappy paths be tested at all.
"""

import json
import os
import stat
import sys

import pytest

from cycle.plugins import agent_cli

posix_only = pytest.mark.skipif(os.name == "nt",
                                reason="the fake binaries here are shell scripts")


def fake_binary(directory, body, name="claude"):
    """A script standing in for the CLI, so the unhappy paths are reachable."""
    path = os.path.join(str(directory), name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!%s\n%s\n" % (sys.executable, body))
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP)
    return path


def answering(directory, payload, code=0, name="claude"):
    return fake_binary(directory, "import json, sys\n"
                       "print(json.dumps(%r))\nsys.exit(%d)"
                       % (payload, code), name)


SIGNED_IN = {"loggedIn": True, "authMethod": "claude.ai",
             "apiProvider": "firstParty", "email": "someone@example.invalid",
             "orgName": "Someone's Organization", "subscriptionType": "pro"}


# ------------------------------------------------------------------- finding it
def test_a_bare_name_is_looked_up_on_the_path(tmp_path, monkeypatch):
    path = answering(tmp_path, SIGNED_IN)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert agent_cli.resolve("claude") == path
    assert agent_cli.resolve("") == path          # the default name


def test_a_path_is_taken_as_one(tmp_path):
    path = answering(tmp_path, SIGNED_IN, name="claude-somewhere-else")
    assert agent_cli.resolve(path) == path


def test_a_name_that_is_nowhere_resolves_to_nothing(monkeypatch, tmp_path):
    """Returning "" rather than raising keeps "not installed" a thing the
    caller reports, which is the useful answer."""
    monkeypatch.setenv("PATH", str(tmp_path))
    assert agent_cli.resolve("claude") == ""
    assert agent_cli.resolve("/no/such/binary") == ""


@posix_only
def test_a_file_that_is_not_executable_is_not_it(tmp_path):
    path = os.path.join(str(tmp_path), "claude")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("not a program")
    os.chmod(path, 0o644)
    assert agent_cli.resolve(path) == ""


# ------------------------------------------------------------------ signed in
@posix_only
def test_a_signed_in_cli_says_who_it_is(tmp_path):
    status = agent_cli.auth_status(answering(tmp_path, SIGNED_IN))

    assert status["installed"] and status["logged_in"]
    assert status["email"] == "someone@example.invalid"
    assert status["organisation"] == "Someone's Organization"
    assert status["plan"] == "pro"
    assert status["method"] == "claude.ai"
    assert status["problem"] == ""


@posix_only
def test_a_cli_that_is_not_signed_in_says_what_to_run(tmp_path):
    """Naming the one command that fixes it is the whole point of checking."""
    status = agent_cli.auth_status(answering(tmp_path, {"loggedIn": False}))

    assert status["installed"] and not status["logged_in"]
    assert "auth login" in status["problem"]


def test_a_cli_that_is_not_installed_says_so(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    status = agent_cli.auth_status()

    assert not status["installed"] and not status["logged_in"]
    assert "not installed" in status["problem"]


@posix_only
def test_a_cli_too_old_for_the_subcommand_is_reported_not_crashed(tmp_path):
    """It prints usage on stderr and exits non-zero - not JSON."""
    path = fake_binary(tmp_path, "import sys\n"
                                 "sys.stderr.write('unknown command: auth')\n"
                                 "sys.exit(1)")
    status = agent_cli.auth_status(path)

    assert status["installed"] and not status["logged_in"]
    assert "did not answer" in status["problem"]
    assert "unknown command" in status["problem"]


@posix_only
def test_a_cli_that_answers_with_nonsense_is_reported(tmp_path):
    path = fake_binary(tmp_path, "print('[1, 2, 3]')")
    status = agent_cli.auth_status(path)

    assert not status["logged_in"]
    assert "unexpected" in status["problem"]


@posix_only
def test_a_cli_that_hangs_is_given_up_on(tmp_path):
    """A settings row that waits forever is worse than one that says so."""
    path = fake_binary(tmp_path, "import time\ntime.sleep(30)")
    status = agent_cli.auth_status(path, timeout=1.0)

    assert not status["logged_in"]
    assert status["problem"]


@posix_only
def test_asking_never_raises_whatever_the_binary_does(tmp_path):
    for body in ("import sys; sys.exit(9)", "print('')", "raise SystemExit(0)",
                 "import os; os.abort()"):
        agent_cli.auth_status(fake_binary(tmp_path, body))


# -------------------------------------------------------------------- saying it
@posix_only
def test_what_a_status_row_shows(tmp_path):
    assert agent_cli.describe(
        agent_cli.auth_status(answering(tmp_path, SIGNED_IN))) == (
            "Signed in as someone@example.invalid (pro)")


@posix_only
def test_a_cli_signed_in_with_no_email_falls_back_to_the_organisation(tmp_path):
    status = agent_cli.auth_status(answering(
        tmp_path, {"loggedIn": True, "orgName": "Acme"}))
    assert agent_cli.describe(status) == "Signed in as Acme"


def test_what_the_rows_say_when_it_is_not_there(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert agent_cli.describe(agent_cli.auth_status()) == (
        "Claude Code is not installed")


@posix_only
def test_what_the_row_says_when_it_is_not_signed_in(tmp_path):
    assert agent_cli.describe(
        agent_cli.auth_status(answering(tmp_path, {"loggedIn": False}))) == (
            "Not signed in")


# --------------------------------------------------------------- signing in
def test_signing_in_uses_the_subscription_by_default():
    """The Console flow bills API usage; the subscription is what most people
    who have Claude Code already have."""
    argv = agent_cli.login_argv("/usr/bin/claude")
    assert argv[:4] == ["/usr/bin/claude", "auth", "login", "--claudeai"]


def test_signing_in_against_the_console_can_be_asked_for():
    assert "--console" in agent_cli.login_argv("/usr/bin/claude", console=True)


def test_an_email_can_be_filled_in_on_the_login_page():
    argv = agent_cli.login_argv("/usr/bin/claude", email="a@example.invalid")
    assert argv[-2:] == ["--email", "a@example.invalid"]


def test_the_command_is_handed_back_even_when_the_binary_is_missing():
    """So the message can name what somebody would have to install."""
    assert agent_cli.login_argv("no-such-claude")[0] == "no-such-claude"


def test_nothing_here_ever_handles_a_credential():
    """The CLI keeps its own; this module's whole reason for existing is that
    no token passes through the application."""
    import inspect

    source = inspect.getsource(agent_cli)
    for word in ("api_key", "ANTHROPIC_API_KEY", "Bearer", "sk-ant"):
        assert word not in source.replace("# ", "")
