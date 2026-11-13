"""How an agent reaches the QCR host (ADR-003 D4): a private Unix socket and a token.

The host's own pipe belongs to the GUI. An agent's MCP server is a process the
agent's CLI starts, so it cannot have that pipe; it gets this socket instead -
and nothing on it is trusted until it shows a token the GUI issued.

**The socket** lives in a directory only this user can enter
(``$XDG_RUNTIME_DIR/qavector``, else ``~/.cache/qavector``, mode 0700, owned by
this user, not a symlink - anything else is refused rather than repaired) and
is itself 0600. A connection from a process of another user is closed at once
(``SO_PEERCRED``), even if the file modes were somehow wider.

**The token** is made by the GUI for one agent turn, written to a 0600 file
whose *path* - never the token - goes on the MCP server's command line, and told
to the host by its SHA-256 alone, with the actor it stands for and when it
expires. The host compares digests in constant time. The first line on a
connection must be ``{"type": "auth", "token": ...}``; from then on every
request is that actor's, whatever the request says about who it is.

**Bounded**: a line is at most 1 MB, a connection at most 16 at once, a token
stops working when it expires or its actor is revoked. Every failure is a
reply with a code; the connection is closed only on a failed auth or a line
that is not JSON.
"""

import hashlib
import hmac
import json
import logging
import os
import socket
import stat
import struct
import threading
import time

log = logging.getLogger("qcr.agent_socket")

LINE_BYTES = 1024 * 1024
#: A Unix socket's path is limited (108 bytes on Linux, less elsewhere).
PATH_BYTES = 100
MAX_CONNECTIONS = 16
AUTH_SECONDS = 10


class SocketError(Exception):
    """The socket cannot be made safely; ``code`` says why."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def socket_directory(environ=None):
    """Where the socket goes: a per-user runtime directory, else the cache."""
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_RUNTIME_DIR") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "qavector")


def private_directory(path):
    """Make (or accept) ``path`` as a directory only this user can enter.

    An existing one that is a symlink, someone else's, or open to others is
    refused: fixing it here would be fixing it under whoever made it so.
    """
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise SocketError("socket_unavailable", "Cannot make the socket directory: %s" % exc.strerror) from None
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SocketError("socket_unsafe", "The socket directory is not a plain directory.")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise SocketError("socket_unsafe", "The socket directory is not private to this user.")
    return path


def digest(token):
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


class TokenBook:
    """Token digests the GUI issued: digest -> (actor, expires at). Thread-safe."""

    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._tokens = {}

    def add(self, actor, token_sha256, expires_at):
        if not isinstance(token_sha256, str) or len(token_sha256) != 64:
            raise ValueError("A token is given by its SHA-256, in hex.")
        with self._lock:
            self._tokens[token_sha256.lower()] = (actor, float(expires_at))

    def forget_actor(self, actor):
        with self._lock:
            for key in [key for key, (one, _) in self._tokens.items() if one == actor]:
                del self._tokens[key]

    def actor_for(self, token):
        """The actor a token stands for, or None - compared digest to digest, in constant time."""
        if not isinstance(token, str) or not token or len(token) > 512:
            return None
        given = digest(token)
        now = self._clock()
        with self._lock:
            found = None
            for key, (actor, expires_at) in self._tokens.items():
                if hmac.compare_digest(key, given) and now < expires_at:
                    found = actor
            return found


def _peer_uid(connection):
    """The uid of the process on the other end (Linux), or None where it cannot be told."""
    if not hasattr(socket, "SO_PEERCRED"):
        return None
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", raw)[1]
    except OSError:
        return None


class AgentGateway:
    """Listens on the socket; hands each authenticated request to ``handle``.

    ``handle(actor, message) -> reply dict`` is the host's: it answers for that
    actor only. Replies go back on the same connection, one JSON line each.
    """

    def __init__(self, handle, tokens, directory=None, *, name=None):
        self._handle, self.tokens = handle, tokens
        name = name or "qcr-%d.sock" % os.getpid()
        directory = directory or socket_directory()
        if len(os.path.join(directory, name).encode()) > PATH_BYTES:
            # Too long to bind: a private directory of this user's under the temp
            # folder instead - checked exactly as strictly (owner, 0700, no symlink).
            directory = os.path.join("/tmp", "qavector-%d" % os.getuid())
        directory = private_directory(directory)
        self.path = os.path.join(directory, name)
        if os.path.lexists(self.path):
            os.unlink(self.path)        # ours by name: a previous host of this pid
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous = os.umask(0o177)
        try:
            self._server.bind(self.path)
        finally:
            os.umask(previous)
        os.chmod(self.path, 0o600)
        self._server.listen(MAX_CONNECTIONS)
        self._connections = set()
        self._lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(target=self._accept, name="qcr-agent-socket", daemon=True)
        self._thread.start()

    def close(self):
        self._closed = True
        try:
            self._server.close()
        except OSError:
            pass
        with self._lock:
            for connection in list(self._connections):
                try:
                    connection.close()
                except OSError:
                    pass
            self._connections.clear()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _accept(self):
        while not self._closed:
            try:
                connection, _ = self._server.accept()
            except OSError:
                return
            uid = _peer_uid(connection)
            with self._lock:
                crowded = len(self._connections) >= MAX_CONNECTIONS
                if not crowded and (uid is None or uid == os.getuid()):
                    self._connections.add(connection)
                    allowed = True
                else:
                    allowed = False
            if not allowed:
                connection.close()
                continue
            threading.Thread(target=self._serve, args=(connection,), name="qcr-agent", daemon=True).start()

    def _serve(self, connection):
        reader = connection.makefile("rb")
        try:
            connection.settimeout(AUTH_SECONDS)
            first = self._line(reader)
            actor = None
            if isinstance(first, dict) and first.get("type") == "auth":
                actor = self.tokens.actor_for(first.get("token"))
            if actor is None:
                self._send(connection, {"type": "auth", "ok": False,
                                        "error": {"code": "authorization_denied", "message": "Not a valid token."}})
                return
            connection.settimeout(None)
            self._send(connection, {"type": "auth", "ok": True, "actor": actor})
            while not self._closed:
                message = self._line(reader)
                if message is None:
                    return
                if not isinstance(message, dict):
                    self._send(connection, {"type": "error", "error": {"code": "invalid_message",
                                                                       "message": "Expected a JSON object."}})
                    return
                if self.tokens.actor_for(first.get("token")) != actor:
                    # Expired or revoked while connected: the next request is refused.
                    self._send(connection, {"type": "reply", "id": message.get("id"), "ok": False, "error": {
                        "code": "authorization_denied", "message": "The token is no longer valid."}})
                    return
                request = dict(message)
                request.pop("actor", None)      # the connection's actor, never the message's
                try:
                    reply = self._handle(actor, request)
                except Exception:
                    log.exception("agent request failed")
                    reply = {"ok": False, "error": {"code": "host_error", "message": "The host could not handle it."}}
                self._send(connection, dict(reply, type="reply", id=message.get("id")))
        except (OSError, ValueError):
            return
        finally:
            with self._lock:
                self._connections.discard(connection)
            try:
                reader.close()
                connection.close()
            except OSError:
                pass

    @staticmethod
    def _line(reader):
        raw = reader.readline(LINE_BYTES + 1)
        if not raw:
            return None
        if len(raw) > LINE_BYTES:
            raise ValueError("line too long")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValueError("not JSON") from None

    @staticmethod
    def _send(connection, message):
        connection.sendall((json.dumps(message, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8"))
