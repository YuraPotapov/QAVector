"""users.json round-tripping and validation.

Every rule tested here is one ``session_launcher.load_users`` would exit on: the
GUI must never write a file the CLI then refuses to read.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import usersfile


def _write(path, entries):
    path.write_text(json.dumps(entries), encoding="utf-8")
    return str(path)


def test_missing_file_is_no_rows_not_an_error(tmp_path):
    assert usersfile.load(str(tmp_path / "nope.json")) == []


def test_reads_the_launchers_schema(tmp_path):
    path = _write(tmp_path / "users.json", [
        {"env": "localhost:8069", "class": "Admin", "login": "admin",
         "password": "pw", "run-tests": ["smoke", "tag:access"]}])
    row = usersfile.load(path)[0]
    assert (row.env, row.cls, row.login, row.password) == (
        "localhost:8069", "Admin", "admin", "pw")
    assert row.tests == ["smoke", "tag:access"]


def test_accepts_the_retired_prefix_key_like_the_launcher_does(tmp_path):
    path = _write(tmp_path / "users.json",
                  [{"prefix": "localhost:8069", "class": "A", "login": "l",
                    "password": "p"}])
    assert usersfile.load(path)[0].env == "localhost:8069"


def test_tests_may_be_a_comma_separated_string(tmp_path):
    path = _write(tmp_path / "users.json",
                  [{"env": "e", "class": "A", "login": "l", "password": "p",
                    "tests": "one, two"}])
    assert usersfile.load(path)[0].tests == ["one", "two"]


def test_unknown_keys_survive_a_round_trip(tmp_path):
    # People keep comments in this file; an editor that eats them is a bad editor.
    path = _write(tmp_path / "users.json",
                  [{"_comment": "keep me", "env": "e", "class": "A", "login": "l",
                    "password": "p"}])
    rows = usersfile.load(path)
    usersfile.save(path, rows)
    assert json.loads(open(path, encoding="utf-8").read())[0]["_comment"] == "keep me"


def test_broken_json_is_reported_with_the_path(tmp_path):
    path = tmp_path / "users.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(usersfile.UsersFileError) as exc:
        usersfile.load(str(path))
    assert "users.json" in str(exc.value)


# ------------------------------------------------------------------ validation

def _row(**kwargs):
    base = {"env": "localhost:8069", "cls": "Admin", "login": "admin",
            "password": "pw"}
    base.update(kwargs)
    return usersfile.UserRow(**base)


def test_a_complete_row_has_no_problems():
    assert usersfile.validate([_row()]) == []


@pytest.mark.parametrize("field", ["cls", "login", "password"])
def test_required_fields_are_reported(field):
    problems = usersfile.validate([_row(**{field: ""})])
    assert len(problems) == 1 and "required" in problems[0]


def test_duplicate_env_plus_login_is_rejected():
    # That pair names the profile folder, so the launcher exits on it.
    problems = usersfile.validate([_row(), _row()])
    assert any("must be unique" in p for p in problems)


def test_the_same_login_under_different_envs_is_fine():
    assert usersfile.validate([_row(), _row(env="https://other.example.com/")]) == []


def test_the_plural_tag_selector_is_caught():
    problems = usersfile.validate([_row(tests=["tags:access"])])
    assert any("tag:access" in p for p in problems)


def test_save_refuses_an_invalid_set(tmp_path):
    path = str(tmp_path / "users.json")
    with pytest.raises(usersfile.UsersFileError):
        usersfile.save(path, [_row(login="")])
    assert not os.path.exists(path)


# ---------------------------------------------------------------------- saving

def test_save_keeps_one_backup(tmp_path):
    path = _write(tmp_path / "users.json",
                  [{"env": "e", "class": "A", "login": "first", "password": "p"}])
    usersfile.save(path, [_row(login="second")])
    assert json.loads(open(path, encoding="utf-8").read())[0]["login"] == "second"
    assert json.loads(open(path + ".bak", encoding="utf-8").read())[0]["login"] == "first"


def test_saved_file_is_what_the_launcher_expects(tmp_path):
    path = str(tmp_path / "users.json")
    usersfile.save(path, [_row(tests=["smoke"])])
    entry = json.loads(open(path, encoding="utf-8").read())[0]
    assert set(entry) == {"env", "class", "login", "password", "run-tests"}
    assert entry["run-tests"] == ["smoke"]


def test_an_empty_tests_list_is_omitted_entirely(tmp_path):
    # The launcher exits on "run-tests": [] - "drop the field instead".
    path = str(tmp_path / "users.json")
    usersfile.save(path, [_row(tests=[])])
    assert "run-tests" not in json.loads(open(path, encoding="utf-8").read())[0]


def test_fingerprint_notices_a_change(tmp_path):
    path = _write(tmp_path / "users.json", [{"env": "e", "class": "A",
                                             "login": "l", "password": "p"}])
    before = usersfile.fingerprint(path)
    usersfile.save(path, [_row()])
    assert usersfile.fingerprint(path) != before


# ---------------------------------------- env values the launcher cannot read

def _rows(*envs):
    return [usersfile.UserRow(env=env, cls="A", login="u%d" % i, password="p")
            for i, env in enumerate(envs)]


def test_a_placeholder_in_an_env_value_is_refused():
    problems = usersfile.validate(_rows("localhost:{{port}}"))
    assert any("placeholder" in p for p in problems)


def test_an_unreadable_port_in_an_env_value_is_refused():
    assert any("port" in p for p in usersfile.validate(_rows("localhost:99999")))


@pytest.mark.parametrize("first, second", [
    ("localhost:8069", "http://localhost:8069/"),
    ("https://shop.example.invalid/", "https://shop.example.invalid/x"),
])
def test_one_server_spelled_two_ways_is_refused(first, second):
    problems = usersfile.validate(_rows(first, second))
    assert any("same server" in p for p in problems)


def test_different_servers_with_the_same_host_are_fine():
    assert usersfile.validate(_rows("localhost:8069", "localhost:8070",
                                    "https://a.example.invalid/",
                                    "https://b.example.invalid/")) == []


def test_the_collision_rule_matches_the_launcher():
    """Mirrored, not imported: checked against the core's own naming."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
    session_launcher = pytest.importorskip("session_launcher")
    samples = [["localhost:8069", "http://localhost:8069/"],
               ["localhost:8069", "localhost:8070"],
               ["https://a.example.invalid/", "https://a.example.invalid/x"],
               ["https://a.example.invalid/", "https://a.other.invalid/"],
               ["127.0.0.1:8069", "localhost:8069"]]
    for values in samples:
        try:
            session_launcher.build_environments(values)
            core_refuses = False
        except AssertionError:
            core_refuses = True
        assert bool(usersfile.env_problems(values)) == core_refuses, values


