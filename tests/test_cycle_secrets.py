"""The store that holds the values a cycle file deliberately does not.

Every test writes to a tmp_path. None of them ever touches the real store, and
one of them says so out loud - the default path is asserted to be under the
user's data root and nowhere near a checkout, because "it wrote the credential
into the repository" is the failure this whole module exists to prevent.
"""

import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cycle import secrets                                       # noqa: E402
from cycle.model import CycleError, parse_cycle, to_document    # noqa: E402
from cycle.cyclefile import render                              # noqa: E402
from cycle import variables                                     # noqa: E402


@pytest.fixture
def store(tmp_path):
    """A store of our own, so nothing here can reach the user's."""
    return str(tmp_path / "secrets.json")


# ------------------------------------------------------------------- the store
def test_a_secret_goes_in_and_comes_back(store):
    secrets.set_secret("nightly", "token", "ghp_example", store)
    assert secrets.get("nightly", "token", store) == "ghp_example"


def test_the_value_is_not_in_the_file_in_plain_sight(store):
    secrets.set_secret("nightly", "token", "ghp_example", store)
    text = open(store, encoding="utf-8").read()
    assert "ghp_example" not in text
    assert "token" in text          # the name is not a secret, the value is


def test_the_store_and_its_key_are_readable_by_their_owner_alone(store):
    secrets.set_secret("nightly", "token", "ghp_example", store)
    key = os.path.join(os.path.dirname(store), secrets.KEY_NAME)
    for path in (store, key):
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert not mode & (stat.S_IRWXG | stat.S_IRWXO), path
    assert secrets.is_private(store)


def test_a_store_that_is_not_there_yet_is_empty_rather_than_an_error(store):
    assert secrets.load(store) == {}
    assert secrets.get("nightly", "token", store) == ""
    assert secrets.names("nightly", store) == []


def test_two_cycles_do_not_see_each_other_s_secrets(store):
    secrets.set_secret("nightly", "token", "one", store)
    secrets.set_secret("release", "token", "two", store)
    assert secrets.get("nightly", "token", store) == "one"
    assert secrets.get("release", "token", store) == "two"


def test_setting_one_secret_leaves_the_others_alone(store):
    secrets.set_secret("nightly", "a", "1", store)
    secrets.set_secret("nightly", "b", "2", store)
    secrets.set_secret("nightly", "a", "3", store)
    assert secrets.load(store)["nightly"] == {"a": "3", "b": "2"}


def test_an_empty_value_removes_the_secret(store):
    secrets.set_secret("nightly", "token", "ghp_example", store)
    secrets.set_secret("nightly", "token", "", store)
    assert secrets.names("nightly", store) == []


def test_a_whole_cycle_s_secrets_can_be_forgotten_at_once(store):
    secrets.set_secret("nightly", "a", "1", store)
    secrets.set_secret("nightly", "b", "2", store)
    secrets.set_secret("release", "c", "3", store)
    secrets.delete("nightly", path=store)
    assert secrets.names("nightly", store) == []
    assert secrets.names("release", store) == ["c"]


def test_names_says_which_are_set_and_never_what_they_are(store):
    secrets.set_secret("nightly", "token", "ghp_example", store)
    assert secrets.names("nightly", store) == ["token"]


def test_describe_answers_set_or_not_for_every_declared_name(store):
    secrets.set_secret("nightly", "token", "ghp_example", store)
    assert secrets.describe("nightly", ["token", "webhook"], store) == [
        {"name": "token", "set": True},
        {"name": "webhook", "set": False},
    ]


