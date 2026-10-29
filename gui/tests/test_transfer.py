"""Export and import: what each choice brings along, and what an import does
with what is already here."""

import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import core, cycleprojectsfile as cpf, launchprojectsfile as lpf, \
    transfer, usersfile

LOCAL, STAGE = "localhost:8069", "https://stage.example.invalid/"


def _inventory():
    return core.Inventory({"envs": [
        {"alias": "localhost", "value": LOCAL, "origin": "http://localhost:8069",
         "count": 2},
        {"alias": "stage", "value": STAGE, "origin": "https://stage.example.invalid",
         "count": 1}]})


def _projects():
    return [lpf.ProjectRow(lpf.WORKSPACE, lpf.WORKSPACE_NAME, [
        lpf.EnvironmentRow("env:" + LOCAL, "Local", LOCAL, "http://localhost:{{port}}",
                           {"port": "8069"})])]


def _rows():
    row = usersfile.UserRow
    return [row(LOCAL, "Agent", "agent", "pw", ["smoke"]),
            row(LOCAL, "Admin", "admin", "pw"),
            row(STAGE, "Agent", "agent", "pw", ["tag:access"])]


def _catalog(projects=None):
    return lpf.catalog(projects if projects is not None else _projects(), _inventory())


# ------------------------------------------------------------- exporting

def test_a_project_brings_its_environments_and_their_users():
    part = transfer.launch_part(_projects(), _catalog(), _rows(),
                                project_ids=[lpf.WORKSPACE])
    assert part["projects"] == [{"name": "Workspace"}]
    assert [(e["name"], e["variables"]) for e in part["environments"]] == [
        ("Local", {"url": "http://localhost:{{port}}", "port": "8069"}),
        ("stage", {"url": "https://stage.example.invalid"})]
    agent = [u for u in part["users"] if u["login"] == "agent"][0]
    assert agent["password"] == "pw"
    assert agent["environments"] == [
        {"project": "Workspace", "environment": "Local", "scenarios": ["smoke"]},
        {"project": "Workspace", "environment": "stage", "scenarios": ["tag:access"]}]


def test_an_environment_brings_only_its_own_users_and_places():
    part = transfer.launch_part(_projects(), _catalog(), _rows(),
                                env_ids=["env:" + STAGE])
    assert [e["name"] for e in part["environments"]] == ["stage"]
    assert [(u["login"], [p["environment"] for p in u["environments"]])
            for u in part["users"]] == [("agent", ["stage"])]


def test_a_user_brings_every_place_it_signs_in_to_by_name():
    part = transfer.launch_part(_projects(), _catalog(), _rows(),
                                people=[("agent", "pw", "Agent")])
    assert part["environments"] == [] and part["projects"] == []
    assert [p["environment"] for p in part["users"][0]["environments"]] == \
        ["Local", "stage"]


def test_a_cycle_project_brings_its_cycles_and_their_secrets(tmp_path):
    path = tmp_path / "ready.yaml"
    path.write_text("name: Ready\nproject: Claim\n", encoding="utf-8")
    rows = [{"id": "ready", "project": "Claim", "path": str(path)},
            {"id": "other", "project": "", "path": str(path)}]
    part = transfer.cycles_part(rows, project_names=["Claim"],
                                cycle_projects=[cpf.ProjectRow("Claim", "QA")],
                                secrets=lambda cycle_id: {"token": "t-1"})
    assert part["cycle_projects"] == [{"name": "Claim", "description": "QA",
                                       "colour": ""}]
    assert [(c["id"], c["secrets"]) for c in part["cycles"]] == [("ready",
                                                                  {"token": "t-1"})]


def test_the_file_is_private_and_reads_back(tmp_path):
    document = transfer.bundle(transfer.launch_part(
        _projects(), _catalog(), _rows(), project_ids=[lpf.WORKSPACE]))
    path = str(tmp_path / ("everything" + transfer.SUFFIX))
    transfer.write(path, document)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    again = transfer.read(path)
    assert again["users"] == document["users"]
    assert transfer.counts(again) == "1 project, 2 environments, 2 users"


def test_a_cycle_file_reads_as_one_cycle_and_anything_else_is_refused(tmp_path):
    cycle = tmp_path / "ready.yaml"
    cycle.write_text("name: Ready\nproject: \"Claim\"\n", encoding="utf-8")
    document = transfer.read(str(cycle))
    assert [(c["id"], c["project"]) for c in document["cycles"]] == [("ready", "Claim")]
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"projects": []}))
    with pytest.raises(transfer.TransferError):
        transfer.read(str(other))
    newer = tmp_path / "newer.json"
    newer.write_text(json.dumps({"format": transfer.FORMAT,
                                 "version": transfer.VERSION + 1}))
    with pytest.raises(transfer.TransferError):
        transfer.read(str(newer))


# ------------------------------------------------------------- importing

def _document():
    return transfer.bundle({
        "projects": [{"name": "Workspace"}],
        "environments": [{"project": "Workspace", "name": "Local",
                          "variables": {"url": "http://localhost:9000",
                                        "port": "9000", "db": "main"}}],
        "users": [{"role": "Agent", "login": "agent", "password": "other",
                   "environments": [{"project": "Workspace", "environment": "Local",
                                     "scenarios": ["smoke"]}]},
                  {"role": "Viewer", "login": "viewer", "password": "v",
                   "environments": [{"project": "Workspace", "environment": "Local",
                                     "scenarios": []},
                                    {"project": "Gone", "environment": "Nowhere",
                                     "scenarios": []}]}]})


