"""Teams as an integration of its own (QCR ADR-015), the core's side.

* A connection is one channel; its webhook is a secret - checked as an address,
  never quoted back, never in the history. Jira's connections are as they were.
* A post goes through the provider: the card with the connection's footer and
  its mention aliases; written down as accepted, failed or unknown, with who
  sent it and who approved it.
* At most one post a minute through a connection, whoever sends it; a failed
  post does not count, and a refusal is not a post.
* The page's operations: reads, a post, Send again (a new post), a test post.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from integrations import cli, reader as reader_mod, store
from integrations.contracts import WriteError
from integrations.teams import capabilities, history

SECRET_PATH = "/workflows/abc/triggers/manual/paths/invoke?sig=SECRET-SIGNATURE"


class Webhook:
    """A Workflows webhook on this machine: answers ``status``, keeps what it got."""

    def __init__(self, status=202):
        self.status, self.got = status, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(outer.status)
                self.end_headers()

            def log_message(self, *args):
                pass
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d%s" % (self.server.server_port, SECRET_PATH)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def world(tmp_path):
    hook = Webhook()
    paths = (str(tmp_path / "integrations.json"), str(tmp_path / "secrets.json"), str(tmp_path / "env.json"))
    entry, problems = store.upsert({"provider": "teams", "name": "QA channel", "settings": {
        "footer": "via QAVector", "aliases": {"Dana": "dana.k@team.example"}}},
        paths[0], paths[1], new_token=hook.url)
    assert not problems
    hook.entry, hook.paths = entry, paths
    hook.connection = lambda: store.credentials(entry["id"], paths[0], paths[1], paths[2])
    yield hook
    hook.close()


def everything_written(paths):
    found = ""
    for path in (paths[0], history.path_for(paths[0])):
        try:
            found += open(path, encoding="utf-8").read()
        except OSError:
            pass
    return found


def test_a_channel_s_webhook_is_its_secret_and_jira_is_as_it_was(world, tmp_path):
    entry = world.entry
    assert entry["provider"] == "teams" and entry["settings"]["access"] == {"agents": "approve", "cycles": "post"}
    assert "token" not in entry and "SECRET" not in json.dumps(store.load(world.paths[0]))
    assert store.credentials(entry["id"], *world.paths)["webhook"] == world.url
    assert [one["token_set"] for one in store.describe(*world.paths[:2])] == [True]
    bad, problems = store.upsert({"provider": "teams", "name": "Elsewhere"}, *world.paths[:2],
                                 new_token="http://teams.example.invalid/hook?sig=SECRET")
    assert problems and "https://" in problems[0] and "SECRET" not in " ".join(problems)
    jira, problems = store.upsert({"name": "Product", "site": "https://jira.example.invalid", "email": "a@b.c"},
                                  *world.paths[:2], new_token="tok")
    assert not problems and jira["provider"] == "jira" and jira["flavour"] == "cloud"
    assert store.problems(store.normalize({"provider": "nope", "name": "X"})) == ["Unknown provider 'nope'."]


def test_a_post_has_the_footer_and_aliases_and_is_written_down_without_the_url(world):
    connection = world.connection()
    made = capabilities.post(connection, {"title": "Release", "text": "- shipped", "mentions": ["Dana"]},
                             {"label": "This page"})
    assert made["post"]["outcome"] == "accepted" and made["post"]["status"] == 202
    card = world.got[0]["attachments"][0]["content"]
    texts = [block["text"] for block in card["body"]]
    assert texts[0] == "Release" and "via QAVector" in texts and "<at>Dana</at>" in texts[-1]
    assert card["msteams"]["entities"][0]["mentioned"]["id"] == "dana.k@team.example"
    assert made["post"]["mentions"] == ["Dana <dana.k@team.example>"]
    assert "SECRET" not in everything_written(world.paths) and "SECRET" not in json.dumps(made)


def test_one_post_a_minute_whoever_sends_it_and_a_failed_one_does_not_count(world, monkeypatch):
    connection = world.connection()
    world.status = 404
    with pytest.raises(Exception):
        capabilities.post(connection, {"text": "first"}, {"label": "This page"})
    assert history.read(world.paths[0])[0]["outcome"] == "failed"
    world.status = 202
    capabilities.post(connection, {"text": "second"}, {"label": "This page"})       # the failed one did not count
    with pytest.raises(WriteError) as refused:
        capabilities.post(connection, {"text": "third"}, {"label": "Agent Claude"})
    assert refused.value.code == "refused" and "One post a minute" in str(refused.value)
    assert 0 < refused.value.details["retry_after"] <= 61
    assert [one["text"].splitlines()[0] for one in history.read(world.paths[0])] == ["second", "first"]
    assert len(world.got) == 2                                                       # the third was never sent
    monkeypatch.setattr(capabilities, "INTERVAL", 0)
    capabilities.post(connection, {"text": "later"}, {"label": "This page"})
    assert len(world.got) == 3


def test_an_approved_post_says_who_asked_and_who_approved(world):
    reader = reader_mod.ProjectReader(*world.paths)
    prepared = reader.prepare(world.entry["id"], "teams", "post", {"text": "Ready for review", "title": "QA-142"})
    assert prepared["card"]["title"] == "Post to QA channel" and "cannot be taken back" in prepared["card"]["notes"][0]
    assert set(prepared["editable"]) == {"title", "text"}
    result = reader.write(world.entry["id"], "teams", "post", {"text": "Ready for review", "title": "QA-142"},
                          {"label": "Agent Claude", "detail": "e-1", "actor": "agent:claude/s1",
                           "approved_by": "you"})
    post = history.get(world.paths[0], result["post"]["id"])
    assert post["approved_by"] == "you" and post["source"]["actor"] == "agent:claude/s1"


def test_the_page_reads_posts_sends_again_and_tests(world, capsys, tmp_path):
    def call(operation, document=None):
        source = tmp_path / "doc.json"
        source.write_text(json.dumps(document or {}))
        cli.main(["--integrations-file=" + world.paths[0], "--cycle-secrets-file=" + world.paths[1],
                  "call", world.entry["id"], operation, "--from=%s" % source])
        return json.loads(capsys.readouterr().out)
    posted = call("post", {"text": "From the page"})
    assert posted["ok"] and posted["result"]["post"]["source"] == {"label": "This page"}
    listed = call("history")
    assert [one["text"].splitlines()[0] for one in listed["result"]["items"]] == ["From the page"]
    again = call("again", {"id": posted["result"]["post"]["id"]})
    assert again["ok"] is False and again["kind"] == "refused" and again["retry_after"] > 0
    capabilities.INTERVAL, saved = 0, capabilities.INTERVAL
    try:
        again = call("again", {"id": posted["result"]["post"]["id"]})
        test = call("test")
    finally:
        capabilities.INTERVAL = saved
    assert again["ok"] and again["result"]["post"]["again_of"] == posted["result"]["post"]["id"]
    assert test["ok"] and test["result"]["post"]["title"] == "QAVector test"
    assert "SECRET" not in json.dumps([posted, listed, again, test])
    # Every post the page made is a line in Activity, newest first, without its words;
    # the refused Send again is not a post, so it is not there.
    from integrations import activity
    lines = activity.read(world.paths[0])
    assert [one["op"] for one in lines] == ["post"] * 3
    assert {one["source"]["label"] for one in lines} == {"This page", "Test from this page"}
    assert "From the page" not in json.dumps(lines) and "SECRET" not in json.dumps(lines)