# ---------------------------------------------- people and where they sign in

def _people():
    row = usersfile.UserRow
    return [row("local", "Agent", "agent", "pw", ["smoke"]),
            row("stage", "Agent", "agent", "pw", ["tag:access"]),
            row("local", "Admin", "admin", "pw-local"),
            row("stage", "Admin", "admin", "pw-stage"),
            row("", "Tester", "new", "pw")]


def test_a_person_is_every_row_with_one_login_password_and_role():
    rows = _people()
    people = usersfile.credentials(rows)
    assert [(p.login, p.role, p.envs(rows)) for p in people] == [
        ("agent", "Agent", ["local", "stage"]),
        ("admin", "Admin", ["local"]),
        ("admin", "Admin", ["stage"]),
        ("new", "Tester", [])]


def test_rows_still_being_typed_are_people_of_their_own():
    rows = [usersfile.UserRow("", "", "", ""), usersfile.UserRow("", "", "", "")]
    assert len(usersfile.credentials(rows)) == 2


def test_assigning_moves_a_loose_row_in_or_adds_one_for_the_place():
    rows = _people()
    usersfile.assign(rows, ("new", "pw", "Tester"), "local")
    assert (rows[4].env, len(rows)) == ("local", 5)
    usersfile.assign(rows, ("new", "pw", "Tester"), "stage", ["smoke"])
    assert (rows[5].env, rows[5].login, rows[5].tests) == ("stage", "new", ["smoke"])
    assert usersfile.assign(rows, ("new", "pw", "Tester"), "stage") is False
    assert usersfile.validate(rows) == []


