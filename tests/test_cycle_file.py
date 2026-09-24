"""Writing a cycle back out: what the YAML says, and that it says the same thing.

The property worth defending here is a round trip. A cycle opened in the
interface and saved with nothing changed has to come back byte-for-byte
equivalent - not merely parse. Everything a plugin may declare travels through
``with:``, so ``with:`` is where that goes wrong first: a mapping or a list
rendered as a Python repr reads back as a string, and the step that opened fine
a moment ago now fails validation and cannot be saved at all.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cycle.cyclefile import describe_cycle, render, save           # noqa: E402

yaml = pytest.importorskip("yaml")


def _round_trip(with_):
    """One step's ``with:`` through render and back, as YAML reads it."""
    text = render({"id": "x", "steps": [{"id": "s", "plugin": "command.shell",
                                         "with": with_}]})
    return yaml.safe_load(text)["steps"][0]["with"], text


def test_a_plain_mapping_survives():
    before = {"command": "echo hi", "timeout_seconds": 4}
    assert _round_trip(before)[0] == before


def test_a_nested_mapping_stays_a_mapping():
    """The bug this file exists for: agent.review's structured inputs came back
    as the string "{'expected_stdout': ...}" and the step stopped validating."""
    before = {"inputs": {"expected_stdout": "Hello!", "runtime": "Python 3"}}
    after, text = _round_trip(before)
    assert after == before
    assert "inputs:" in text and "{'" not in text


def test_a_newline_inside_a_nested_value_survives():
    before = {"inputs": {"expected_stdout": "Hello, QAVector!\n"}}
    assert _round_trip(before)[0] == before


def test_a_list_stays_a_list():
    before = {"files": ["steps/a/stdout.log", "steps/b/stdout.log"]}
    after, text = _round_trip(before)
    assert after == before
    assert "[steps/a/stdout.log, steps/b/stdout.log]" in text


def test_a_list_item_with_a_comma_is_not_written_in_flow_form():
    """A comma inside [a, b] would end the item early and invent another."""
    before = {"files": ["one,two", "three"]}
    after, text = _round_trip(before)
    assert after == before
    assert "- one,two" in text


def test_a_list_of_mappings_survives():
    before = {"matrix": [{"name": "a", "jobs": 2}, {"name": "b", "jobs": 1}]}
    assert _round_trip(before)[0] == before


def test_mappings_nest_as_deeply_as_a_plugin_asks():
    before = {"a": {"b": {"c": {"d": "deep"}}}}
    assert _round_trip(before)[0] == before


def test_an_empty_mapping_or_list_is_still_one_when_it_comes_back():
    before = {"nothing": {}, "none": [], "something": "x"}
    assert _round_trip(before)[0] == before


@pytest.mark.parametrize("text", [
    "1.0", "true", "false", "null", "no", "on", "~",          # the obvious ones
    "12:30", "2026-09-19", ".inf", ".nan", "0x1f", "0o17",    # the quiet ones
    "- not a list", "# not a comment", "a: b", " leading", "trailing ",
    "", "007", "1e5", "yes",
])
def test_a_value_that_looks_like_something_else_still_comes_back_as_text(text):
    """Every one of these is a value somebody typed into a form. "12:30" read
    back as the number 750 until the writer started asking the parser."""
    assert _round_trip({"inputs": {"v": text}})[0] == {"inputs": {"v": text}}


# --------------------------------------------------------------- the whole file
def test_a_cycle_written_and_read_again_says_the_same_thing():
    document = {
        "id": "nightly", "name": "Nightly", "project": "Demo",
        "variables": {"branch": "main", "token": {"secret": True}},
        "steps": [
            {"id": "a", "plugin": "command.shell", "with": {"command": "echo"}},
            {"id": "b", "plugin": "agent.review", "needs": ["a"],
             "timeout": 300, "retry": {"attempts": 2, "delay": 1.5},
             "with": {"task": "Review it", "files": ["a.log"],
                      "inputs": {"expected": "ok\n"}, "max_iterations": 4}},
        ],
    }
    back = yaml.safe_load(render(document))
    assert back["steps"] == document["steps"]
    assert back["variables"] == document["variables"]


@pytest.mark.parametrize("path", [
    "/src/app", "/src/my app", "/src/app, [copy]", "/src/app # copy",
    "/src/\u043f\u0440\u043e\u0454\u043a\u0442",  # a Cyrillic folder name
    r"C:\Users\Tester\My Project", "", "null",
])
def test_a_path_keeps_its_type_and_value_after_saving_and_reopening(tmp_path, path):
    document = {
        "id": "demo",
        "variables": {"project_dir": {"kind": "path", "value": path},
                      "branch": "main", "token": {"secret": True}},
        "steps": [{"id": "check", "plugin": "command.shell",
                   "with": {"command": "echo hi", "dir": "${vars.project_dir}"}}],
    }
    # The Properties dialog saves a document, then reads it back from the core.
    # Repeat once to cover an unchanged Save after reopening as well.
    for _ in range(2):
        result = save("demo", str(tmp_path), document=document)
        assert result["ok"], result["problems"]
        opened = describe_cycle("demo", str(tmp_path))
        assert opened["problems"] == []
        assert opened["document"]["variables"] == document["variables"]
        assert opened["cycle"]["secrets"] == ["token"]
        assert opened["cycle"]["paths"] == ["project_dir"]
        document = opened["document"]
