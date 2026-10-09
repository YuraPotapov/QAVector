"""Posting to Teams through a workflow's webhook - and never repeating its URL."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from cycle import registry
from cycle.context import RunContext
from cycle.plugins.teams import card, people
from domain.cycle import CycleRun, CycleStep

NOTE = """Release 09.10.2026

- QA-12 Show how many confirmed orders a customer has
- QA-13 Mark large orders

**Remarks**
- QA-13: update the module"""


class Workflow(object):
    """A loopback stand-in for a Teams workflow: keeps what it was sent."""

    def __init__(self, status=202):
        self.status = status
        self.received = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):                                  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.received.append((self.path, self.headers["Content-Type"],
                                       json.loads(body)))
                self.send_response(outer.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = ("http://127.0.0.1:%d/workflows/abc/triggers/manual/run?sig=SECRET"
                    % self.server.server_port)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def workflow():
    made = Workflow()
    yield made
    made.close()


def send(tmp_path, **settings):
    context = RunContext(CycleRun(id="r", cycle_id="c"), None, str(tmp_path))
    step = CycleStep(id="tell", plugin="teams.message", settings=settings)
    return registry.get("teams.message").execute(context, step)


def texts(payload):
    return [block["text"] for block in
            payload["attachments"][0]["content"]["body"]]


def test_the_note_is_posted_as_an_adaptive_card(workflow, tmp_path):
    result = send(tmp_path, webhook=workflow.url, text=NOTE)

    assert result.ok, result.message
    assert result.outputs == {"sent": True, "status": 202, "mentioned": 0}
    [(path, kind, payload)] = workflow.received
    assert path.startswith("/workflows/abc/") and kind.startswith("application/json")
    assert payload["type"] == "message"
    attachment = payload["attachments"][0]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert attachment["content"]["type"] == "AdaptiveCard"
    assert texts(payload) == [
        "Release 09.10.2026",
        "• QA-12 Show how many confirmed orders a customer has",
        "• QA-13 Mark large orders",
        "**Remarks**",
        "• QA-13: update the module"]


def test_a_title_goes_above_the_message(workflow, tmp_path):
    send(tmp_path, webhook=workflow.url, text="Hello", title="Release")
    body = workflow.received[0][2]["attachments"][0]["content"]["body"]
    assert body[0] == {"type": "TextBlock", "text": "Release", "weight": "Bolder",
                       "size": "Medium", "wrap": True}


def test_a_blank_line_becomes_a_gap_not_a_block():
    body = card("one\n\ntwo")["attachments"][0]["content"]["body"]
    assert [one["text"] for one in body] == ["one", "two"]
    assert body[1]["spacing"] == "Medium"


def test_a_refusal_fails_the_step_without_the_url(tmp_path):
    refusing = Workflow(status=404)
    try:
        result = send(tmp_path, webhook=refusing.url, text="Hello")
    finally:
        refusing.close()
    assert not result.ok
    assert "HTTP 404" in result.message and "127.0.0.1" in result.message
    assert "SECRET" not in result.message and "workflows" not in result.message
    assert result.outputs == {"sent": False, "status": 404, "mentioned": 0}


def test_an_unreachable_workflow_fails_without_the_url(tmp_path):
    result = send(tmp_path, webhook="http://127.0.0.1:9/hook?sig=SECRET", text="Hi")
    assert not result.ok and "Could not reach 127.0.0.1" in result.message
    assert "SECRET" not in result.message


@pytest.mark.parametrize("webhook", ["http://hooks.example.invalid/x?sig=SECRET",
                                     "ftp://example.invalid/x", "not a url"])
def test_only_https_or_a_stand_in_on_this_machine(tmp_path, webhook):
    result = send(tmp_path, webhook=webhook, text="Hi")
    assert not result.ok and "https://" in result.message
    assert "SECRET" not in result.message


def test_an_empty_message_is_not_sent(workflow, tmp_path):
    result = send(tmp_path, webhook=workflow.url, text="   ")
    assert not result.ok and "text_file" in result.message
    assert workflow.received == []


def test_its_metadata_survives_the_wire():
    json.dumps(registry.get("teams.message").metadata.to_dict())


def test_the_message_can_come_from_a_file_in_the_workspace(workflow, tmp_path):
    """For a message longer than the forty lines a step keeps of its output."""
    many = "\n".join("- TestX.test_%d" % one for one in range(60))
    (tmp_path / "failed.md").write_text("Tests failed\n" + many, encoding="utf-8")
    result = send(tmp_path, webhook=workflow.url, text_file="failed.md")
    assert result.ok, result.message
    lines = texts(workflow.received[0][2])
    assert lines[0] == "Tests failed" and len(lines) == 61
    assert lines[-1] == "\u2022 TestX.test_59"


def test_text_wins_over_the_file(workflow, tmp_path):
    (tmp_path / "other.md").write_text("from the file", encoding="utf-8")
    send(tmp_path, webhook=workflow.url, text="from text", text_file="other.md")
    assert texts(workflow.received[0][2]) == ["from text"]


def test_a_missing_file_fails_and_sends_nothing(workflow, tmp_path):
    result = send(tmp_path, webhook=workflow.url, text_file="nowhere.md")
    assert not result.ok and "Cannot read the message from nowhere.md" in result.message
    assert workflow.received == []


def test_a_very_long_file_is_cut_and_says_so(workflow, tmp_path):
    from cycle.plugins.teams import MAX_TEXT
    (tmp_path / "long.md").write_text("x" * (MAX_TEXT + 500), encoding="utf-8")
    send(tmp_path, webhook=workflow.url, text_file="long.md")
    assert texts(workflow.received[0][2])[-1] == "(cut at %d characters)" % MAX_TEXT


def entities(payload):
    return payload["attachments"][0]["content"].get("msteams", {}).get("entities", [])


def test_a_person_the_text_tags_is_mentioned_there(workflow, tmp_path):
    """Beside the test their change broke - not again at the end."""
    text = "Tests failed\n- TestA.test_one - <at>Olena Koval</at>"
    result = send(tmp_path, webhook=workflow.url, text=text,
                  mentions="Olena Koval <olena@example.invalid>")
    assert result.ok, result.message
    assert result.outputs["mentioned"] == 1 and "mentioning 1" in result.message
    payload = workflow.received[0][2]
    assert texts(payload) == ["Tests failed", "\u2022 TestA.test_one - <at>Olena Koval</at>"]
    assert entities(payload) == [{
        "type": "mention", "text": "<at>Olena Koval</at>",
        "mentioned": {"id": "olena@example.invalid", "name": "Olena Koval"}}]


def test_people_the_text_does_not_tag_are_tagged_at_the_end(workflow, tmp_path):
    send(tmp_path, webhook=workflow.url, text="Tests failed",
         mentions=["Olena Koval <olena@example.invalid>", "taras@example.invalid"])
    payload = workflow.received[0][2]
    assert texts(payload)[-1] == ("<at>Olena Koval</at>, "
                                  "<at>taras@example.invalid</at>")
    assert [one["mentioned"]["id"] for one in entities(payload)] == [
        "olena@example.invalid", "taras@example.invalid"]


def test_nobody_to_mention_leaves_the_card_as_it_was(workflow, tmp_path):
    result = send(tmp_path, webhook=workflow.url, text="Hello", mentions="")
    assert result.outputs["mentioned"] == 0
    assert "msteams" not in workflow.received[0][2]["attachments"][0]["content"]


def test_a_list_from_a_step_reads_lines_and_commas_once_each():
    found, problems = people("Olena Koval <olena@example.invalid>, taras@example.invalid\n"
                             "O. Koval <OLENA@example.invalid>\n\n")
    assert found == [("Olena Koval", "olena@example.invalid"),
                     ("taras@example.invalid", "taras@example.invalid")]
    assert problems == []


def test_a_mention_written_in_the_cycle_must_be_an_address():
    problems = registry.get("teams.message").problems(
        {"webhook": "https://example.invalid/x", "text": "Hi", "mentions": ["Olena Koval"]})
    assert problems == ["mentions: 'Olena Koval' is not an email or \"name <email>\"."]


def test_a_bad_address_from_a_step_costs_only_its_own_mention(workflow, tmp_path):
    """One address another step got wrong must not cost everybody the message."""
    result = send(tmp_path, webhook=workflow.url, text="Tests failed",
                  mentions="Olena Koval <olena@example.invalid>\nnot an address")
    assert result.ok, result.message
    assert result.outputs["mentioned"] == 1 and "not an address" in result.message
    assert len(entities(workflow.received[0][2])) == 1