def test_one_login_cannot_be_in_a_place_twice():
    rows = _people()
    with pytest.raises(usersfile.UsersFileError):
        usersfile.assign(rows, ("admin", "pw-stage", "Admin"), "local")


def test_leaving_the_last_place_keeps_the_person():
    rows = _people()
    usersfile.unassign(rows, ("agent", "pw", "Agent"), "stage")
    assert [(r.env, r.login) for r in rows if r.login == "agent"] == [("local", "agent")]
    usersfile.unassign(rows, ("agent", "pw", "Agent"), "local")
    agent = [r for r in rows if r.login == "agent"]
    assert [(r.env, r.tests) for r in agent] == [("", [])]


def test_a_change_to_a_person_reaches_every_row_of_theirs():
    rows = _people()
    usersfile.update_credential(rows, ("agent", "pw", "Agent"), password="new-pw")
    assert {r.password for r in rows if r.login == "agent"} == {"new-pw"}
    assert [r.password for r in rows if r.login == "admin"] == ["pw-local", "pw-stage"]
    usersfile.remove_credential(rows, ("agent", "new-pw", "Agent"))
    assert all(r.login != "agent" for r in rows)


def test_scenarios_belong_to_the_person_in_one_place():
    rows = _people()
    usersfile.set_tests(rows, "stage", "agent", ["smoke", "tag:x"])
    assert [r.tests for r in rows if r.login == "agent"] == [["smoke"], ["smoke", "tag:x"]]
    with pytest.raises(usersfile.UsersFileError):
        usersfile.set_tests(rows, "stage", "new", ["smoke"])


def test_copying_and_dropping_a_place():
    rows = _people()
    usersfile.copy_env(rows, "local", "copy")
    assert [(r.login, r.tests) for _i, r in usersfile.members(rows, "copy")] == [
        ("agent", ["smoke"]), ("admin", [])]
    usersfile.drop_env(rows, "local")
    assert usersfile.members(rows, "local") == []
    # admin/pw-local was only there: still in the list, without a place.
    assert [(r.env, r.password) for r in rows if r.login == "admin"] == [
        ("stage", "pw-stage"), ("copy", "pw-local")]
    usersfile.drop_env(rows, "copy")
    assert [(r.env, r.password) for r in rows if r.login == "admin"] == [
        ("stage", "pw-stage"), ("", "pw-local")]
    assert usersfile.validate(rows) == []


def test_two_without_a_place_with_one_login_are_said_plainly():
    rows = [usersfile.UserRow("", "A", "same", "1"), usersfile.UserRow("", "B", "same", "2")]
    assert any("without an environment" in p for p in usersfile.validate(rows))


def test_change_reads_applies_and_saves_or_leaves_the_file(tmp_path):
    path = _write(tmp_path / "users.json", [
        {"env": "local", "class": "Agent", "login": "agent", "password": "pw"}])
    usersfile.change(path, lambda rows: usersfile.copy_env(rows, "local", "stage"))
    assert [r["env"] for r in json.load(open(path))] == ["local", "stage"]
    before = open(path).read()
    with pytest.raises(usersfile.UsersFileError):
        usersfile.change(path, lambda rows: rows.append(rows[0].copy()))
    assert open(path).read() == before
