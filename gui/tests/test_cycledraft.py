"""An unsaved edit of a cycle: its changes, and the way back through them.

No Qt: a draft is a document and a history of documents, and everything the
editor does to one is a function of it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui.cycledraft import Draft


def draft():
    return Draft("demo", {"id": "demo", "steps": [
        {"id": "a", "plugin": "command.shell", "group": "Prepare"},
        {"id": "b", "plugin": "command.shell", "needs": ["a"],
         "with": {"command": "echo ${steps.a.outputs.stdout}"}},
        {"id": "c", "plugin": "report.html", "needs": ["b"]}],
        "triggers": [{"id": "t", "watch": "a"}]}, base="digest")


def ids(one):
    return [step["id"] for step in one.steps()]


def test_a_fresh_draft_has_nothing_unsaved_and_nothing_to_undo():
    one = draft()
    assert not one.dirty and not one.can_undo() and not one.can_redo()
    assert one.base == "digest"


def test_a_new_step_goes_after_the_one_it_waits_for_and_joins_its_phase():
    one = draft()
    made = one.add_step("command.shell", after="a")
    assert made == "shell" and ids(one) == ["a", "shell", "b", "c"]
    assert one.step("shell") == {"id": "shell", "plugin": "command.shell",
                                 "needs": ["a"], "group": "Prepare"}
    assert one.dirty


def test_a_new_step_gets_an_id_nothing_else_has():
    one = draft()
    assert one.add_step("command.shell") == "shell"
    assert one.add_step("command.shell") == "shell_2"
    assert one.unique_id("a b/c") == "a_b_c"


def test_undo_and_redo_walk_the_changes():
    one = draft()
    one.add_step("command.shell", after="c")
    one.remove_step("b")
    assert ids(one) == ["a", "c", "shell"]
    assert one.undo() and ids(one) == ["a", "b", "c", "shell"]
    assert one.undo() and ids(one) == ["a", "b", "c"] and not one.dirty
    assert one.redo() and ids(one) == ["a", "b", "c", "shell"]
    one.set_group("a", "")
    assert not one.can_redo(), "a new change is a new future"


def test_removing_a_step_takes_it_out_of_every_needs():
    one = draft()
    one.remove_step("b")
    assert "needs" not in one.step("c")
    assert one.references_to("a") == [], "b named it and b is gone"


def test_what_names_a_step_is_found_before_it_is_removed():
    one = draft()
    assert one.references_to("a") == ["b"]
    assert one.references_to("b") == ["c"]


def test_a_dependency_that_would_make_a_loop_is_refused():
    one = draft()
    assert not one.could_need("a", "c")
    assert not one.set_need("a", "c", True)
    assert not one.dirty
    assert one.could_need("c", "a") and one.set_need("c", "a", True)
    assert one.step("c")["needs"] == ["b", "a"]


def test_why_a_link_is_refused_is_said_in_words():
    """What the canvas shows next to a link dragged where it cannot land."""
    one = draft()
    assert one.why_not_need("c", "a") == ""
    assert one.why_not_need("b", "a") == "b already waits for a."
    assert "would be a loop" in one.why_not_need("a", "c")
    assert "itself" in one.why_not_need("a", "a")
    assert one.why_not_need("a", "ghost") == "No such step."


def test_what_a_step_waits_for_directly_is_a_list_whatever_the_file_wrote():
    one = draft()
    assert one.needs_of("b") == ["a"] and one.needs_of("a") == []
    assert one.needs_of("ghost") == []


def test_a_link_moved_to_another_step_is_one_change():
    one = draft()
    assert one.move_need("c", "b", "b", "a") is False, "b already waits for a"
    assert one.move_need("c", "b", "c", "a")
    assert one.step("c")["needs"] == ["a"], "in b's place"
    one.undo()
    assert one.step("c")["needs"] == ["b"]


def test_a_link_s_head_moved_takes_it_off_one_step_and_onto_another():
    one = draft()
    assert one.why_not_move("b", "a", "c", "a") == ""
    assert one.move_need("b", "a", "c", "a")
    assert "needs" not in one.step("b")
    assert one.step("c")["needs"] == ["b", "a"]


def test_a_moved_link_is_judged_without_the_one_it_replaces():
    """a -> b -> c: moving b's link to wait for c instead would be a loop
    only through the link being moved, so it is judged without it."""
    one = draft()
    assert "loop" in one.why_not_need("a", "c")
    assert "loop" in one.why_not_move("b", "a", "b", "c"), "c still waits for b"
    assert one.why_not_move("b", "a", "a", "b") == "", "the old one goes first"
    assert "does not wait" in one.why_not_move("a", "b", "a", "c")


def test_a_dependency_can_be_taken_away():
    one = draft()
    assert one.set_need("b", "a", False)
    assert "needs" not in one.step("b")


def test_a_renamed_step_is_followed_by_what_waits_for_it_and_its_trigger():
    one = draft()
    step = dict(one.step("a"), id="start")
    one.replace_step("a", step)
    assert one.step("b")["needs"] == ["start"]
    assert one.document["triggers"][0]["watch"] == "start"


def test_a_duplicate_sits_beside_the_original():
    one = draft()
    made = one.duplicate_step("b")
    assert made == "b_2" and ids(one) == ["a", "b", "b_2", "c"]
    assert one.step("b_2")["needs"] == ["a"]


def test_phases_are_listed_in_the_order_they_first_appear():
    one = draft()
    one.set_group("c", "Report")
    one.set_group("b", "Prepare")
    assert one.phases() == ["Prepare", "Report"]


def test_saving_makes_the_draft_clean_and_takes_the_new_digest():
    one = draft()
    one.add_step("command.shell")
    one.saved("new-digest")
    assert not one.dirty and one.base == "new-digest"
    assert one.can_undo(), "undo still walks back past a save"


def test_a_saved_step_brings_the_variables_this_cycle_lacks_and_no_others():
    one = Draft("demo", {"id": "demo", "variables": {"site": "mine"},
                         "steps": [{"id": "a", "plugin": "command.shell"}]})
    one.add_step("jira.issues", after="a",
                 step={"id": "jira_todo", "with": {"site": "${vars.site}"}},
                 variables={"site": "theirs", "token": {"secret": True}})

    assert one.document["variables"] == {"site": "mine",
                                         "token": {"secret": True}}
    assert one.step("jira_todo")["needs"] == ["a"]


def test_one_undo_takes_the_saved_step_and_its_variables_back_out():
    one = Draft("demo", {"id": "demo", "steps": []})
    one.add_step("jira.issues", step={"id": "jira_todo"},
                 variables={"token": {"secret": True}})
    one.undo()

    assert one.steps() == [] and "variables" not in one.document
