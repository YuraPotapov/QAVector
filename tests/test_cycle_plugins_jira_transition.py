"""Moving one Jira issue along its workflow.

The fake here keeps state: a transition really changes the status it serves
afterwards. That matters for the test that carries the plugin - a run killed
between the transition and the record re-runs, finds the issue already where it
wanted it, and must report that rather than failing. A fake that answered the
same thing every time could not tell the two apart.

The other property worth defending is the refusal. A Jira workflow is the
project's own; the transition out of To Do has a different name in every
instance. So a name that is not on offer has to come back with the names that
were, or somebody is left reading a 400.
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cycle import registry                                        # noqa: E402
from cycle.context import CancelToken, RunContext                 # noqa: E402
from cycle.workspace import create                                # noqa: E402
from domain.cycle import CycleRun, CycleStep                      # noqa: E402

#: A small workflow: To Do -> In Progress -> Done, each move named the way a
#: real one is - after the action, not after the status it lands in.
def _move(id_, name, to):
    """One transition, in the shape Jira actually sends it: `to` is a status
    object, not the name."""
    return {"id": id_, "name": name, "to": {"name": to, "id": "s" + id_}}


WORKFLOW = {
    "To Do": [_move("11", "Start Progress", "In Progress")],
    "In Progress": [_move("21", "Done", "Done"),
                    _move("22", "Stop Progress", "To Do")],
    "Done": [_move("31", "Reopen", "To Do")],
}


class _Jira(object):
    """A loopback Jira with one issue whose status actually changes."""

    def __init__(self, status="To Do", workflow=None, refuse=0):
        self.status = status
        self.workflow = workflow if workflow is not None else WORKFLOW
        self.seen = []                  # (method, path, body)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _send(self, status, payload):
                # A real 204 carries nothing at all, which is how Jira answers
                # a transition - so the fake answers that way too.
                raw = (b"" if status == 204 or payload is None
                       else json.dumps(payload).encode("utf-8"))
                self.send_response(status)
                if raw:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                if raw:
                    self.wfile.write(raw)

            def do_GET(self):           # noqa: N802 - the base class's spelling
                outer.seen.append(("GET", self.path, None))
                if refuse:
                    # A bare refusal, with nothing in it - which is when the
                    # plugin's own wording about which credential is wanted.
                    return self._send(refuse, None)
                if "/transitions" in self.path:
                    return self._send(200, {"transitions":
                                            outer.workflow.get(outer.status, [])})
                self._send(200, {"key": "QA-7",
                                 "fields": {"status": {"name": outer.status}}})

            def do_POST(self):          # noqa: N802
                body = json.loads(self.rfile.read(
                    int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.seen.append(("POST", self.path, body))
                if refuse:
                    return self._send(refuse, None)
                wanted = str((body.get("transition") or {}).get("id"))
                for one in outer.workflow.get(outer.status, []):
                    if one["id"] == wanted:
                        outer.status = one["to"]["name"]
                        return self._send(204, None)
                self._send(400, {"errorMessages": ["transition not available"]})

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.site = "http://127.0.0.1:%d" % self._server.server_port
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()

    def posts(self):
        return [one for one in self.seen if one[0] == "POST"]


@pytest.fixture
def jira():
    made = []

    def serve(**kwargs):
        one = _Jira(**kwargs)
        made.append(one)
        return one

    yield serve
    for one in made:
        one.close()


@pytest.fixture
def move(tmp_path):
    def go(site, **settings):
        workspace = create("20260919-000000-t", str(tmp_path / "runs"))
        context = RunContext(CycleRun(id="r", cycle_id="c",
                                      workspace=workspace),
                             None, workspace, cancel=CancelToken())
        step = CycleStep(id="t", plugin="jira.transition", settings=dict({
            "site": site, "email": "me@example.invalid", "token": "sekret",
            "issue": "QA-7", "to": "Start Progress"}, **settings))
        return registry.get("jira.transition").execute(context, step)
    return go


# -------------------------------------------------------------- moving it
def test_it_moves_the_issue_and_says_where_from_and_to(jira, move):
    server = jira()
    result = move(server.site)

    assert result.ok, result.message
    assert result.outputs["changed"] is True
    assert result.outputs["from_status"] == "To Do"
    assert result.outputs["status"] == "In Progress"
    assert result.outputs["transition"] == "Start Progress"
    assert server.status == "In Progress", "the issue really moved"


def test_the_status_is_read_back_rather_than_assumed(jira, move):
    """A post-function can land the issue somewhere other than where the
    transition's name suggested. Reporting the name would report an intention."""
    workflow = dict(WORKFLOW,
                    **{"To Do": [_move("11", "Start Progress",
                                       "Somewhere Else")]})
    server = jira(workflow=workflow)
    result = move(server.site)

    assert result.outputs["status"] == "Somewhere Else"


def test_a_transition_can_be_named_by_its_id(jira, move):
    server = jira()
    assert move(server.site, to="11").ok
    assert server.status == "In Progress"


def test_the_name_is_matched_without_regard_to_case(jira, move):
    server = jira()
    assert move(server.site, to="start progress").ok


def test_what_it_sends_is_the_transition_and_nothing_else(jira, move):
    server = jira()
    move(server.site)

    method, path, body = server.posts()[0]
    assert path.endswith("/issue/QA-7/transitions")
    assert body == {"transition": {"id": "11"}}


# --------------------------------------------------------------- refusals
def test_a_transition_that_is_not_on_offer_names_the_ones_that_are(jira, move):
    """The difference between a usable error and a 400."""
    server = jira()
    result = move(server.site, to="Close As Wont Fix")

    assert not result.ok
    assert "Close As Wont Fix" in result.message
    assert "Start Progress" in result.message, "it has to say what was offered"
    assert server.posts() == [], "and must not have tried"


