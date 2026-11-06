"""A project's connections: the file, the token beside it, and what a run reads.

The property worth more than the rest: a token is never in the connections file,
never in an answer, and lives only in the project's encrypted secrets store.
"""

import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cycle import secrets as secret_store                          # noqa: E402
from integrations import store                                      # noqa: E402

TOKEN = "tok-3f9a-not-a-real-one"


@pytest.fixture
def paths(tmp_path):
    return str(tmp_path / "integrations.json"), str(tmp_path / "cyclesecrets.json")


def _add(paths, name="Product team", token=TOKEN, **fields):
    entry = dict({"name": name, "site": "https://qa.example.invalid",
                  "flavour": "cloud", "auth": "basic",
                  "email": "you@yourcompany.example"}, **fields)
    saved, problems = store.upsert(entry, paths[0], paths[1], new_token=token)
    assert problems == []
    return saved


def test_a_connection_gets_an_id_from_its_name_and_its_token_goes_elsewhere(paths):
    saved = _add(paths)
    assert saved["id"] == "product-team"
    text = open(paths[0], encoding="utf-8").read()
    assert TOKEN not in text
    assert "token" not in json.loads(text)["connections"][0]
    assert store.token("product-team", paths[1]) == TOKEN
    # Kept where every run is already told to look, under an entry no cycle
    # can be called.
    assert secret_store.load(paths[1])[store.CREDENTIALS] == {"product-team": TOKEN}


def test_both_files_are_readable_by_their_owner_alone(paths):
    _add(paths)
    for path in paths:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_renaming_keeps_the_id_cycles_use(paths):
    saved = _add(paths)
    renamed, problems = store.upsert(dict(saved, name="QA board"), paths[0], paths[1])
    assert problems == []
    assert renamed["id"] == "product-team"
    assert [one["name"] for one in store.load(paths[0])] == ["QA board"]


def test_a_second_connection_of_the_same_name_is_refused_and_ids_never_collide(paths):
    _add(paths)
    _entry, problems = store.upsert({"name": "product team", "site": "https://x.example.invalid",
                                     "email": "a@b.example"}, paths[0], paths[1])
    assert problems == ["A connection called 'product team' already exists."]
    other = _add(paths, name="Product-Team!")
    assert other["id"] == "product-team-2"


@pytest.mark.parametrize("fields, problem", [
    ({"site": ""}, "Site is required."),
    ({"site": "qa.example.invalid"}, "Site must start with http:// or https://."),
    ({"email": ""}, "Basic authentication needs the account email."),
    ({"flavour": "datacentre"}, "Product must be one of cloud, server."),
    ({"name": ""}, "Name is required."),
])
def test_what_a_connection_needs(paths, fields, problem):
    entry = dict({"name": "One", "site": "https://qa.example.invalid",
                  "email": "you@yourcompany.example"}, **fields)
    _entry, problems = store.upsert(entry, paths[0], paths[1])
    assert problem in problems
    assert store.load(paths[0]) == []


def test_a_bearer_token_needs_no_email(paths):
    saved = _add(paths, auth="bearer", email="", flavour="server")
    assert saved["auth"] == "bearer"


def test_an_empty_token_on_edit_keeps_the_one_set_and_clearing_removes_it(paths):
    saved = _add(paths)
    store.upsert(dict(saved, name="Renamed"), paths[0], paths[1], new_token="")
    assert store.token(saved["id"], paths[1]) == TOKEN
    store.upsert(saved, paths[0], paths[1], clear_token=True)
    assert store.token(saved["id"], paths[1]) == ""
    assert store.describe(paths[0], paths[1])[0]["token_set"] is False


def test_what_is_listed_says_whether_a_token_is_set_and_never_what_it_is(paths):
    _add(paths)
    listed = store.describe(paths[0], paths[1])
    assert listed[0]["token_set"] is True
    assert TOKEN not in json.dumps(listed)


def test_a_saved_answer_sent_back_does_not_write_token_set_into_the_file(paths):
    saved = _add(paths)
    store.upsert(dict(saved, token_set=True), paths[0], paths[1])
    assert "token_set" not in open(paths[0], encoding="utf-8").read()


def test_a_past_check_survives_a_rename_but_not_a_new_account(paths):
    saved = _add(paths)
    store.upsert(dict(saved, last_check={"ok": True, "account": "Tester"}),
                 paths[0], paths[1])
    renamed, _ = store.upsert(dict(saved, name="Other name"), paths[0], paths[1])
    assert renamed["last_check"]["account"] == "Tester"
    moved, _ = store.upsert(dict(renamed, site="https://other.example.invalid"),
                            paths[0], paths[1])
    assert "last_check" not in moved


def test_removing_a_connection_forgets_its_token(paths):
    saved = _add(paths)
    assert store.remove(saved["id"], paths[0], paths[1]) is True
    assert store.load(paths[0]) == []
    assert store.token(saved["id"], paths[1]) == ""
    assert store.remove(saved["id"], paths[0], paths[1]) is False


