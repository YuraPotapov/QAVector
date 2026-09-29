"""A form's save is an edit to the file, not a rewrite of it.

The development cycles carry a hundred lines of comments saying why each step
is there. Until saves became edits, changing one field in one step's dialog
wrote the whole file out again and every one of those comments went with it -
silently. What is defended here: an unchanged save changes nothing, a changed
field changes that field and nothing else, and anything a save *would* lose is
put to a person instead of being lost.
"""

import glob
import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cycle import cyclefile, cyclepatch, registry                     # noqa: E402

yaml = pytest.importorskip("yaml")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIPPED = sorted(os.path.splitext(os.path.basename(path))[0]
                 for path in glob.glob(os.path.join(ROOT, "cycles", "*.yaml")))

COMMENTED = """\
id: demo
name: Commented   # what the tree calls it
# Why this cycle exists, in the author's words.
variables:
  branch: main
  token: {secret: true}

steps:
  # -- the first step ------------------------------------------------------
  - id: one
    plugin: command.shell
    label: First
    with:
      command: echo hi   # say hello
      env:
        A: "1"
    timeout: 60

  # -- the second one waits for it ------------------------------------------
  - id: two
    plugin: command.shell
    needs: [one]
    with:
      command: |
        echo one
        echo two

  # The report is written whatever happened.
  - id: report
    plugin: report.json
    needs: [two]
    if: ${run.id}
"""


def _tree(tmp_path, text=COMMENTED, name="demo.yaml"):
    (tmp_path / name).write_text(text, encoding="utf-8")
    return str(tmp_path)


def _opened(tree, cycle_id="demo"):
    return cyclefile.describe_cycle(cycle_id, tree, registry)


def _step(document, step_id):
    return next(one for one in document["steps"] if one["id"] == step_id)


def _read(tree, name="demo.yaml"):
    with open(os.path.join(tree, name), encoding="utf-8") as handle:
        return handle.read()


# ------------------------------------------------------------- nothing changed
def test_saving_what_was_opened_changes_nothing(tmp_path):
    tree = _tree(tmp_path)
    document = _opened(tree)["document"]

    answer = cyclefile.save("demo", tree, document=document, registry=registry)

    assert answer["ok"], answer["problems"]
    assert _read(tree) == COMMENTED


@pytest.mark.parametrize("cycle_id", SHIPPED)
def test_every_shipped_cycle_survives_an_unchanged_save_byte_for_byte(tmp_path,
                                                                      cycle_id):
    shutil.copy(os.path.join(ROOT, "cycles", cycle_id + ".yaml"), str(tmp_path))
    before = _read(str(tmp_path), cycle_id + ".yaml")
    opened = _opened(str(tmp_path), cycle_id)

    answer = cyclefile.save(cycle_id, str(tmp_path), document=opened["document"],
                            registry=registry, base=opened["digest"])

    assert answer["ok"] and answer["lost"] == []
    assert _read(str(tmp_path), cycle_id + ".yaml") == before


@pytest.mark.parametrize("cycle_id", SHIPPED)
def test_every_shipped_cycle_keeps_its_comments_when_a_step_changes(tmp_path,
                                                                    cycle_id):
    shutil.copy(os.path.join(ROOT, "cycles", cycle_id + ".yaml"), str(tmp_path))
    before = _read(str(tmp_path), cycle_id + ".yaml")
    document = _opened(str(tmp_path), cycle_id)["document"]
    last = document["steps"][-1]
    last["label"] = "Renamed by the test"
    last["timeout"] = 321

    answer = cyclefile.save(cycle_id, str(tmp_path), document=document,
                            registry=registry)

    assert answer["ok"], answer["problems"]
    after = _read(str(tmp_path), cycle_id + ".yaml")
    assert cyclepatch.comments(after) == cyclepatch.comments(before)
    reopened = _opened(str(tmp_path), cycle_id)["document"]
    assert reopened == document


# -------------------------------------------------------------- one thing moved
def test_a_changed_setting_is_changed_where_it_stands(tmp_path):
    tree = _tree(tmp_path)
    document = _opened(tree)["document"]
    _step(document, "one")["with"]["command"] = "echo bye"

    assert cyclefile.save("demo", tree, document=document, registry=registry)["ok"]
    assert _read(tree) == COMMENTED.replace("command: echo hi   # say hello",
                                            "command: echo bye   # say hello")


def test_a_renamed_cycle_keeps_the_comment_on_its_name(tmp_path):
    tree = _tree(tmp_path)
    document = _opened(tree)["document"]
    document["name"] = "Renamed"

    cyclefile.save("demo", tree, document=document, registry=registry)
    assert "name: Renamed   # what the tree calls it" in _read(tree)


