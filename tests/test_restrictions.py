"""Restrictions on a project folder: the model, the tool folders and the cycle file.

Nothing here starts a sandbox; tests/test_restricted_linux.py does, when asked.
"""

import os

import pytest

from cycle import checkpoints, model, restrict
from cycle import registry as REGISTRY
from execution_runtime import restrictions as r, toolfolders
from execution_runtime.errors import RuntimePolicyError

PROJECT = "/work/shop"


def resolve(*layers, folder=PROJECT):
    return r.resolve(list(layers), folder, home="/home/someone")


def test_nothing_set_is_unrestricted_and_says_so():
    effective = resolve(("project", None), ("service", {}))
    assert not effective["restricted"]
    assert r.summary(effective) == "Network on · No restrictions"


@pytest.mark.parametrize("raw,message", [
    ({"network": "maybe"}, "on or off"),
    ({"files": "everything"}, "Files is one of"),
    ({"limits": {"memory_mb": 1.5}}, "whole number"),
    ({"limits": {"cpu_cores": 0}}, "between"),
    ({"limits": {"processes": 2}}, "at least 8"),
    ({"limits": {"disk": 1}}, "Limits are"),
    ({"folders": [{"path": "relative/dir"}]}, "absolute"),
    ({"folders": [{"path": "/data", "access": "write"}]}, "read_write or read_only"),
    ({"switched_off": ["memory"]}, "network and/or files"),
    ({"timeout": 3}, "Unknown restriction"),
])
def test_stored_restrictions_are_checked_with_readable_messages(raw, message):
    with pytest.raises(RuntimePolicyError, match=message):
        r.parse(raw)


def test_the_project_sets_defaults_and_a_service_sets_its_own_in_either_direction():
    project = ("project", {"network": "off", "files": "read_only", "limits": {"memory_mb": 4096, "processes": 128}})
    inherited = resolve(project, ("service", {}))
    assert inherited["network"] == "off" and inherited["files"] == "read_only"
    assert inherited["provenance"]["network"] == "project"
    own = resolve(project, ("service", {"network": "on", "files": "read_write", "limits": {"memory_mb": 8192}}))
    # The one service that needs its port and more memory says so for itself.
    assert own["network"] == "on" and own["files"] == "read_write"
    assert own["limits"] == {"memory_mb": 8192, "processes": 128}
    assert own["provenance"]["network"] == "service" and own["provenance"]["limits.processes"] == "project"
    assert r.summary(own) == "Network on · Project read and write · 8 GB · 128 processes"


def test_switching_off_keeps_what_was_configured():
    effective = resolve(("project", {"network": "on", "files": "read_only", "folders": [{"path": "~/data"}],
                                     "switched_off": ["network", "files"]}))
    assert effective["network"] == "off" and effective["files"] == "none" and effective["folders"] == []
    assert effective["configured"] == {"network": "on", "files": "read_only"}
    assert effective["switched_off"] == ["network", "files"]
    again = resolve(("project", {"network": "on", "files": "read_only", "folders": [{"path": "~/data"}]}))
    assert again["files"] == "read_only" and again["folders"] == [{"path": "/home/someone/data", "access": "read_only"}]


def test_a_project_switch_holds_services_that_did_not_say_otherwise():
    project = ("project", {"switched_off": ["network"]})
    assert resolve(project, ("service", {}))["network"] == "off"
    assert resolve(project, ("service", {"network": "on"}))["network"] == "on"


def test_a_service_lists_its_own_folders_instead_of_the_project_s():
    project = ("project", {"files": "read_only", "folders": [{"path": "/data", "access": "read_only"}]})
    assert resolve(project, ("service", {}))["folders"] == [{"path": "/data", "access": "read_only"}]
    own = resolve(project, ("service", {"folders": [{"path": "/elsewhere", "access": "read_write"}]}))
    assert own["folders"] == [{"path": "/elsewhere", "access": "read_write"}]


def test_a_folder_inside_the_project_is_refused_because_files_already_decides_it():
    with pytest.raises(RuntimePolicyError, match="inside the project folder"):
        resolve(("project", {"files": "read_only", "folders": [{"path": PROJECT + "/build", "access": "read_write"}]}))