def test_the_plan_says_what_is_already_here():
    items = transfer.plan(_document(), _projects(), _catalog(), _rows(),
                          cycle_ids=["ready"])
    assert [(i.kind, i.name, i.exists) for i in items] == [
        ("Project", "Workspace", True), ("Environment", "Workspace / Local", True),
        ("User", "agent", True), ("User", "viewer", False)]


def test_added_to_the_existing_ones_nothing_of_yours_changes():
    projects, rows = _projects(), _rows()
    said = transfer.apply_launch(_document(), transfer.MERGE, projects, _catalog(),
                                 rows)
    local = projects[0].environments[0]
    assert local.variables == {"url": "http://localhost:{{port}}", "port": "8069",
                               "db": "main"}
    # agent was in Local already: kept, password and all.
    assert [(r.env, r.password) for r in rows if r.login == "agent"] == [
        (LOCAL, "pw"), (STAGE, "pw")]
    assert [(r.env, r.cls) for r in rows if r.login == "viewer"] == [(LOCAL, "Viewer")]
    assert any("Gone / Nowhere is not here" in line for line in said)
    assert "Into Local: viewer." in said
    assert usersfile.validate(rows) == [] and lpf.validate(projects) == []


def test_added_as_copies_everything_sits_beside_what_is_here():
    projects, rows = _projects(), _rows()
    transfer.apply_launch(_document(), transfer.COPY, projects, _catalog(), rows)
    names = [env.name for env in projects[0].environments]
    assert names == ["Local", "Local (copy)"]
    copy = projects[0].environments[1]
    assert copy.account_key == "local-copy" and copy.address == "http://localhost:9000"
    agents = [(r.env, r.cls, r.password) for r in rows if r.login == "agent"]
    assert ("local-copy", "Agent (copy)", "other") in agents
    assert usersfile.validate(rows) == [] and lpf.validate(projects) == []


def test_into_an_empty_place_everything_is_new(tmp_path):
    projects, rows = [], []
    document = _document()
    document["projects"] = [{"name": "Helpdesk"}]
    for env in document["environments"]:
        env["project"] = "Helpdesk"
    for user in document["users"]:
        for place in user["environments"]:
            place["project"] = "Helpdesk" if place["project"] == "Workspace" \
                else place["project"]
    transfer.apply_launch(document, transfer.MERGE, projects, [], rows)
    assert [p.name for p in projects] == ["Helpdesk"]
    env = projects[0].environments[0]
    assert (env.name, env.account_key) == ("Local", "local")
    assert [(r.env, r.login, r.tests) for r in rows] == [
        ("local", "agent", ["smoke"]), ("local", "viewer", [])]


class FakeCore:
    def __init__(self):
        self.calls = []

    def cycle_import(self, path):
        self.calls.append(("import", os.path.basename(path), open(path).read()))
        return {"ok": True}

    def cycle_save(self, cycle_id, document):
        self.calls.append(("save", cycle_id, document["yaml"]))
        return {"ok": True}

    def cycle_move(self, moves):
        self.calls.append(("move", moves))
        return {"ok": True}

    def cycle_secret_set(self, cycle_id, name, value):
        self.calls.append(("secret", cycle_id, name, value))
        return {"ok": True}


def _cycles():
    return transfer.bundle({
        "cycle_projects": [{"name": "Claim", "description": "", "colour": ""}],
        "cycles": [{"id": "ready", "project": "Claim",
                    "yaml": "id: ready\nname: Ready\nproject: Claim\n",
                    "secrets": {"token": "t-1"}},
                   {"id": "fresh", "project": "Claim", "yaml": "name: Fresh\n"}]})


def test_cycles_added_to_the_existing_ones_update_them(tmp_path):
    path = str(tmp_path / cpf.FILE_NAME)
    cpf.save(path, [cpf.ProjectRow("Claim")])
    fake = FakeCore()
    transfer.apply_cycles(_cycles(), transfer.MERGE, fake, path, ["ready"])
    assert fake.calls[0] == ("save", "ready", "id: ready\nname: Ready\nproject: Claim\n")
    assert ("secret", "ready", "token", "t-1") in fake.calls
    assert ("import", "fresh.yaml", "name: Fresh\n") in fake.calls
    assert [p.name for p in cpf.load(path)] == ["Claim"]


def test_cycles_added_as_copies_get_an_id_a_name_and_a_project_of_their_own(tmp_path):
    path = str(tmp_path / cpf.FILE_NAME)
    cpf.save(path, [cpf.ProjectRow("Claim")])
    fake = FakeCore()
    transfer.apply_cycles(_cycles(), transfer.COPY, fake, path, ["ready"])
    assert ("import", "ready_copy.yaml",
            "id: ready_copy\nname: Ready (copy)\nproject: Claim\n") in fake.calls
    assert ("move", [{"id": "ready_copy", "project": "Claim (copy)"}]) in fake.calls
    assert ("secret", "ready_copy", "token", "t-1") in fake.calls
    assert [p.name for p in cpf.load(path)] == ["Claim", "Claim (copy)"]
