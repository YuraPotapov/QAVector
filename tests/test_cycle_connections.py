"""A cycle step that names a connection instead of carrying a site and a token.

What matters: the step reaches Jira exactly as if it had said where and who
itself; the token comes from the project's store and appears nowhere a run
writes; and a step that cannot use its connection is refused with a sentence
that says where to fix it.
"""

import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cycle import model, registry, watch                            # noqa: E402
from cycle.executor import run_cycle                                # noqa: E402
from domain.cycle import SUCCESS, FAILED                            # noqa: E402
from cycle import connections as binding                            # noqa: E402
from integrations import store                                      # noqa: E402
from fake_jira import FakeJira                                      # noqa: E402

TOKEN = "tok-71c2-not-a-real-one"
EMAIL = "you@yourcompany.example"


@pytest.fixture
def jira():
    made = []

    def serve(**answers):
        one = FakeJira(dict({"/rest/api/3/myself": {"displayName": "Tester"},
                             "/rest/api/3/search/jql": {"isLast": True, "issues": [
                                 {"key": "QA-1", "fields": {
                                     "summary": "Login fails",
                                     "status": {"name": "To Do"}}}]},
                             "/rest/api/3/issue/QA-1/comment": {"comments": []}},
                            **answers))
        made.append(one)
        return one

    yield serve
    for one in made:
        one.close()


def _connection(site, **settings):
    return {"id": "product-team", "provider": "jira", "name": "Product team",
            "site": site, "flavour": "cloud", "auth": "basic", "email": EMAIL,
            "verify_tls": True, "token": TOKEN, "settings": dict(settings)}


def _cycle(*steps, variables=None):
    return model.parse_cycle({"id": "c", "variables": dict(variables or {}),
                              "steps": list(steps)}, "c")


READ = {"id": "queue", "plugin": "jira.issues",
        "with": {"connection": "product-team", "user": "currentUser()",
                 "comments": False, "attachments": "none", "subtasks": "none"}}


# -- the form -----------------------------------------------------------------
def test_a_step_with_a_connection_needs_no_site_email_or_token():
    plugin = registry.get("jira.issues")
    assert plugin.problems(READ["with"]) == []
    move = registry.get("jira.transition")
    assert move.problems({"connection": "${vars.jira}", "issue": "QA-1",
                          "to": "Start Progress"}) == []


def test_a_step_without_one_still_needs_them_and_says_so_as_before():
    found = registry.get("jira.issues").problems({"user": "currentUser()"})
    assert "Site is required." in found
    assert "API token is required." in found
    assert "Basic authentication needs the account email." in found


def test_a_connection_and_a_site_together_are_refused():
    found = registry.get("jira.issues").problems(
        dict(READ["with"], site="https://x.example.invalid", token="${vars.t}"))
    assert any(one.startswith("Give Connection or site, token, not both") for one in found)


def test_a_connection_chosen_from_another_step_is_refused():
    found = registry.get("jira.issues").problems(
        dict(READ["with"], connection="${steps.pick.outputs.key}"))
    assert any("connection may read only ${vars...}" in one for one in found)


def test_the_cycle_validator_accepts_a_connection_step():
    assert model.problems(_cycle(READ), registry=registry) == []


# -- expanding ----------------------------------------------------------------
def test_a_connection_stands_for_exactly_the_fields_the_plugin_has():
    expanded = binding.expand(registry.get("jira.transition"),
                              {"connection": "product-team", "issue": "QA-1", "to": "Go"},
                              {"product-team": _connection("https://x.example.invalid")})
    assert "connection" not in expanded
    assert expanded["site"] == "https://x.example.invalid"
    assert expanded["token"] == TOKEN and expanded["email"] == EMAIL
    assert expanded["verify_tls"] is True and expanded["flavour"] == "cloud"


def test_settings_naming_no_connection_pass_through_untouched():
    settings = {"site": "https://x.example.invalid", "token": "t"}
    assert binding.expand(registry.get("jira.issues"), settings, {}) is settings


@pytest.mark.parametrize("connections, words", [
    ({}, "No connection 'product-team' in this project"),
    ({"product-team": dict(_connection("https://x.example.invalid"), token="")},
     "has no token"),
    ({"product-team": dict(_connection("https://x.example.invalid"), provider="teams")},
     "needs a jira one"),
])
def test_a_connection_that_cannot_be_used_is_refused(connections, words):
    with pytest.raises(binding.BindingError) as caught:
        binding.expand(registry.get("jira.issues"), {"connection": "product-team"},
                       connections)
    assert words in str(caught.value)