def test_no_file_restrictions_lists_no_folders():
    effective = resolve(("project", {"network": "off", "folders": [{"path": "/data"}]}))
    assert effective["files"] == "all" and effective["folders"] == []
    assert r.summary(effective) == "Network off"


def test_tool_folders_reach_a_cli_installed_under_home_and_its_settings(tmp_path):
    home = tmp_path / "home"
    versions = home / ".local/share/tool/versions"
    versions.mkdir(parents=True)
    binary = versions / "1.0"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    bindir = home / ".local/bin"
    bindir.mkdir(parents=True)
    (bindir / "claude").symlink_to(binary)
    (home / ".claude").mkdir()
    (home / ".claude.json").write_text("{}")
    (home / ".gitconfig").write_text("[user]\n")
    (home / ".ssh").mkdir()
    argv, folders = toolfolders.for_command(["claude", "-p", "x"], {"PATH": str(bindir)}, home=str(home))
    assert argv[0] == str(binary)
    found = {one["path"]: one["access"] for one in folders}
    assert found == {str(versions): "read_only", str(home / ".claude"): "read_write",
                     str(home / ".claude.json"): "copy", str(home / ".gitconfig"): "read_only"}
    assert all(one["reason"] for one in folders)


def test_tool_folders_add_nothing_for_system_tools_or_tools_inside_the_project(tmp_path):
    project = tmp_path / "project"
    (project / ".venv/bin").mkdir(parents=True)
    python = project / ".venv/bin/python"
    python.write_text("")
    python.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    assert toolfolders.for_command(["/bin/sh", "-c", "true"], home=str(home))[1] == []
    assert toolfolders.for_command([str(python)], home=str(home), visible=[str(project)])[1] == []


def test_tool_folders_reach_a_virtualenv_outside_the_project_and_its_base_python(tmp_path):
    home = tmp_path / "home"
    base = home / ".pyenv/versions/3.11/bin"
    base.mkdir(parents=True)
    (base / "python").write_text("")
    env = home / ".virtualenvs/shop/bin"
    env.mkdir(parents=True)
    (env / "python").write_text("")
    (env / "python").chmod(0o755)
    (home / ".virtualenvs/shop/pyvenv.cfg").write_text("home = %s\n" % base)
    _, folders = toolfolders.for_command([str(env / "python"), "app.py"], home=str(home))
    assert [one["path"] for one in folders] == [str(home / ".virtualenvs/shop"), str(home / ".pyenv/versions/3.11")]


# -- the cycle file ------------------------------------------------------------------
def cycle(**extra):
    raw = {"id": "demo", "variables": {"proj": "/work/shop"},
           "steps": [{"id": "test", "plugin": "command.shell", "with": {"command": "make test"}},
                     {"id": "notify", "plugin": "report.json", "needs": ["test"]}]}
    raw.update(extra)
    return model.parse_cycle(raw, "demo")


def test_a_cycle_without_a_project_folder_is_untouched():
    plain = cycle()
    assert model.problems(plain, REGISTRY) == []
    assert "project_folder" not in model.to_document(plain)
    assert restrict.for_step(plain, plain.steps[0], {"vars": {}}, "/tmp/step") is None


def test_restrictions_round_trip_through_the_cycle_file():
    made = cycle(project_folder="${vars.proj}", restrictions={"network": "off", "switched_off": ["network"]})
    made.steps[0].restrictions = {"files": "read_only"}
    made.steps[1].system = True
    document = model.to_document(made)
    assert document["project_folder"] == "${vars.proj}"
    assert document["restrictions"] == {"network": "off", "switched_off": ["network"]}
    assert document["steps"][0]["restrictions"] == {"files": "read_only"}
    assert document["steps"][1]["system"] is True
    again = model.parse_cycle(document, "demo")
    assert again.project_folder == "${vars.proj}" and again.steps[1].system is True
    graph = model.to_graph(again)
    assert graph["project_folder"] == "${vars.proj}"
    assert graph["nodes"][0]["restrictable"] is True and "restrictable" not in graph["nodes"][1]


@pytest.mark.parametrize("change,message", [
    (dict(restrictions={"network": "off"}), "need a project_folder"),
    (dict(project_folder="/work", execution={"runtime": {"mode": "isolated"}}), "either restrictions or execution.runtime"),
    (dict(project_folder="/work", restrictions={"files": "some"}), "Files is one of"),
])
def test_cycle_problems_name_what_is_wrong(change, message):
    assert any(message in one for one in model.problems(cycle(**change), REGISTRY))