def test_it_refuses_when_the_issue_is_not_where_the_run_thought(jira, move):
    """The same idea as git.commit's verified tree: do not act on something
    that changed under you."""
    server = jira(status="In Progress")
    result = move(server.site, to="Done", expect_status="To Do")

    assert not result.ok
    assert "In Progress" in result.message and "To Do" in result.message
    assert server.posts() == []
    assert server.status == "In Progress"


def test_it_proceeds_when_the_issue_is_where_the_run_thought(jira, move):
    server = jira()
    assert move(server.site, expect_status="To Do").ok


def test_refused_credentials_say_which_credentials(jira, move):
    server = jira(refuse=401)
    result = move(server.site)

    assert not result.ok
    assert "401" in result.message and "API token" in result.message


def test_a_site_that_does_not_answer_fails_the_step(move):
    result = move("http://127.0.0.1:1")
    assert not result.ok
    assert "Cannot reach" in result.message


# ------------------------------------------------------- doing it twice
def test_a_rerun_that_finds_it_already_there_reports_so_rather_than_failing(
        jira, move):
    """A run killed between the transition and the record. Jira stops offering
    a move already made, so asking again would be a 400."""
    server = jira()
    first = move(server.site, lands_in="In Progress")
    assert first.outputs["changed"] is True

    again = move(server.site, lands_in="In Progress")

    assert again.ok
    assert again.outputs["changed"] is False
    assert again.outputs["status"] == "In Progress"
    assert "already in" in again.message
    assert len(server.posts()) == 1, "it must not transition a second time"


def test_being_asked_for_the_status_it_is_already_in_is_not_a_failure(jira,
                                                                      move):
    server = jira(status="In Progress")
    result = move(server.site, to="In Progress")

    assert result.ok
    assert result.outputs["changed"] is False
    assert server.posts() == []


def test_landing_somewhere_else_fails_rather_than_reporting_success(jira, move):
    """A post-function moved it on. The transition was made, and saying it went
    where the step asked would be claiming something nobody checked."""
    workflow = dict(WORKFLOW,
                    **{"To Do": [_move("11", "Start Progress", "Escalated")]})
    server = jira(workflow=workflow)
    result = move(server.site, lands_in="In Progress")

    assert not result.ok
    assert "Escalated" in result.message and "In Progress" in result.message
    assert result.outputs["changed"] is True, "it did happen; say so"
    assert result.outputs["status"] == "Escalated"


def test_landing_where_the_step_said_passes(jira, move):
    server = jira()
    assert move(server.site, lands_in="In Progress").ok


def test_without_lands_in_a_transition_named_after_its_status_still_reconciles(
        jira, move):
    """The common case: people name the move after where it goes."""
    server = jira(status="In Progress")
    first = move(server.site, to="Done")
    assert first.outputs["changed"] is True

    again = move(server.site, to="Done")
    assert again.ok and again.outputs["changed"] is False


def test_a_genuinely_wrong_name_is_still_refused_when_it_is_not_where_it_goes(
        jira, move):
    """"Already there" must not become a way for a typo to pass."""
    server = jira(status="In Progress")
    result = move(server.site, to="Nonsense")

    assert not result.ok
    assert "Nonsense" in result.message


# --------------------------------------------------------- what it carries
def test_a_comment_goes_with_the_transition_as_one_action(jira, move):
    server = jira()
    move(server.site, comment="Picked up by the nightly run.")

    body = server.posts()[0][2]
    added = body["update"]["comment"][0]["add"]["body"]
    assert added["type"] == "doc", "Cloud takes a document, not a string"
    assert added["content"][0]["content"][0]["text"] == (
        "Picked up by the nightly run.")


def test_a_server_comment_is_plain_text_rather_than_a_document(jira, move):
    server = jira()
    move(server.site, flavour="server", auth="bearer", email="",
         comment="Picked up.")

    assert server.posts()[0][2]["update"]["comment"][0]["add"]["body"] == (
        "Picked up.")


def test_server_talks_to_its_own_api_version(jira, move):
    server = jira()
    move(server.site, flavour="server", auth="bearer", email="")
    assert all("/rest/api/2/" in path for _m, path, _b in server.seen)


def test_fields_the_screen_asks_for_are_sent(jira, move):
    server = jira(status="In Progress")
    move(server.site, to="Done", fields={"resolution": "Fixed"})

    assert server.posts()[0][2]["fields"] == {"resolution": "Fixed"}


def test_what_the_workflow_offered_is_an_output(jira, move):
    server = jira()
    result = move(server.site)
    assert result.outputs["available"] == ["Start Progress"]


# ------------------------------------------------------------- the shape
def test_it_is_its_own_plugin_rather_than_a_mode_of_reading():
    """So the cycle file says plainly which of the two a step is."""
    assert registry.get("jira.transition") is not registry.get("jira.issues")
    assert "transition" not in [one.key for one
                                in registry.get("jira.issues").metadata.inputs]


def test_the_reading_plugin_still_cannot_write():
    reading = registry.get("jira.issues")
    assert reading.metadata.permissions == ("network",)
    assert "transition" not in reading.metadata.summary.lower()


def test_a_reference_is_left_for_the_run_to_resolve():
    assert registry.get("jira.transition").problems(
        {"site": "https://x.invalid", "email": "e", "token": "${vars.t}",
         "issue": "${steps.todo.outputs.keys}", "to": "${vars.move}"}) == []


def test_its_metadata_survives_the_wire():
    json.dumps(registry.get("jira.transition").metadata.to_dict())