# -- a run --------------------------------------------------------------------
def test_a_run_reaches_jira_through_the_connection_and_writes_no_token(jira, tmp_path):
    server = jira()
    run = run_cycle(_cycle(READ), str(tmp_path / "run"),
                    connections={"product-team": _connection(server.site)})
    assert run.steps["queue"].status == SUCCESS, run.steps["queue"].message
    assert run.steps["queue"].outputs["keys"] == ["QA-1"]
    expected = "Basic " + base64.b64encode(("%s:%s" % (EMAIL, TOKEN)).encode()).decode()
    assert {one.authorization for one in server.seen} == {expected}
    for folder, _dirs, files in os.walk(str(tmp_path / "run")):
        for name in files:
            with open(os.path.join(folder, name), "rb") as handle:
                assert TOKEN.encode() not in handle.read(), name


def test_a_run_whose_step_cannot_use_its_connection_fails_that_step_saying_why(tmp_path):
    run = run_cycle(_cycle(READ), str(tmp_path / "run"), connections={})
    assert run.steps["queue"].status == FAILED
    assert "No connection 'product-team'" in run.steps["queue"].message


def test_a_connection_s_token_is_redacted_from_what_a_person_is_shown(tmp_path):
    from cycle import execution_policy

    class Context(object):
        secrets = binding.redactions({"product-team": _connection("x")})
        cycle = _cycle(READ)

    preview = execution_policy.input_preview({"note": "sent %s" % TOKEN},
                                             {"note": "sent"}, Context())
    assert TOKEN not in json.dumps(preview)


# -- a listener ---------------------------------------------------------------
def _listening(site_step):
    return model.parse_cycle({
        "id": "dev", "variables": {"task_key": ""},
        "triggers": [{"id": "new_task", "watch": "queue", "pin": "task_key"}],
        "steps": [site_step]}, "dev")


def test_a_listener_asks_through_the_connection(jira, tmp_path):
    server = jira()
    found = watch.check(_listening(READ), memory_path=str(tmp_path / "m.json"),
                        connections={"product-team": _connection(server.site)})
    assert found["first"] is True
    assert server.paths("POST") == ["/rest/api/3/search/jql"]


def test_a_listener_without_its_connection_asks_nothing(jira, tmp_path):
    server = jira()
    with pytest.raises(watch.WatchError) as caught:
        watch.check(_listening(READ), memory_path=str(tmp_path / "m.json"),
                    connections={})
    assert "No connection 'product-team'" in str(caught.value)
    assert server.seen == []


# -- the command line ---------------------------------------------------------
def _launcher(monkeypatch, capsys, *args):
    import runtime_paths
    import session_launcher
    monkeypatch.setattr("sys.argv", ["qavector"] + list(args))
    # --cycle-runs-dir is process-wide; it must not outlive this test.
    monkeypatch.setattr(runtime_paths, "_cycle_runs_override",
                        runtime_paths._cycle_runs_override)
    with pytest.raises(SystemExit) as caught:
        session_launcher.main()
    return caught.value.code, capsys.readouterr()


def test_a_run_naming_a_missing_connection_is_refused_before_it_starts(
        tmp_path, monkeypatch, capsys, caplog):
    cycles = tmp_path / "cycles"
    cycles.mkdir()
    (cycles / "c.yaml").write_text(
        "id: c\nsteps:\n  - id: queue\n    plugin: jira.issues\n    with:\n"
        "      connection: product-team\n      user: currentUser()\n", encoding="utf-8")
    monkeypatch.setenv("CMS_HOME", str(tmp_path))
    code, out = _launcher(monkeypatch, capsys, "--cycle-run=c",
                          "--cycles-dir=%s" % cycles,
                          "--cycle-runs-dir=%s" % (tmp_path / "runs"),
                          "--integrations-file=%s" % (tmp_path / "integrations.json"),
                          "--cycle-secrets-file=%s" % (tmp_path / "cyclesecrets.json"))
    assert code == 2
    assert "No connection 'product-team' in this project" in caplog.text
    assert not (tmp_path / "runs").exists() or not os.listdir(str(tmp_path / "runs"))


def test_the_cycle_list_says_which_connections_each_cycle_names(tmp_path, monkeypatch,
                                                                 capsys):
    cycles = tmp_path / "cycles"
    cycles.mkdir()
    (cycles / "c.yaml").write_text(
        "id: c\nvariables:\n  jira: ops\nsteps:\n  - id: queue\n    plugin: jira.issues\n"
        "    with:\n      connection: ${vars.jira}\n      user: currentUser()\n",
        encoding="utf-8")
    code, out = _launcher(monkeypatch, capsys, "--cycle-list",
                          "--cycles-dir=%s" % cycles)
    assert code == 0
    rows = json.loads(out.out)["cycles"]
    assert [row["connections"] for row in rows] == [["ops"]]