def test_step_restrictions_need_the_folder_and_are_refused_on_system_steps():
    made = cycle(project_folder="/work")
    made.steps[1].restrictions = {"network": "off"}
    assert any("system step runs without restrictions" in one for one in model.problems(made, REGISTRY))
    no_folder = cycle()
    no_folder.steps[0].restrictions = {"network": "off"}
    assert any("need the cycle's project_folder" in one for one in model.problems(no_folder, REGISTRY))


def test_resolution_follows_variables_and_skips_system_steps():
    made = cycle(project_folder="${vars.proj}", restrictions={"files": "read_only"})
    scope = {"vars": {"proj": "/work/other"}}
    held = restrict.for_step(made, made.steps[0], scope, "/runs/1/steps/test")
    assert held.folder == "/work/other" and held.effective["files"] == "read_only"
    assert held.workdir("") == "/work/other" and held.workdir("sub") == "/work/other/sub"
    assert restrict.for_step(made, made.steps[1], scope, "/runs/1/steps/notify") is None
    made.steps[0].system = True
    assert restrict.for_step(made, made.steps[0], scope, "/runs/1/steps/test") is None


def test_fingerprints_change_only_for_steps_the_restrictions_hold():
    plain = cycle()
    before = checkpoints.signatures(plain, {}, REGISTRY)
    assert checkpoints.signatures(cycle(restrictions={}), {}, REGISTRY) == before
    held = checkpoints.signatures(cycle(project_folder="${vars.proj}", restrictions={"network": "off"}), {}, REGISTRY)
    assert held["test"] != before["test"]
    assert held["notify"] == before["notify"]


def test_steps_start_exactly_as_before_when_nothing_holds_them(monkeypatch):
    started = []
    monkeypatch.setattr(restrict.subprocess, "Popen", lambda *args, **kwargs: started.append((args, kwargs)))
    restrict.popen(["git", "status"], cwd="/work", env={"A": "1"})
    assert started == [((["git", "status"],), {"cwd": "/work", "env": {"A": "1"}, "shell": False})]


def test_a_held_thread_starts_through_the_launcher(monkeypatch):
    calls = []

    class Prepared:
        argv, cwd, env, pass_fds = ["bwrap", "--", "git"], "/", {"B": "2"}, (9,)

        def close(self):
            calls.append("closed")

    class Held:
        def prepare(self, argv, cwd, env):
            calls.append((argv, cwd))
            return Prepared()

    monkeypatch.setattr(restrict.subprocess, "Popen", lambda argv, **kwargs: calls.append((argv, kwargs.get("pass_fds"))))
    with restrict.active(Held()):
        restrict.popen("echo hi", cwd="/work", env={}, shell=True)
    restrict.popen(["true"])
    assert calls[0] == (["/bin/sh", "-c", "echo hi"], "/work")
    assert calls[1] == (["bwrap", "--", "git"], (9,))
    assert calls[2] == "closed"
    assert calls[3] == (["true"], None)
    assert restrict.current() is None


def test_the_cycle_file_writer_keeps_restrictions_and_quotes_on_and_off():
    import yaml
    from cycle import cyclefile
    document = {"id": "x", "project_folder": "~/p",
                "restrictions": {"network": "off", "files": "read_only", "switched_off": ["network"],
                                 "folders": [{"path": "~/data", "access": "read_only"}],
                                 "limits": {"memory_mb": 4096, "cpu_cores": 1.5}},
                "steps": [{"id": "a", "plugin": "command.shell", "system": True,
                           "restrictions": {"network": "on"}, "with": {"command": "true"}}]}
    text = cyclefile.render(document)
    assert 'network: "off"' in text and 'network: "on"' in text
    assert yaml.safe_load(text) == document


def test_an_unquoted_on_or_off_written_by_hand_means_on_or_off():
    import yaml
    raw = yaml.safe_load("network: off\n")
    assert raw == {"network": False}
    assert r.parse(raw) == {"network": "off"}
    assert r.parse(yaml.safe_load("network: on\n")) == {"network": "on"}