def test_the_tokens_entry_is_no_cycle_s_secret(paths):
    _add(paths)
    secret_store.set_secret("nightly", "jira_token", "other", paths[1])
    assert secret_store.names("nightly", paths[1]) == ["jira_token"]
    assert store.CREDENTIALS not in secret_store.names("nightly", paths[1])


def test_a_missing_file_is_no_connections_and_a_broken_one_says_so(paths, tmp_path):
    assert store.load(paths[0]) == []
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    with pytest.raises(store.StoreError):
        store.load(str(broken))


def test_the_default_path_is_never_the_checkout(own_integrations_store):
    assert store.resolve_path("") == own_integrations_store


# -- a connection ready to use ------------------------------------------------
def test_credentials_are_a_connection_with_its_token_and_where_to_log(paths):
    _add(paths)
    found = store.credentials("product-team", *paths)
    assert found["token"] == TOKEN and found["renew"]() == TOKEN
    assert found["api"] == "" and found["log"] == paths[0]


def test_credentials_for_a_missing_or_tokenless_connection_say_why(paths):
    with pytest.raises(store.Unavailable) as caught:
        store.credentials("nowhere", *paths)
    assert caught.value.kind == "not_found"
    saved = _add(paths)
    store.upsert(saved, paths[0], paths[1], clear_token=True)
    with pytest.raises(store.Unavailable) as caught:
        store.credentials("product-team", *paths)
    assert caught.value.kind == "no_token"


def test_a_setting_for_one_kind_of_user_is_dropped_from_old_files(paths):
    _add(paths)
    document = json.loads(open(paths[0], encoding="utf-8").read())
    document["connections"][0]["settings"]["cycle_writes"] = False
    open(paths[0], "w", encoding="utf-8").write(json.dumps(document))
    assert "cycle_writes" not in store.load(paths[0])[0]["settings"]


def test_what_credentials_add_is_never_written_back(paths):
    _add(paths)
    found = store.credentials("product-team", *paths)
    store.upsert(dict(found, name="Renamed"), *paths)
    written = open(paths[0], encoding="utf-8").read()
    assert TOKEN not in written
    for key in ("renew", '"log"', '"api"'):
        assert key not in written


# -- moving connections between projects --------------------------------------
def test_an_export_carries_each_connection_with_its_token(paths):
    _add(paths)
    _add(paths, name="Ops", token="ops-token")
    found = store.export(["ops"], *paths)
    assert [(one["id"], one["token"]) for one in found["connections"]] == [("ops", "ops-token")]
    assert store.export((), *paths)["connections"][0]["token"] == TOKEN


def test_an_import_keeps_ids_so_whatever_named_them_still_does(paths, tmp_path):
    _add(paths)
    exported = store.export((), *paths)
    there = (str(tmp_path / "there.json"), str(tmp_path / "there-secrets.json"))
    said = store.merge(exported, "merge", *there)
    assert said == ["Added connection Product team."]
    assert store.get("product-team", there[0]) is not None
    assert store.token("product-team", there[1]) == TOKEN


def test_an_import_onto_a_connection_already_here_adds_to_it_or_copies_it(paths):
    saved = _add(paths)
    store.upsert(saved, paths[0], paths[1], clear_token=True)
    incoming = {"connections": [dict(saved, token="incoming")]}
    assert store.merge(incoming, "merge", *paths) == [
        "Gave connection Product team the token it lacked."]
    assert store.merge(incoming, "merge", *paths) == ["Kept connection Product team as it was."]
    assert store.merge(incoming, "copy", *paths) == ["Added connection Product team (copy)."]
    copy = store.get("product-team-copy", paths[0])
    assert copy["name"] == "Product team (copy)"
    assert store.token("product-team-copy", paths[1]) == "incoming"


def test_the_own_oauth_app_travels_with_its_secret_and_is_never_overwritten(paths, tmp_path):
    from integrations import oauth
    _add(paths)
    assert oauth.save_app("client-1", "app-secret", 50000, *paths) == []
    exported = store.export((), *paths)
    assert exported["integration_apps"][0]["secret"] == "app-secret"
    there = (str(tmp_path / "there.json"), str(tmp_path / "there-secrets.json"))
    store.merge(exported, "merge", *there)
    assert oauth.app(*there)["client_id"] == "client-1" and oauth.app(*there)["secret_set"]
    assert oauth.save_app("client-2", "other", 50000, *there) == []
    store.merge(exported, "merge", *there)
    assert oauth.app(*there)["client_id"] == "client-2"


def test_the_export_command_writes_tokens_only_into_an_owner_only_file(paths, tmp_path,
                                                                       capsys):
    from integrations import cli
    _add(paths)
    target = tmp_path / "out.json"
    code = cli.main(["export", "--to=%s" % target, "--integrations-file=%s" % paths[0],
                     "--cycle-secrets-file=%s" % paths[1]])
    printed = capsys.readouterr().out
    assert code == 0 and json.loads(printed)["ids"] == ["product-team"]
    assert TOKEN not in printed
    assert oct(os.stat(target).st_mode & 0o777) == "0o600"
    assert json.loads(target.read_text())["connections"][0]["token"] == TOKEN