def test_a_block_of_text_is_rewritten_as_a_block_and_its_blank_line_kept(tmp_path):
    tree = _tree(tmp_path)
    document = _opened(tree)["document"]
    _step(document, "two")["with"]["command"] = "echo three\necho four\n"

    cyclefile.save("demo", tree, document=document, registry=registry)
    text = _read(tree)
    assert "      command: |\n        echo three\n        echo four\n\n  # The report" in text
    assert yaml.safe_load(text)["steps"][1]["with"]["command"] == "echo three\necho four\n"


def test_a_new_key_goes_where_the_writer_would_put_it(tmp_path):
    tree = _tree(tmp_path)
    document = _opened(tree)["document"]
    _step(document, "one")["group"] = "Prepare"

    cyclefile.save("demo", tree, document=document, registry=registry)
    assert "    label: First\n    group: Prepare\n    with:" in _read(tree)


def test_a_removed_key_takes_only_its_own_line(tmp_path):
    tree = _tree(tmp_path)
    document = _opened(tree)["document"]
    del _step(document, "one")["timeout"]

    cyclefile.save("demo", tree, document=document, registry=registry)
    assert _read(tree) == COMMENTED.replace("    timeout: 60\n", "")


def test_a_new_variable_is_added_beside_the_others(tmp_path):
    tree = _tree(tmp_path)
    document = _opened(tree)["document"]
    document["variables"]["depth"] = "3"

    cyclefile.save("demo", tree, document=document, registry=registry)
    text = _read(tree)
    assert "  branch: main\n  depth: \"3\"\n  token: {secret: true}\n" in text
    assert cyclepatch.lost_comments(COMMENTED, text) == []


# ------------------------------------------------------------------ the steps
def test_a_new_step_goes_after_the_one_it_follows(tmp_path):
    tree = _tree(tmp_path)
    document = _opened(tree)["document"]
    document["steps"].insert(1, {"id": "middle", "plugin": "command.shell",
                                 "needs": ["one"], "with": {"command": "true"}})

    assert cyclefile.save("demo", tree, document=document, registry=registry)["ok"]
    text = _read(tree)
    assert cyclepatch.lost_comments(COMMENTED, text) == []
    assert [one["id"] for one in yaml.safe_load(text)["steps"]] == [
        "one", "middle", "two", "report"]
    assert text.index("  - id: middle") < text.index("# -- the second one")


def test_a_removed_step_takes_only_itself(tmp_path):
    tree = _tree(tmp_path)
    document = _opened(tree)["document"]
    document["steps"] = [one for one in document["steps"] if one["id"] != "report"]

    assert cyclefile.save("demo", tree, document=document, registry=registry)["ok"]
    text = _read(tree)
    assert "id: report" not in text
    assert "# -- the first step" in text and "echo two" in text


def test_a_step_whose_id_changed_is_changed_in_place(tmp_path):
    """What the step dialog does when somebody renames the id."""
    tree = _tree(tmp_path)
    document = _opened(tree)["document"]
    _step(document, "report")["id"] = "summary"

    assert cyclefile.save("demo", tree, document=document, registry=registry)["ok"]
    assert _read(tree) == COMMENTED.replace("  - id: report", "  - id: summary")


# ------------------------------------------------------------ what cannot be kept
def test_steps_put_in_another_order_ask_before_losing_comments(tmp_path):
    tree = _tree(tmp_path)
    document = _opened(tree)["document"]
    one, two, report = document["steps"]
    document["steps"] = [one, report, two]
    report["needs"] = ["one"]
    two["needs"] = ["one"]

    answer = cyclefile.save("demo", tree, document=document, registry=registry)

    assert not answer["ok"] and answer["confirm"]
    assert "# -- the first step ------------------------------------------------------" in answer["lost"]
    assert answer["diff"].startswith("---")
    assert _read(tree) == COMMENTED, "nothing is written until somebody agrees"

    agreed = cyclefile.save("demo", tree, document=document, registry=registry,
                            allow_loss=True)
    assert agreed["ok"]
    assert [one["id"] for one in yaml.safe_load(_read(tree))["steps"]] == [
        "one", "report", "two"]


def test_text_typed_into_the_yaml_view_is_written_as_typed(tmp_path):
    """The person's own text: whatever they removed, they removed."""
    tree = _tree(tmp_path)
    text = COMMENTED.replace("# Why this cycle exists, in the author's words.\n", "")
    answer = cyclefile.save("demo", tree, yaml_text=text, registry=registry)
    assert answer["ok"] and _read(tree) == text


# ---------------------------------------------------------------- two editors
def test_a_file_changed_since_it_was_opened_is_not_overwritten(tmp_path):
    tree = _tree(tmp_path)
    opened = _opened(tree)
    elsewhere = COMMENTED.replace("echo hi", "echo from another editor")
    (tmp_path / "demo.yaml").write_text(elsewhere, encoding="utf-8")
    document = opened["document"]
    document["name"] = "Mine"

    answer = cyclefile.save("demo", tree, document=document, registry=registry,
                            base=opened["digest"])

    assert not answer["ok"] and answer["stale"]
    assert _read(tree) == elsewhere