def test_one_unreadable_entry_does_not_hide_the_rest(store):
    """A value encrypted with a key that is gone must not take the file down."""
    secrets.set_secret("nightly", "good", "kept", store)
    document = json.load(open(store, encoding="utf-8"))
    document["secrets"]["nightly"]["broken"] = "not a fernet token at all"
    with open(store, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
    found = secrets.load(store)["nightly"]
    assert found == {"good": "kept"}


def test_a_missing_key_says_so_rather_than_answering_with_nothing(store):
    secrets.set_secret("nightly", "token", "ghp_example", store)
    os.unlink(os.path.join(os.path.dirname(store), secrets.KEY_NAME))
    with pytest.raises(secrets.SecretsError) as caught:
        secrets.load(store)
    assert "key" in str(caught.value)


def test_the_default_store_is_under_the_user_s_data_and_not_in_a_checkout():
    """The one assertion about the real path, and the reason for the module."""
    import runtime_paths
    where = secrets.default_path()
    assert os.path.dirname(where) == runtime_paths.user_data_root()
    assert os.path.basename(where) == secrets.FILE_NAME
    assert not where.endswith(os.path.join("cycles", secrets.FILE_NAME))


def test_a_configured_path_wins_over_the_default(tmp_path):
    mine = str(tmp_path / "elsewhere.json")
    assert secrets.resolve_path(mine) == mine
    assert secrets.resolve_path("") == secrets.default_path()
    assert secrets.resolve_path("  ") == secrets.default_path()


# -------------------------------------------------------- the cycle file shape
def test_a_secret_variable_is_declared_by_name_only():
    cycle = parse_cycle({"id": "n", "variables": {"branch": "main",
                                                  "token": {"secret": True}},
                         "steps": [{"id": "a", "plugin": "command.shell"}]}, "n")
    assert cycle.variables == {"branch": "main"}
    assert cycle.secrets == ("token",)


def test_a_value_in_the_cycle_file_is_refused():
    """The mistake this exists to stop: a real key typed into a committed file."""
    for shape in ({"secret": True, "value": "sk-live"},
                  {"secret": True, "default": "sk-live"}):
        with pytest.raises(CycleError) as caught:
            parse_cycle({"id": "n", "variables": {"token": shape},
                         "steps": [{"id": "a", "plugin": "command.shell"}]}, "n")
        assert "must not carry" in str(caught.value)


def test_a_mapping_that_does_not_say_secret_is_refused():
    with pytest.raises(CycleError) as caught:
        parse_cycle({"id": "n", "variables": {"token": {"a": 1}},
                     "steps": [{"id": "a", "plugin": "command.shell"}]}, "n")
    assert "secret: true" in str(caught.value)


def test_a_secret_survives_the_round_trip_through_the_document():
    raw = {"id": "n", "variables": {"branch": "main", "token": {"secret": True}},
           "steps": [{"id": "a", "plugin": "command.shell"}]}
    document = to_document(parse_cycle(raw, "n"))
    assert document["variables"] == {"branch": "main", "token": {"secret": True}}
    again = parse_cycle(document, "n")
    assert again.secrets == ("token",)


def test_the_rendered_yaml_says_secret_and_carries_no_value():
    text = render({"id": "n",
                   "variables": {"branch": "main", "token": {"secret": True}},
                   "steps": [{"id": "a", "plugin": "command.shell"}]})
    assert "token: {secret: true}" in text
    assert "branch: main" in text


# ----------------------------------------------------------- reaching a run
def test_a_secret_resolves_like_any_other_variable():
    cycle = parse_cycle({"id": "n", "variables": {"token": {"secret": True}},
                         "steps": [{"id": "a", "plugin": "command.shell"}]}, "n")
    root = variables.scope(None, cycle, {}, env={}, secrets={"token": "ghp_x"})
    assert variables.resolve("${vars.token}", root) == "ghp_x"


def test_a_secret_with_no_value_resolves_to_nothing_rather_than_failing():
    """A run that never reaches the step that needs it should still start."""
    cycle = parse_cycle({"id": "n", "variables": {"token": {"secret": True}},
                         "steps": [{"id": "a", "plugin": "command.shell"}]}, "n")
    root = variables.scope(None, cycle, {}, env={})
    assert variables.resolve("${vars.token}", root) == ""


def test_the_store_cannot_reintroduce_a_variable_the_cycle_no_longer_hides():
    """A stale entry left behind must not quietly become a value again."""
    cycle = parse_cycle({"id": "n", "variables": {"token": "plain now"},
                         "steps": [{"id": "a", "plugin": "command.shell"}]}, "n")
    root = variables.scope(None, cycle, {}, env={}, secrets={"token": "the old one"})
    assert variables.resolve("${vars.token}", root) == "plain now"


# ------------------------------------------- one credential, several cycles
# The same Jira token is wanted by every cycle that reads Jira, and typing it
# again for each is how one of them ends up with last month's.
def test_a_cycle_can_be_given_what_another_one_has(store):
    secrets.set_secret("todo_to_plan", "jira_token", "sekret", store)

    assert secrets.copy_to("todo_to_plan", "development", path=store) == \
        ["jira_token"]
    assert secrets.get("development", "jira_token", store) == "sekret"
    assert secrets.get("todo_to_plan", "jira_token", store) == "sekret", \
        "the one it came from keeps it"


def test_copying_leaves_a_secret_the_target_already_has(store):
    """Overwriting a credential somebody deliberately made different is the
    one outcome nobody could have asked for."""
    secrets.set_secret("from", "jira_token", "theirs", store)
    secrets.set_secret("to", "jira_token", "mine", store)

    assert secrets.copy_to("from", "to", path=store) == []
    assert secrets.get("to", "jira_token", store) == "mine"


def test_copying_takes_every_secret_unless_told_which(store):
    secrets.set_secret("from", "jira_token", "a", store)
    secrets.set_secret("from", "other", "b", store)

    assert secrets.copy_to("from", "to", path=store) == ["jira_token", "other"]

    secrets.delete("to", path=store)
    assert secrets.copy_to("from", "to", ["other"], path=store) == ["other"]
    assert secrets.names("to", store) == ["other"]


def test_copying_from_a_cycle_with_nothing_is_not_an_error(store):
    assert secrets.copy_to("empty", "to", path=store) == []
    assert secrets.names("to", store) == []


def test_a_cycle_cannot_copy_to_itself(store):
    secrets.set_secret("one", "jira_token", "a", store)
    assert secrets.copy_to("one", "one", path=store) == []


def test_a_name_that_is_not_there_is_skipped_rather_than_written_empty(store):
    secrets.set_secret("from", "jira_token", "a", store)

    assert secrets.copy_to("from", "to", ["nope"], path=store) == []
    assert secrets.names("to", store) == []


def test_the_value_is_not_what_comes_back(store):
    """It is decrypted and re-encrypted inside the call - which is the whole
    reason this is a store operation rather than a read and then a write."""
    secrets.set_secret("from", "jira_token", "sekret", store)
    answer = secrets.copy_to("from", "to", path=store)

    assert "sekret" not in repr(answer)
