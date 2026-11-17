"""A Jira issue as the context of a conversation (QCR ADR-014), the core's side.

* A conversation is linked to an issue when its agent reads the issue's
  details (issue, comments, context) - never by a search. A link is kept
  beside the record, so a turn that rewrites the record cannot lose it; it
  can be undone, and what a conversation was started for stays.
* The issue's conversations and their usage count the linked ones too.
* ``integrations.jira.<connection>.context`` gives, in one read, the issue as
  text and its newest comments, and what QAVector knows: the conversations
  about it, their tasks, what they cost, and the private notes.
"""

import json
import os
import sys

import pytest

from integrations import notes, store
from integrations.agents import sessions, usage
from integrations.agents.store import ACCESS_GRANTS
from qcr.host import Host

sys.path.insert(0, os.path.dirname(__file__))
from fake_jira import FakeJira  # noqa: E402
from test_qcr_integration_writes import EMAIL, TOKEN, _answers  # noqa: E402

OWNER = {"id": "claude", "name": "Claude", "kind": "claude"}


def turn(**more):
    return dict({"n": 1, "started": 1.0, "ended": 2.0, "state": "completed", "model": "sonnet",
                 "tokens": {"input": 10, "output": 5}, "cost_usd": 0.5, "cost_kind": "estimate"}, **more)


def test_a_link_is_kept_beside_the_record_counted_and_can_be_undone(tmp_path):
    root = str(tmp_path / "sessions")
    started = sessions.create(root, OWNER, str(tmp_path), "read", source={"label": "Jira", "ref": "jira:c:QA-1"})
    chat = sessions.create(root, OWNER, str(tmp_path), "read")
    for one in (started, chat):
        sessions.append_turn(root, one["id"], turn(), [{"type": "user", "text": "hi"}])
    assert sessions.link(root, chat["id"], "jira:c:QA-1") is True
    assert sessions.link(root, chat["id"], "jira:c:QA-1") is False                 # once
    assert sessions.link(root, started["id"], "jira:c:QA-1") is False               # started for it
    loaded = sessions.load(root, chat["id"])
    assert sessions.refs_of(loaded) == ["jira:c:QA-1"]
    sessions.save(root, loaded)                                                     # a turn saving its copy
    with open(os.path.join(sessions.directory(root, chat["id"]), "session.json")) as handle:
        assert "links" not in json.load(handle)["source"]
    assert sessions.refs_of(sessions.load(root, chat["id"])) == ["jira:c:QA-1"]     # not lost
    assert sorted(one["id"] for one in sessions.listing(root, ref="jira:c:QA-1")["sessions"]) == sorted(
        [started["id"], chat["id"]])
    assert usage.for_ref(root, "jira:c:QA-1")["own"]["conversations"] == 2
    # A turn's copy, read before the link, still shows it when described (as the GUI is told).
    stale = dict(loaded, source={"label": "Chat", "ref": ""})
    assert sessions.described(root, stale)["source"]["links"] == ["jira:c:QA-1"]
    assert sessions.unlink(root, chat["id"], "jira:c:QA-1") is True
    assert [one["id"] for one in sessions.listing(root, ref="jira:c:QA-1")["sessions"]] == [started["id"]]
    assert sessions.unlink(root, started["id"], "jira:c:QA-1") is False             # what it began with stays


@pytest.fixture
def host(tmp_path):
    server = FakeJira(_answers(**{
        "GET /rest/api/3/issue/QA-142/comment": {"total": 7, "comments": [
            {"id": str(n), "author": {"displayName": "Dana"}, "created": "2026-11-0%dT10:00:00.000+0000" % n,
             "body": {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [
                 {"type": "text", "text": "Comment %d " % n + "x" * 3000}]}]}} for n in range(1, 6)]},
        }))
    paths = (str(tmp_path / "integrations.json"), str(tmp_path / "secrets.json"), str(tmp_path / "env.json"))
    entry, problems = store.upsert({"name": "Product team", "site": server.site, "email": EMAIL, "settings": {}},
                                   paths[0], paths[1], new_token=TOKEN)
    assert not problems
    sent = []
    made = Host("p", str(tmp_path / "journal.sqlite"), lambda line: sent.append(json.loads(line)))
    made.open_integrations(*paths)
    made._on_hello({"protocol": 1, "actors": {}})
    root = str(tmp_path / "agent-sessions")
    made._on_agent_connections({"sessions_root": root, "agents_path": str(tmp_path / "agents.json")})
    chat = sessions.create(root, OWNER, str(tmp_path), "read")
    actor = "agent:claude/%s" % chat["id"]
    made._on_actor({"actor": actor, "grants": [{"permission": p} for p in ACCESS_GRANTS["jira"]]})
    made.sent, made.root, made.actor, made.chat, made.cid = sent, root, actor, chat["id"], entry["id"]
    made.integrations_path = paths[0]

    def read(op, **request):
        answer = made.handle_agent(actor, {"type": "read", "capability": "integrations.jira.%s.%s" % (
            entry["id"], op), "input": request})
        assert answer["ok"], answer
        return answer["result"]
    made.read = read
    yield made
    made.close()
    server.close()


def linked(host):
    return [one for one in host.sent if one.get("type") == "conversation_linked"]


def test_reading_an_issue_s_details_links_the_conversation_and_a_search_does_not(host):
    host._linked(host.actor, {"capability": "integrations.jira.%s.search" % host.cid,
                              "input": {"jql": "key = QA-142", "key": "QA-142"}})
    assert linked(host) == [] and sessions.links_of(host.root, host.chat) == []
    host.read("issue", key="qa-142")
    ref = "jira:%s:QA-142" % host.cid
    assert sessions.links_of(host.root, host.chat) == [ref]
    assert linked(host) == [{"type": "conversation_linked", "session": host.chat, "ref": ref}]
    host.read("comments", key="QA-142")
    assert len(linked(host)) == 1                                                   # told once


def test_the_context_is_the_issue_as_text_and_what_qavector_knows(host):
    ref = "jira:%s:QA-142" % host.cid
    other = sessions.create(host.root, OWNER, "/tmp", "read", source={"label": "Jira", "ref": ref})
    sessions.append_turn(host.root, other["id"], turn(), [{"type": "user", "text": "look"}])
    task = sessions.create(host.root, OWNER, "/tmp", "read",
                           source={"label": "Delegated", "parent": other["id"], "by": "Claude", "record": True})
    notes.save(host.integrations_path, host.cid, "QA-142", "Ask Dana about the IBAN mask first.")
    host.read("context", key="QA-142")          # read once: this conversation is linked from now on
    found = host.read("context", key="QA-142")
    issue = found["issue"]
    assert issue["key"] == "QA-142" and "description_html" not in issue and "description_doc" not in issue
    comments = found["comments"]
    assert len(comments["newest"]) == 5 and comments["total"] == 7 and comments["more"] == 2
    assert all(len(one["text"]) <= 1500 and one.get("cut") for one in comments["newest"])
    said = found["qavector"]
    hows = {one["id"]: one["how"] for one in said["conversations"]}
    assert hows[other["id"]] == "started for it" and hows[host.chat] == "linked"
    assert [one["id"] for one in said["tasks"]] == [task["id"]]
    assert said["usage"]["own"]["conversations"] == 1 and said["usage"]["own"]["estimate_usd"] == 0.5
    assert said["notes"][0]["text"].startswith("Ask Dana")