def test_connections_are_loaded_for_a_real_run(tmp_path, monkeypatch, capsys, jira):
    server = jira()
    integrations, secrets = str(tmp_path / "integrations.json"), str(tmp_path / "s.json")
    entry, problems = store.upsert(
        {"name": "Product team", "site": server.site, "email": EMAIL},
        integrations, secrets, new_token=TOKEN)
    assert problems == []
    cycles = tmp_path / "cycles"
    cycles.mkdir()
    (cycles / "c.yaml").write_text(
        "id: c\nsteps:\n  - id: queue\n    plugin: jira.issues\n    with:\n"
        "      connection: product-team\n      user: currentUser()\n"
        "      comments: false\n      attachments: none\n      subtasks: none\n",
        encoding="utf-8")
    code, out = _launcher(monkeypatch, capsys, "--cycle-run=c",
                          "--cycles-dir=%s" % cycles,
                          "--cycle-runs-dir=%s" % (tmp_path / "runs"),
                          "--integrations-file=%s" % integrations,
                          "--cycle-secrets-file=%s" % secrets)
    assert code == 0, out.err
    assert server.paths("POST") == ["/rest/api/3/search/jql"]
    assert TOKEN not in out.out + out.err


# -- what a run reads ---------------------------------------------------------
@pytest.fixture
def paths(tmp_path):
    return str(tmp_path / "integrations.json"), str(tmp_path / "cyclesecrets.json")


def _add(paths, name="Product team", token=TOKEN):
    saved, problems = store.upsert({"name": name, "site": "https://qa.example.invalid",
                                    "email": EMAIL}, paths[0], paths[1], new_token=token)
    assert problems == []
    return saved


def _plain_cycle(*settings, variables=None):
    from domain.cycle import Cycle, CycleStep
    steps = tuple(CycleStep("s%d" % index, "jira.issues", settings=dict(one))
                  for index, one in enumerate(settings))
    return Cycle(id="c", name="c", steps=steps, variables=dict(variables or {}))


def test_the_connections_a_cycle_names_literally_or_through_its_variables():
    cycle = _plain_cycle({"connection": "product-team"}, {"connection": "${vars.jira}"},
                         {"site": "https://x.example.invalid"}, variables={"jira": "ops"})
    assert binding.named_by(cycle, env={}) == (["product-team", "ops"], [])
    # A run's own value wins, as it does for every variable.
    assert binding.named_by(cycle, {"jira": "night"}, env={})[0] == ["product-team", "night"]


def test_a_connection_read_from_another_step_is_a_problem_before_the_run():
    ids, problems = binding.named_by(_plain_cycle(
        {"connection": "${steps.pick.outputs.key}"}), env={})
    assert ids == [] and problems[0].startswith("s0: connection")


def test_a_run_gets_only_the_connections_it_names_each_with_its_token(paths):
    _add(paths)
    _add(paths, name="Other", token="other-token")
    found, problems = binding.load(_plain_cycle({"connection": "product-team"}), None, *paths)
    assert problems == [] and list(found) == ["product-team"]
    assert found["product-team"]["token"] == TOKEN
    assert found["product-team"]["log"] == paths[0]


def test_a_run_naming_a_missing_or_tokenless_connection_is_refused(paths):
    saved = _add(paths)
    store.upsert(saved, paths[0], paths[1], clear_token=True)
    found, problems = binding.load(
        _plain_cycle({"connection": "product-team"}, {"connection": "nowhere"}), None, *paths)
    assert found == {}
    assert problems == [
        "Connection 'Product team' has no token. Set it in Integrations -> Jira -> Connections.",
        "No connection 'nowhere' in this project. Add it in Integrations -> Jira -> Connections."]


def test_a_cycle_naming_no_connection_reads_nothing(paths, monkeypatch):
    monkeypatch.setattr(store, "get", lambda *_: pytest.fail("read the file"))
    assert binding.load(_plain_cycle({"site": "https://x.example.invalid"}), None,
                        *paths) == ({}, [])


def test_the_integrations_package_knows_nothing_of_cycles():
    """The integration is the base; cycles use it. Nothing in it may import a
    cycle's concepts - only the shared secrets store and report renderer."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "integrations"
    allowed = {"from cycle import secrets as secret_store", "from cycle import jira_report"}
    for source in root.rglob("*.py"):
        for line in source.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(("from cycle", "import cycle")):
                assert stripped in allowed, "%s: %s" % (source.name, stripped)
