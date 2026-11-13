"""The agent's way to the QCR host (ADR-003 D4): a private socket and a token."""

import json
import os
import socket
import stat
import time

import pytest

from qcr.agent_socket import AgentGateway, SocketError, TokenBook, digest, private_directory

TOKEN = "t" * 64


@pytest.fixture
def gateway(tmp_path):
    seen = []

    def handle(actor, message):
        seen.append((actor, message))
        return {"ok": True, "echo": message.get("type")}
    tokens = TokenBook()
    tokens.add("agent:claude/s1", digest(TOKEN), time.time() + 60)
    one = AgentGateway(handle, tokens, str(tmp_path / "qv"), name="s.sock")
    one.seen = seen
    yield one
    one.close()


def connect(gateway):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    client.connect(gateway.path)
    return client, client.makefile("rb")


def say(client, reader, message):
    client.sendall((json.dumps(message) + "\n").encode())
    line = reader.readline()
    return json.loads(line) if line else None


def test_the_directory_and_the_socket_are_private(gateway, tmp_path):
    assert stat.S_IMODE(os.stat(tmp_path / "qv").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(gateway.path).st_mode) == 0o600
    gateway.close()
    assert not os.path.exists(gateway.path)


def test_a_directory_open_to_others_or_a_symlink_is_refused_not_repaired(tmp_path):
    open_dir = tmp_path / "open"
    open_dir.mkdir(mode=0o755)
    os.chmod(open_dir, 0o755)
    with pytest.raises(SocketError) as caught:
        private_directory(str(open_dir))
    assert caught.value.code == "socket_unsafe" and stat.S_IMODE(os.stat(open_dir).st_mode) == 0o755
    (tmp_path / "real").mkdir(mode=0o700)
    os.symlink(tmp_path / "real", tmp_path / "link")
    with pytest.raises(SocketError):
        private_directory(str(tmp_path / "link"))


def test_nothing_is_answered_before_a_valid_token(gateway):
    client, reader = connect(gateway)
    assert say(client, reader, {"type": "invoke", "id": 1})["ok"] is False
    assert reader.readline() == b"", "closed after a failed auth"
    client, reader = connect(gateway)
    assert say(client, reader, {"type": "auth", "token": "wrong"})["error"]["code"] == "authorization_denied"
    assert gateway.seen == []


def test_after_auth_every_request_is_the_token_s_actor_whatever_it_claims(gateway):
    client, reader = connect(gateway)
    assert say(client, reader, {"type": "auth", "token": TOKEN}) == {"type": "auth", "ok": True,
                                                                    "actor": "agent:claude/s1"}
    reply = say(client, reader, {"type": "invoke", "id": 7, "actor": "gui-user", "capability": "x"})
    assert reply == {"type": "reply", "id": 7, "ok": True, "echo": "invoke"}
    actor, message = gateway.seen[0]
    assert actor == "agent:claude/s1" and "actor" not in message


def test_an_expired_or_revoked_token_stops_working(gateway):
    gateway.tokens.add("agent:old", digest("o" * 64), time.time() - 1)
    client, reader = connect(gateway)
    assert say(client, reader, {"type": "auth", "token": "o" * 64})["ok"] is False
    client, reader = connect(gateway)
    say(client, reader, {"type": "auth", "token": TOKEN})
    gateway.tokens.forget_actor("agent:claude/s1")
    reply = say(client, reader, {"type": "list", "id": 2})
    assert reply["ok"] is False and reply["error"]["code"] == "authorization_denied"


def test_a_line_that_is_not_json_closes_the_connection(gateway):
    client, reader = connect(gateway)
    say(client, reader, {"type": "auth", "token": TOKEN})
    client.sendall(b"not json\n")
    assert reader.readline() == b""


def test_tokens_are_kept_as_digests_only():
    book = TokenBook()
    book.add("a", digest("secret-token"), time.time() + 60)
    assert "secret-token" not in json.dumps(book._tokens)
    assert book.actor_for("secret-token") == "a" and book.actor_for("other") is None
    with pytest.raises(ValueError):
        book.add("a", "not-a-digest", time.time() + 60)


def test_a_directory_too_deep_for_a_socket_falls_back_to_a_private_temp_one(tmp_path):
    deep = tmp_path / ("d" * 90)
    gateway = AgentGateway(lambda actor, message: {"ok": True}, TokenBook(), str(deep), name="deep-test.sock")
    try:
        assert gateway.path == "/tmp/qavector-%d/deep-test.sock" % os.getuid()
        assert stat.S_IMODE(os.stat(os.path.dirname(gateway.path)).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(gateway.path).st_mode) == 0o600
    finally:
        gateway.close()
    assert not os.path.exists(gateway.path)
