"""Reading the agent's event stream into stages somebody can follow.

The lines here are the shapes the Claude Code CLI actually emits, taken from a
recorded run. That matters: this module reads a format somebody else owns and
changes, so a test written from what the code does would only ever confirm the
code. The two rules worth defending are that the useful turns become rows and
that everything else - including a shape from a newer CLI - is dropped rather
than raised on.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cycle.plugins import agent_stream                            # noqa: E402


def _line(payload):
    return json.dumps(payload)


def _assistant(*blocks):
    return _line({"type": "assistant", "message": {"content": list(blocks)}})


def _only(line):
    found = agent_stream.stages(line)
    assert len(found) == 1, found
    return found[0]


# ----------------------------------------------------------------- the turns
def test_the_opening_line_says_which_model_is_working():
    stage = _only(_line({"type": "system", "subtype": "init",
                         "model": "claude-sonnet-4-6", "tools": ["Read"]}))
    assert stage["kind"] == "start"
    assert "claude-sonnet-4-6" in stage["title"]


def test_thinking_becomes_a_row_carrying_what_it_was_weighing():
    stage = _only(_assistant({"type": "thinking",
                              "thinking": "The file may not exist."}))
    assert stage["kind"] == "thinking"
    assert stage["title"] == "Thinking"
    assert stage["detail"] == "The file may not exist."


def test_a_tool_call_says_which_file_it_opened_not_just_that_it_read():
    """"Read" alone is the same row every time; "Read main.py" is the work."""
    stage = _only(_assistant({"type": "tool_use", "name": "Read",
                              "input": {"file_path": "/repo/src/main.py"}}))
    assert stage["kind"] == "tool"
    assert stage["title"] == "Read main.py"
    assert stage["status"] == "running"


def test_the_full_path_is_in_the_body_since_the_title_shows_the_name_alone():
    stage = _only(_assistant({"type": "tool_use", "name": "Read",
                              "input": {"file_path": "/repo/src/main.py"}}))
    assert stage["detail"] == "file_path: /repo/src/main.py"


def test_an_argument_the_title_already_carries_in_full_is_not_repeated():
    stage = _only(_assistant({"type": "tool_use", "name": "Grep",
                              "input": {"pattern": "def greet"}}))
    assert stage["title"] == "Grep def greet"
    assert stage["detail"] == ""


def test_the_other_arguments_are_lines_rather_than_json():
    """Three lines of punctuation around one fact is not a row to read."""
    stage = _only(_assistant({"type": "tool_use", "name": "Grep",
                              "input": {"pattern": "greet", "path": "/repo",
                                        "-n": True}}))
    assert "{" not in stage["detail"]
    assert "-n: true" in stage["detail"]


def test_a_tool_without_a_telling_argument_still_gets_a_row():
    stage = _only(_assistant({"type": "tool_use", "name": "TodoWrite",
                              "input": {"todos": []}}))
    assert stage["title"] == "TodoWrite"


def test_what_a_tool_returned_is_its_own_row():
    stage = _only(_line({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "1\thello"}]}}))
    assert stage["kind"] == "tool_result"
    assert stage["detail"] == "1\thello"
    assert stage["status"] == "done"


def test_a_tool_that_failed_says_so_in_its_status():
    stage = _only(_line({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "no such file", "is_error": True}]}}))
    assert stage["status"] == "failed"


def test_a_tool_result_sent_as_blocks_reads_the_same_as_one_sent_as_text():
    stage = _only(_line({"type": "user", "message": {"content": [
        {"type": "tool_result",
         "content": [{"type": "text", "text": "found it"}]}]}}))
    assert stage["detail"] == "found it"


def test_what_the_agent_concluded_is_a_row():
    stage = _only(_assistant({"type": "text", "text": "The code is fine."}))
    assert (stage["kind"], stage["detail"]) == ("text", "The code is fine.")


def test_one_turn_that_thinks_and_then_calls_a_tool_gives_both_rows():
    found = agent_stream.stages(_assistant(
        {"type": "thinking", "thinking": "Check the file."},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}))
    assert [one["kind"] for one in found] == ["thinking", "tool"]


def test_the_last_row_says_what_it_cost():
    stage = _only(_line({"type": "result", "subtype": "success",
                         "is_error": False, "num_turns": 2,
                         "total_cost_usd": 0.0428}))
    assert stage["kind"] == "result"
    assert stage["title"] == "Finished"
    assert "2 turns" in stage["detail"] and "$0.0428" in stage["detail"]


def test_a_run_that_ended_badly_says_so():
    stage = _only(_line({"type": "result", "subtype": "error_max_turns",
                         "is_error": True}))
    assert (stage["title"], stage["status"]) == ("Failed", "failed")


# ----------------------------------------------------------------- the review
REVIEW = {"summary": "Prints the line and exits.", "risk": "low",
          "issues": [{"severity": "low", "description": "Unused import",
                      "file": "main.py", "line": 1}],
          "recommendations": ["Drop the import"]}


def test_the_answer_a_review_step_asked_for_is_taken_apart_not_printed():
    """It arrives as JSON because the step asked for JSON. One long line of it
    is the least readable thing a panel could show."""
    stage = _only(_assistant({"type": "text", "text": json.dumps(REVIEW)}))
    assert stage["kind"] == "review"
    assert stage["body"]["issues"][0]["file"] == "main.py"


def test_the_review_s_title_carries_the_verdict():
    stage = _only(_assistant({"type": "text", "text": json.dumps(REVIEW)}))
    assert stage["title"] == "Review - low risk, 1 issue"


def test_many_issues_are_counted_in_the_plural():
    review = dict(REVIEW, issues=REVIEW["issues"] * 3)
    stage = _only(_assistant({"type": "text", "text": json.dumps(review)}))
    assert stage["title"] == "Review - low risk, 3 issues"


def test_the_summary_is_the_body_so_a_plain_reader_still_gets_the_sentence():
    stage = _only(_assistant({"type": "text", "text": json.dumps(REVIEW)}))
    assert stage["detail"] == REVIEW["summary"]


def test_a_review_inside_a_code_fence_is_still_a_review():
    """Asking for bare JSON mostly works; a fence is the ordinary failure."""
    fenced = "Here it is:\n```json\n%s\n```" % json.dumps(REVIEW)
    assert _only(_assistant({"type": "text", "text": fenced}))["kind"] == "review"


def test_ordinary_prose_is_not_dressed_up_as_a_review():
    stage = _only(_assistant({"type": "text", "text": "I had a look around."}))
    assert stage["kind"] == "text"
    assert "body" not in stage


def test_some_other_json_object_is_not_dressed_up_as_a_review():
    """Validated rather than merely parsed: showing any object under a review's
    headings would be a confident lie about what the agent said."""
    stage = _only(_assistant({"type": "text", "text": '{"a": 1}'}))
    assert stage["kind"] == "text"


def test_a_review_missing_a_required_field_is_left_as_text():
    broken = dict(REVIEW)
    del broken["risk"]
    assert _only(_assistant({"type": "text",
                             "text": json.dumps(broken)}))["kind"] == "text"


# --------------------------------------------------------------- what is dropped
def test_the_running_token_count_is_not_a_stage():
    """It arrives a dozen times a turn and says nothing about the work."""
    assert agent_stream.stages(
        _line({"type": "system", "subtype": "thinking_tokens", "tokens": 40})) == []


def test_a_rate_limit_notice_is_about_the_account_not_the_work():
    assert agent_stream.stages(_line({"type": "rate_limit_event"})) == []


def test_a_type_this_version_has_never_heard_of_is_dropped_not_raised_on():
    """The CLI ships on its own schedule; a new event is not a failed step."""
    assert agent_stream.stages(_line({"type": "something_new_entirely"})) == []


def test_a_line_that_is_not_json_is_dropped():
    for line in ("", "   ", "not json at all", '{"half": '):
        assert agent_stream.stages(line) == []


def test_an_empty_thinking_or_text_block_earns_no_row():
    assert agent_stream.stages(_assistant({"type": "thinking",
                                           "thinking": "  "})) == []
    assert agent_stream.stages(_assistant({"type": "text", "text": ""})) == []


def test_a_very_long_body_is_cut_rather_than_carried_whole():
    stage = _only(_assistant({"type": "thinking", "thinking": "x" * 10000}))
    assert len(stage["detail"]) <= agent_stream.DETAIL_CHARS + 3
    assert stage["detail"].endswith("...")


# -------------------------------------------------------------- the final answer
def test_the_answer_is_the_last_result_object_in_the_stream():
    text = "\n".join([
        _line({"type": "system", "subtype": "init"}),
        _assistant({"type": "text", "text": "working"}),
        _line({"type": "result", "subtype": "success", "result": "the review"}),
    ])
    assert agent_stream.last_result(text)["result"] == "the review"


def test_a_stream_cut_off_at_the_front_still_yields_its_answer():
    """The reader takes the tail of a file that can run to megabytes."""
    text = ('ment": "half a line that was cut"}\n'
            + _line({"type": "result", "subtype": "success", "result": "ok"}))
    assert agent_stream.last_result(text)["result"] == "ok"


def test_a_stream_that_never_finished_has_no_answer():
    text = _assistant({"type": "text", "text": "still going"})
    assert agent_stream.last_result(text) is None
    assert agent_stream.last_result("") is None
