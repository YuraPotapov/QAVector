"""Messages between agents (ADR-006 D4, Phase 5.3): the durable store, then the
whole way through the real host with stand-in CLIs (no model is asked anything)."""

import os

import pytest

from integrations.agents import sessions, store
from qcr import QCRError
from qcr.adapters import delegation
from qcr.messages import MessageStore
from test_qcr_delegation import ACTOR, World, _codex_turns


# -- the store ----------------------------------------------------------------------
@pytest.fixture
def box(tmp_path):
    now = [1000.0]
    one = MessageStore(str(tmp_path / "m" / "messages.sqlite"), clock=lambda: now[0])
    yield one, now
    one.close()


def _send(box, message_id="m1", **fields):
    values = dict(kind="message", sender_actor="a", sender_session="s1", sender_connection="claude",
                  recipient_connection="codex", body="hello")
    values.update(fields)
    return box.create(message_id, "p", **values)


def test_a_message_is_recorded_once_per_id(box):
    store_, _now = box
    first, created = _send(store_)
    assert created and first["state"] == "queued" and first["conversation_id"] == "m1"
    again, created = _send(store_)
    assert not created and again == first
    with pytest.raises(QCRError) as caught:
        _send(store_, body="something else")
    assert caught.value.code == "conflict"


def test_states_move_forward_only_and_end_once(box):
    store_, _now = box
    _send(store_)
    assert store_.transition("m1", "delivered")["state"] == "delivered"
    with pytest.raises(QCRError):
        store_.transition("m1", "awaiting_approval")
    assert store_.transition("m1", "acknowledged")["state"] == "acknowledged"
    assert store_.transition("m1", "acknowledged")["state"] == "acknowledged"   # the same end: no-op
    with pytest.raises(QCRError) as caught:
        store_.transition("m1", "failed")
    assert caught.value.code == "terminal"
    assert [event["state"] for event in store_.events("m1")] == ["queued", "delivered", "acknowledged"]


def test_unread_messages_expire_and_listing_filters(box):
    store_, now = box
    _send(store_, "m1")
    _send(store_, "m2", recipient_connection="other")
    store_.transition("m1", "delivered")
    assert [one["message_id"] for one in store_.list("p", recipient_connection="codex")] == ["m1"]
    assert store_.sent_since("p", "s1", 0) == 2
    now[0] += 8 * 24 * 3600
    assert store_.list("p", recipient_connection="codex")[0]["state"] == "expired"


# -- through the host ------------------------------------------------------------------
@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "codex.jsonl"))
    made = []

    def make(**kwargs):
        one = World(tmp_path, **kwargs)
        grant(one, others=kwargs.get("others", "auto"))
        made.append(one)
        return one
    yield make
    for one in made:
        one.close()


def grant(w, actor=ACTOR, others="auto", connection="claude", session="s-parent", targets=("codex",)):
    grants = [{"permission": "agents.message", "resource_id": delegation.message_resource(one)}
              for one in targets]
    w.host._on_actor({"actor": actor, "grants": grants, "agents": {
        "session": session, "connection": connection, "name": connection.title(), "folder": str(w.folder),
        "mode": "full", "others": others, "restrictions": {}}})


def send(w, target="codex", actor=ACTOR, **fields):
    return w.ask("invoke", actor, capability="agents.%s.message" % target,
                 input=dict({"body": "The port is 8070 now."}, **fields))


def test_a_message_without_a_reply_waits_in_the_inbox_and_starts_nothing(world, tmp_path):
    w = world()
    sent = send(w)
    done = w.wait(sent["execution"]["execution_id"])
    assert done["state"] == "succeeded" and done["output"]["state"] == "delivered"
    assert not _codex_turns(tmp_path)                    # no turn, no tokens
    inbox = w.host._on_messages({"recipient_connection": "codex"})["messages"]
    assert [(one["body"], one["state"], one["sender_session"]) for one in inbox] == [
        ("The port is 8070 now.", "delivered", "s-parent")]
    message_id = inbox[0]["message_id"]
    assert message_id == done["execution_id"]
    assert w.host._on_message_ack({"message_id": message_id, "by": "you"})["message"]["state"] == "acknowledged"
    with pytest.raises(QCRError):
        w.host._on_message_ack({"message_id": message_id})


