"""An agent's program is found where its installers put it, not only on PATH.

Every place is a folder the test makes; nothing on this computer is looked at.
"""

import os
import stat

import pytest

from cycle import connections as binding
from cycle import registry
from integrations.agents import adapters, locate
from integrations.agents.adapters import claude, codex

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the stand-in programs are scripts")


def _program(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
def nowhere(tmp_path, monkeypatch):
    """No program on PATH, and a home of the test's own."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


def test_path_comes_first(nowhere, monkeypatch):
    on_path = _program(nowhere / "bin" / "codex")
    monkeypatch.setenv("PATH", str(nowhere / "bin"))
    app = _program(nowhere / "app" / "codex")
    assert locate.find("codex", [("the app", app)]) == (on_path, locate.ON_PATH)


def test_an_app_that_carries_the_program_is_found_and_named(nowhere):
    app = _program(nowhere / "app" / "resources" / "codex")
    places = [("npm", str(nowhere / "npm" / "codex")), ("the ChatGPT app", app)]
    assert locate.find("codex", places) == (app, "the ChatGPT app")


def test_the_newest_node_version_wins(nowhere):
    for version in ("v9.11.2", "v18.20.8", "v18.3.0"):
        _program(nowhere / "home" / ".nvm" / "versions" / "node" / version / "bin" / "claude")
    path, where = locate.find("claude", locate.node_places("claude"))
    assert where == "nvm" and "/v18.20.8/" in path


def test_a_file_that_cannot_run_is_not_a_program(nowhere):
    plain = nowhere / "app" / "codex"
    plain.parent.mkdir()
    plain.write_text("")
    assert locate.find("codex", [("the app", str(plain))]) == ("", "")


def test_a_program_somebody_chose_is_never_swapped(nowhere):
    app = _program(nowhere / "app" / "codex")
    chosen = _program(nowhere / "chosen" / "codex")
    assert locate.find("codex", [("the app", app)], chosen) == (chosen, locate.GIVEN)
    assert locate.find("codex", [("the app", app)], str(nowhere / "gone" / "codex")) == ("", "")
    # The default name itself is no choice: it is looked for everywhere.
    assert locate.find("codex", [("the app", app)], "codex") == (app, "the app")


def test_both_adapters_look_in_their_installers_places(nowhere, monkeypatch):
    app = _program(nowhere / "app" / "codex")
    monkeypatch.setattr(codex, "PLACES", (("the ChatGPT app", app),))
    own = _program(nowhere / "home" / ".claude" / "local" / "claude")
    assert codex.find("") == (app, "the ChatGPT app") and codex.resolve() == app
    assert claude.find("") == (own, "Claude Code's installer")
    found = {one["kind"]: one for one in adapters.describe()}
    assert found["codex"]["found"] == app and found["codex"]["found_where"] == "the ChatGPT app"
    assert "npm install -g @openai/codex" in found["codex"]["install_hint"]


def test_nothing_found_says_how_to_install(nowhere, monkeypatch):
    monkeypatch.setattr(codex, "PLACES", ())
    found = {one["kind"]: one for one in adapters.describe()}["codex"]
    assert found["found"] == "" and found["found_where"] == ""
    assert found["install_hint"]


def test_a_cycle_step_runs_the_program_its_connection_finds(nowhere):
    own = _program(nowhere / "home" / ".claude" / "local" / "claude")
    metadata = registry.get("agent.edit").metadata
    connections = {"agents/claude-code": {"kind": "claude", "binary": ""}}
    expanded = binding._expand_agent(metadata, {"connection": "claude-code", "task": "t"},
                                     "claude-code", connections)
    assert expanded["claude"] == own
    connections["agents/claude-code"]["binary"] = "/chosen/claude"
    expanded = binding._expand_agent(metadata, {"connection": "claude-code", "task": "t"},
                                     "claude-code", connections)
    assert expanded["claude"] == "/chosen/claude"
