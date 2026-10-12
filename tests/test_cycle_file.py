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


# -- what a save answers with --------------------------------------------------
# A front-end used to read every cycle on the machine (--describe) and then the
# one it had just written (--cycle-show) after each save. The answer now carries
# both, and they have to be exactly what those two reads would have said.

_CYCLE = ("id: demo\nname: Demo\nproject: Portal\n"
          "steps:\n  - id: first  # keep me\n    plugin: command.shell\n"
          "    with:\n      command: echo hi\n")


def test_a_save_answers_with_the_row_and_the_cycle_as_opened(tmp_path):
    from cycle.cyclefile import list_cycles

    answer = save("demo", str(tmp_path), yaml_text=_CYCLE)
    assert answer["ok"]
    assert answer["row"] == list_cycles(str(tmp_path))[0]
    assert answer["shown"] == describe_cycle("demo", str(tmp_path))


def test_a_dry_run_answers_with_neither(tmp_path):
    answer = save("demo", str(tmp_path), yaml_text=_CYCLE, dry_run=True)
    assert "row" not in answer and "shown" not in answer


def test_moving_rewrites_only_the_project_and_keeps_comments(tmp_path):
    from cycle.cyclefile import move

    save("demo", str(tmp_path), yaml_text=_CYCLE)
    answer = move([{"id": "demo", "project": "Shop"}, {"id": "gone", "project": ""}],
                  str(tmp_path))

    assert [row["project"] for row in answer["moved"]] == ["Shop"]
    assert [one["id"] for one in answer["failed"]] == ["gone"]
    assert not answer["ok"]
    text = (tmp_path / "demo.yaml").read_text(encoding="utf-8")
    assert "project: Shop" in text and "# keep me" in text


def test_moving_out_of_every_project_drops_the_key(tmp_path):
    from cycle.cyclefile import move

    save("demo", str(tmp_path), yaml_text=_CYCLE)
    answer = move([{"id": "demo", "project": ""}], str(tmp_path))
    assert answer["ok"] and answer["moved"][0]["project"] == ""
    assert "project:" not in (tmp_path / "demo.yaml").read_text(encoding="utf-8")


def test_yaml_is_read_with_libyaml_when_it_is_there():
    from engine import yamlload

    expected = yaml.CSafeLoader if getattr(yaml, "__with_libyaml__", False) \
        else yaml.SafeLoader
    assert yamlload.LOADER is expected
    assert yamlload.safe_load("a: [1, two]\n") == {"a": [1, "two"]}


@pytest.mark.skipif(not getattr(yaml, "__with_libyaml__", False),
                    reason="PyYAML built without libyaml")
def test_libyaml_patches_a_file_exactly_as_the_pure_parser_did(monkeypatch):
    """cyclepatch slices the file by node positions. libyaml puts every node
    where the pure parser did, but names a plain scalar's style '' rather than
    None - this is the test that a patch made with it is the same file."""
    import copy
    import glob

    from cycle import cyclefile, model
    from engine import yamlload

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    checked = 0
    for path in sorted(glob.glob(os.path.join(here, "cycles", "*.yaml"))):
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        document = model.to_document(model.parse_cycle(yaml.safe_load(text), None))
        changed = copy.deepcopy(document)
        changed["description"] = (changed.get("description") or "") + " - changed"
        changed["project"] = "Somewhere"
        if changed.get("steps"):
            changed["steps"][-1]["timeout"] = 77
            changed["steps"][0].setdefault("with", {})["note"] = "a # not a comment"
        written = []
        for loader in (yaml.SafeLoader, yaml.CSafeLoader):
            monkeypatch.setattr(yamlload, "LOADER", loader)
            written.append(cyclefile.compose(changed, text))
        assert written[0] == written[1], path
        checked += 1
    assert checked