def test_a_question_is_answered_by_one_read_only_turn_and_the_reply_comes_back(world, tmp_path):
    w = world()
    done = w.wait(send(w, body="Which port do you use?", expects_reply=True)["execution"]["execution_id"])
    assert done["state"] == "succeeded", done
    output = done["output"]
    assert output["state"] == "acknowledged" and output["reply"]["body"].startswith("Answer: ")
    turns = [row for row in _codex_turns(tmp_path) if row.get("method") == "turn/start"]
    assert len(turns) == 1 and turns[0]["params"]["sandboxPolicy"]["type"] == "readOnly"
    assert "Which port do you use?" in turns[0]["params"]["input"][0]["text"]
    child = sessions.load(w.root, output["session_id"])
    assert child["mode"] == "read" and child["others"] == "deny" and child["source"]["label"] == "Message"
    back = w.host._on_messages({"recipient_session": "s-parent"})["messages"]
    assert [one["kind"] for one in back] == ["reply"] and back[0]["reply_to"] == done["execution_id"]
    assert back[0]["sender_connection"] == "codex" and back[0]["state"] == "delivered"
    assert w.host.messages.get(done["execution_id"])["state"] == "acknowledged"


def test_a_reply_goes_back_to_the_conversation_that_wrote(world):
    w = world()
    original = w.wait(send(w)["execution"]["execution_id"])["execution_id"]
    codex_actor = "agent:codex/s-codex"
    grant(w, actor=codex_actor, connection="codex", session="s-codex", targets=("claude",))
    replied = w.wait(send(w, "claude", codex_actor, body="Noted.", reply_to=original)["execution"]["execution_id"],
                     actor=codex_actor)
    assert replied["state"] == "succeeded", replied
    reply = w.host.messages.get(replied["execution_id"])
    assert reply["kind"] == "reply" and reply["recipient_session"] == "s-parent"
    assert reply["conversation_id"] == original
    assert w.host.messages.get(original)["state"] == "acknowledged"
    # Nobody replies to a message that was not addressed to them.
    grant(w, actor="agent:claude/s-other", session="s-other")
    stranger = send(w, "codex", "agent:claude/s-other", body="x", reply_to=original)
    assert w.wait(stranger["execution"]["execution_id"], actor="agent:claude/s-other")["error"]["code"] == "not_found"


def test_approve_holds_the_message_until_the_person_answers(world, tmp_path):
    w = world(others="approve")
    first = send(w)["execution"]["execution_id"]
    assert w.host.messages.get(first)["state"] == "awaiting_approval"
    card = next(one for one in w.sent if one.get("type") == "agent_approval")
    assert card["kind"] == "message" and card["body"] == "The port is 8070 now."
    # One waiting message per recipient: the next one is busy, not lost.
    assert send(w)["error"]["code"] == "busy"
    w.host._on_agent_answer({"execution_id": first, "approve": False, "by": "you"})
    assert w.wait(first)["error"]["code"] == "denied"
    assert w.host.messages.get(first)["state"] == "denied"
    second = send(w)["execution"]["execution_id"]
    w.host._on_agent_answer({"execution_id": second, "approve": True, "by": "you"})
    assert w.wait(second)["output"]["state"] == "delivered"


def test_cancel_while_waiting_fails_the_message(world):
    w = world(others="approve")
    waiting = send(w)["execution"]["execution_id"]
    w.ask("cancel", execution_id=waiting)
    assert w.host.messages.get(waiting)["state"] == "failed"


def test_a_recipient_that_works_alone_takes_no_messages(world):
    w = world()
    codex = store.get("codex", w.agents)
    store.upsert(dict(codex, others_default="deny"), w.agents)
    refused = w.wait(send(w)["execution"]["execution_id"])
    assert refused["error"]["code"] == "recipient_unavailable"


def test_deny_own_connection_rate_and_secrets(world, monkeypatch):
    w = world()
    grant(w, targets=("codex", "claude"))
    assert w.wait(send(w, "claude")["execution"]["execution_id"])["error"]["code"] == "authorization_denied"
    monkeypatch.setattr(delegation, "MAX_MESSAGES_PER_HOUR", 2)
    # A refused message was never recorded, so it does not count.
    w.wait(send(w)["execution"]["execution_id"])
    w.wait(send(w, body="again")["execution"]["execution_id"])
    limited = w.wait(send(w, body="once more")["execution"]["execution_id"])
    assert limited["error"]["code"] == "limit_exceeded"
    before = len(w.host.store.list("p", limit=1000))
    secret = send(w, body="key sk-" + "c" * 30)
    assert secret["error"]["code"] == "secret_in_context"
    assert len(w.host.store.list("p", limit=1000)) == before
    grant(w, others="deny")
    assert w.wait(send(w, body="later")["execution"]["execution_id"])["error"]["code"] == "authorization_denied"


def test_messages_are_kept_in_the_project_folder(world):
    w = world()
    w.wait(send(w)["execution"]["execution_id"])
    assert os.path.isfile(os.path.join(os.path.dirname(w.host.context_dir), "messages.sqlite"))
