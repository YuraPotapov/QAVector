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
    assert any("report.json has nothing restrictions could hold" in one for one in model.problems(made, REGISTRY))
    made.steps[1].restrictions, made.steps[0].system = {}, True
    made.steps[0].restrictions = {"network": "off"}
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
        def prepare(self, argv, cwd, env, folders=()):
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


# -- steps that need the network, Jira and Teams, the run's folder ------------------------
def cycle_with(plugin, settings=None, **extra):
    raw = {"id": "demo", "project_folder": "/work/shop",
           "steps": [{"id": "one", "plugin": plugin, "with": settings or {}}]}
    raw.update(extra)
    return model.parse_cycle(raw, "demo")


@pytest.mark.parametrize("plugin,why", [
    ("agent.review", "an agent cannot reach its model"),
    ("jira.issues", "Jira cannot be reached"),
    ("teams.message", "Teams cannot be reached"),
])
def test_a_step_that_needs_the_network_is_refused_before_it_starts(plugin, why):
    made = cycle_with(plugin, restrictions={"switched_off": ["network"]})
    with pytest.raises(restrict.Restrictions) as refused:
        restrict.for_step(made, made.steps[0], {"vars": {}}, "/runs/1/steps/one")
    assert "network is off for this step" in str(refused.value) and why in str(refused.value)
    assert "globe" in str(refused.value)
    made.steps[0].restrictions = {"network": "on"}
    assert restrict.for_step(made, made.steps[0], {"vars": {}}, "/runs/1/steps/one") is None


def test_a_shell_step_without_the_network_still_runs():
    made = cycle_with("command.shell", {"command": "make"}, restrictions={"network": "off"})
    held = restrict.for_step(made, made.steps[0], {"vars": {}}, "/runs/1/steps/one")
    assert held.effective["network"] == "off"


def test_jira_and_teams_are_restricted_and_only_the_browser_stays_a_system_step():
    for plugin in ("jira.issues", "jira.transition", "teams.message"):
        assert not restrict.is_system(cycle_with(plugin).steps[0]) and plugin in restrict.IN_SANDBOX
    assert restrict.is_system(cycle_with("scenario.run").steps[0])
    graph = model.to_graph(cycle_with("teams.message"))
    assert graph["nodes"][0]["restrictable"] is True


def test_a_restricted_step_sees_its_run_folder_and_reads_but_cannot_rewrite_the_record(monkeypatch):
    seen = {}

    class Launcher:
        def prepare(self, effective, argv, cwd, env, extra):
            seen["extra"] = extra
            return "prepared"

    from execution_runtime.backends.linux import restricted
    monkeypatch.setattr(restricted, "launcher", lambda: Launcher())
    made = cycle_with("command.shell", {"command": "make"}, restrictions={"files": "read_write"})
    held = restrict.for_step(made, made.steps[0], {"vars": {}}, "/runs/1/steps/one", "/runs/1")
    held.prepare(["make"], "/work/shop", {})
    access = {one["path"]: one["access"] for one in seen["extra"]}
    assert access["/runs/1"] == "read_write" and access["/runs/1/steps/one"] == "read_write"
    for name in ("metadata.json", "graph.json", ".execution", "logs", "steps"):
        assert access["/runs/1/" + name] == "read_only"
    # Without a workspace given it is the step folder's grandparent.
    assert restrict.StepRestriction(held.effective, "/runs/1/steps/one").workspace == "/runs/1"
    seen.clear()
    open_files = cycle_with("command.shell", {"command": "make"}, restrictions={"network": "off"})
    restrict.for_step(open_files, open_files.steps[0], {"vars": {}}, "/runs/1/steps/one").prepare(["make"], "/w", {})
    assert seen["extra"] == []          # Files "No restrictions" already sees everything


def test_the_worker_answers_with_the_plugin_s_result_and_its_errors(monkeypatch, tmp_path):
    import io
    import json
    from cycle import step_worker

    class Plugin:
        def execute(self, context, step):
            print("chatter goes to the log")
            path = os.path.join(context.step_dir(step.id), "out.json")
            with open(path, "w") as handle:
                handle.write("{}")
            if step.settings.get("fail"):
                raise LookupError("no such project")
            return REGISTRY.succeeded("done %s" % step.settings["token"], count=2)

    monkeypatch.setattr(REGISTRY, "get", lambda plugin_id: Plugin())

    def ask(settings):
        stdout, stderr = io.StringIO(), io.StringIO()
        request = {"plugin": "teams.message", "workspace": str(tmp_path),
                   "step": {"id": "notify", "plugin": "teams.message", "label": "", "settings": settings}}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request)))
        monkeypatch.setattr("sys.stdout", stdout)
        monkeypatch.setattr("sys.stderr", stderr)
        assert step_worker.main() == 0
        return stdout.getvalue(), stderr.getvalue()

    answer, log = ask({"token": "secret-value"})
    assert "chatter" in log and "chatter" not in answer
    result = step_worker._result(answer.encode(), 0, str(tmp_path / "missing.log"))
    assert result.ok and result.message == "done secret-value" and result.outputs == {"count": 2}
    assert os.path.isfile(tmp_path / "steps" / "notify" / "out.json")
    answer, _ = ask({"fail": True})
    with pytest.raises(Exception) as raised:
        step_worker._result(answer.encode(), 0, "")
    assert type(raised.value).__name__ == "LookupError" and "no such project" in str(raised.value)
    with pytest.raises(RuntimeError, match="without a result: it broke"):
        (tmp_path / "w.log").write_text("it broke")
        step_worker._result(b"", 1, str(tmp_path / "w.log"))


def _stand_in(answer_after=0.0):
    """A local stand-in for a Teams workflow; returns (url, received, server)."""
    import http.server
    import threading
    import time
    received = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            time.sleep(answer_after)
            self.send_response(202)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d/hook" % server.server_address[1], received, server


class _RunContext:
    def __init__(self, workspace):
        from cycle.context import CancelToken
        self.workspace = str(workspace)
        self.cancel = CancelToken()

    def step_dir(self, step_id, *parts):
        path = os.path.join(self.workspace, "steps", step_id, *parts)
        os.makedirs(path, exist_ok=True)
        return path


def test_a_teams_step_runs_in_a_worker_process_of_its_own(tmp_path):
    from domain.cycle import CycleStep
    url, received, server = _stand_in()
    try:
        context = _RunContext(tmp_path)
        (tmp_path / "message.md").write_text("Release is ready")
        step = CycleStep("notify", "teams.message", settings={"webhook": url, "text_file": "message.md"})
        result = restrict.Sandboxed(REGISTRY.get("teams.message")).execute(context, step)
        assert result.ok and result.outputs["sent"] is True, result.message
        assert b"Release is ready" in received[0]
    finally:
        server.shutdown()


def test_stopping_a_run_ends_its_worker(tmp_path):
    import threading
    import time
    from cycle import step_worker
    from cycle.context import Cancelled
    from domain.cycle import CycleStep
    url, received, server = _stand_in(answer_after=30)
    try:
        context = _RunContext(tmp_path)
        step = CycleStep("notify", "teams.message", settings={"webhook": url, "text": "hello"})
        threading.Timer(1.5, context.cancel.set, args=("stopped",)).start()
        started = time.monotonic()
        with pytest.raises(Cancelled):
            step_worker.call(context, step)
        assert time.monotonic() - started < 10
    finally:
        server.shutdown()