def test_the_same_file_opened_twice_saves_the_second_time(tmp_path):
    tree = _tree(tmp_path)
    opened = _opened(tree)
    document = opened["document"]
    document["name"] = "First"
    first = cyclefile.save("demo", tree, document=document, registry=registry,
                           base=opened["digest"])
    document["name"] = "Second"
    second = cyclefile.save("demo", tree, document=document, registry=registry,
                            base=first["digest"])
    assert second["ok"] and "name: Second" in _read(tree)


# ----------------------------------------------------------------- a dry run
def test_a_dry_run_says_everything_and_writes_nothing(tmp_path):
    tree = _tree(tmp_path)
    document = _opened(tree)["document"]
    _step(document, "two")["needs"] = ["ghost"]

    answer = cyclefile.save("demo", tree, document=document, registry=registry,
                            dry_run=True)

    assert not answer["ok"]
    assert any("ghost" in one for one in answer["problems"])
    assert "needs: [ghost]" in answer["text"]
    assert "+    needs: [ghost]" in answer["diff"]
    assert [node["id"] for node in answer["cycle"]["nodes"]] == ["one", "two", "report"]
    assert _read(tree) == COMMENTED


# ------------------------------------------------------------- another file
def test_a_duplicate_keeps_the_comments_of_the_file_it_came_from(tmp_path):
    tree = _tree(tmp_path)
    document = dict(_opened(tree)["document"], id="copy", name="Copy")

    answer = cyclefile.save("copy", tree, document=document, registry=registry,
                            source_id="demo")

    assert answer["ok"]
    copied = _read(tree, "copy.yaml")
    assert copied.startswith("id: copy\nname: Copy   # what the tree calls it\n")
    assert cyclepatch.lost_comments(COMMENTED, copied) == []


def test_a_yml_file_is_saved_where_it_is(tmp_path):
    tree = _tree(tmp_path, name="demo.yml")
    document = _opened(tree)["document"]
    document["name"] = "Still yml"

    cyclefile.save("demo", tree, document=document, registry=registry)

    assert sorted(os.listdir(tree)) == ["demo.yml", "demo.yml.bak"]
    assert "name: Still yml" in _read(tree, "demo.yml")


def test_deleting_takes_every_spelling_so_nothing_comes_back(tmp_path):
    tree = _tree(tmp_path, name="demo.yml")
    (tmp_path / "demo.yaml").write_text(COMMENTED, encoding="utf-8")

    assert cyclefile.delete("demo", tree)["ok"]
    assert not any(name.startswith("demo.y") for name in os.listdir(tree))


# ------------------------------------------------------------------ comments
def test_a_hash_inside_a_value_is_not_a_comment():
    text = ('a: "x # y"\n'
            "b: |\n"
            "  # a heading in some prose\n"
            "c: [one, two]  # this one is\n")
    assert cyclepatch.comments(text) == ["# this one is"]


def test_prose_is_written_as_a_block_and_read_back_exactly():
    for text in ("one\ntwo\n", "one\ntwo", "  indented first\nline\n",
                 "a: b\n# c\n", "tab\there\n", "trailing space \nnext\n"):
        document = {"id": "x", "steps": [{"id": "s", "plugin": "command.shell",
                                          "with": {"command": text}}]}
        written = cyclefile.render(document)
        assert yaml.safe_load(written)["steps"][0]["with"]["command"] == text


# -------------------------------------------------------------- the launcher
def _launch(tmp_path, envelope, cycle_id="demo"):
    source = tmp_path / "envelope.json"
    source.write_text(json.dumps(envelope), encoding="utf-8")
    done = subprocess.run(
        [sys.executable, os.path.join(ROOT, "session_launcher.py"),
         "--cycle-save=" + cycle_id, "--from=" + str(source),
         "--cycles-dir=" + str(tmp_path / "tree")],
        capture_output=True, text=True, timeout=120)
    return json.loads(done.stdout)


def test_the_launcher_takes_an_envelope_and_still_takes_a_bare_document(tmp_path):
    (tmp_path / "tree").mkdir()
    tree = _tree(tmp_path / "tree")
    opened = _opened(tree)
    document = opened["document"]
    document["name"] = "From the envelope"

    dry = _launch(tmp_path, {"document": document, "dry_run": True,
                             "base": opened["digest"]})
    assert dry["ok"] and "From the envelope" in dry["text"]
    assert _read(tree) == COMMENTED

    wet = _launch(tmp_path, {"document": document, "base": opened["digest"]})
    assert wet["ok"] and "name: From the envelope   # what" in _read(tree)

    document["name"] = "Bare"
    assert _launch(tmp_path, document)["ok"]
    assert "name: Bare" in _read(tree)
