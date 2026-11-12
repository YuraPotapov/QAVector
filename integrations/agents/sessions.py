"""Conversations with an agent, and everything said in them, kept in the project.

A *session* is one conversation: one connection, one folder the agent works in,
and the turns taken so far. Whoever talks to an agent through this integration
gets one, so there is a single place that says what every agent was asked and
what it did::

    <project>/agent-sessions/
      20261111-172345-3fa91c/
        session.json     who, where, how, and a line per turn
        events.jsonl     what was said and done, in order, whole
        turns/0001/      prompt.md, stdout.log (the CLI's own stream), stderr.log

**Derived, not stored twice.** The list of sessions and the log of turns are
read off the ``session.json`` files; there is no index beside them to disagree
with a folder somebody deleted by hand.

**A source it does not interpret.** A session says where it came from with a
``source`` its creator gives - ``{"label": ..., "detail": ...}`` - which this
module keeps and shows and never reads.

**Private.** A conversation holds whatever was pasted into it and whatever the
agent read, so every file here is owner-only.
"""

import json
import os
import re
import secrets
import shutil
import time
from contextlib import contextmanager

import runtime_paths

DIR_NAME = "agent-sessions"
VERSION = 1

#: What a session id looks like: when it was made, and a few random digits.
#: Checked on every id that arrives from outside, because an id is a folder
#: name and must not be able to name anywhere else.
ID_PATTERN = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")

#: How a turn ended. ``running`` only while one is; a turn still called running
#: by a session nobody holds was cut off, and is reported as ``interrupted``.
RUNNING, COMPLETED, FAILED, STOPPED, INTERRUPTED = (
    "running", "completed", "failed", "stopped", "interrupted")

#: How long a title made from a first message may be.
TITLE_CHARS = 60

PRIVATE_FILE = 0o600
PRIVATE_DIR = 0o700


class SessionError(Exception):
    """A session that cannot be found, read or changed; ``kind`` says which."""

    def __init__(self, message, kind="error"):
        super().__init__(message)
        self.kind = kind


# -- where --------------------------------------------------------------------
def default_root():
    """Where sessions go when nobody has said otherwise: beside the user's data."""
    return os.path.join(runtime_paths.user_data_root(), DIR_NAME)


def resolve_root(configured=""):
    configured = (configured or "").strip()
    return os.path.abspath(os.path.expanduser(configured)) if configured else default_root()


def directory(root, session_id):
    """The folder of one session. Raises for an id that is not one."""
    if not isinstance(session_id, str) or not ID_PATTERN.match(session_id):
        raise SessionError("%r is not a session id." % (session_id,), "invalid")
    return os.path.join(resolve_root(root), session_id)


def turn_directory(root, session_id, number):
    return os.path.join(directory(root, session_id), "turns", "%04d" % number)


def make_turn_directory(root, session_id, number):
    """The folder one turn's files go in, made owner-only at every level."""
    where = turn_directory(root, session_id, number)
    _private_dir(os.path.dirname(where))
    return _private_dir(where)


def _private_dir(path):
    os.makedirs(path, mode=PRIVATE_DIR, exist_ok=True)
    return path


def _write(path, document):
    """Write one JSON document, atomically and owner-only."""
    temp = "%s.tmp" % path
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temp, path)
    except OSError as exc:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise SessionError("cannot write %s: %s" % (path, exc))


# -- one session ----------------------------------------------------------------
def title_from(message):
    """A title for a conversation nobody named: how it began."""
    for line in str(message or "").splitlines():
        line = " ".join(line.split())
        if line:
            return line if len(line) <= TITLE_CHARS else line[:TITLE_CHARS - 3].rstrip() + "..."
    return ""


def create(root, connection, folder, mode, model="", effort="", title="",
           source=None, restrictions=None, skills=None):
    """Start a conversation. Returns the session; nothing is asked of anybody.

    ``connection`` is the connection as its store holds it: its id is what the
    session names, and its name and kind are copied, so a conversation still
    says whom it was with after the connection is renamed or gone.
    """
    folder = os.path.abspath(os.path.expanduser(str(folder or "").strip() or "~"))
    if not os.path.isdir(folder):
        raise SessionError("%s is not a folder." % folder, "invalid")
    base = _private_dir(resolve_root(root))
    while True:
        session_id = "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), secrets.token_hex(3))
        where = os.path.join(base, session_id)
        try:
            os.mkdir(where, PRIVATE_DIR)
            break
        except FileExistsError:
            continue
        except OSError as exc:
            raise SessionError("cannot create %s: %s" % (where, exc))
    now = time.time()
    session = {
        "version": VERSION, "id": session_id, "title": str(title or "").strip(),
        "connection": connection["id"], "connection_name": connection.get("name", ""),
        "kind": connection["kind"], "folder": folder, "mode": mode,
        "model": str(model or "").strip(), "effort": str(effort or "").strip(),
        "restrictions": dict(restrictions or {}),
        # The library skills the agent is handed, by name: what the next turn
        # takes, and each turn writes down what it took.
        "skills": list(skills or []),
        # The conversation's id in the CLI's own store, and whether the CLI
        # has it yet: a first turn names it, every later one continues it.
        "provider_session": "", "provider_started": False,
        "source": dict(source or {"label": "Chat", "detail": ""}),
        "created": now, "updated": now, "state": "", "cost_usd": 0.0, "turns": [],
    }
    save(root, session)
    return session


def load(root, session_id):
    """One session as it was last saved. Raises :class:`SessionError`."""
    path = os.path.join(directory(root, session_id), "session.json")
    try:
        with open(path, encoding="utf-8") as handle:
            session = json.load(handle)
    except FileNotFoundError:
        raise SessionError("There is no session %s." % session_id, "not_found") from None
    except (OSError, ValueError) as exc:
        raise SessionError("cannot read %s: %s" % (path, exc))
    if not isinstance(session, dict) or session.get("id") != session_id:
        raise SessionError("%s is not this session's record." % path)
    return session


def save(root, session):
    _write(os.path.join(directory(root, session["id"]), "session.json"), session)


def described(root, session):
    """``session`` as a list shows it: what is on disk, and what is true now.

    The file says a turn is running from the moment it starts; whether one
    still is can only be asked of the lock. A turn the file calls running that
    nobody holds was cut off - the process was killed, or the machine went
    down - and saying "running" for ever would be the worst answer.
    """
    shown = dict(session)
    running = held(root, session["id"])
    turns = [dict(one) for one in session.get("turns") or []]
    if turns and turns[-1].get("state") == RUNNING and not running:
        turns[-1]["state"] = INTERRUPTED
    shown["turns"] = turns
    shown["running"] = running
    shown["state"] = RUNNING if running else (turns[-1]["state"] if turns else "")
    return shown


def listing(root, limit=0, offset=0, query=""):
    """``{sessions, total, more}``, the most recently used first.

    ``query`` narrows by title, folder, connection and source, as typed.
    ``limit`` 0 is all of them.
    """
    base = resolve_root(root)
    try:
        names = [name for name in os.listdir(base) if ID_PATTERN.match(name)]
    except OSError:
        names = []
    found = []
    for name in names:
        try:
            found.append(load(root, name))
        except SessionError:
            continue                    # half made, or not ours: not a session
    needle = " ".join(str(query or "").lower().split())
    if needle:
        def text(one):
            source = one.get("source") or {}
            return " ".join(str(part) for part in (
                one.get("title"), one.get("folder"), one.get("connection_name"),
                one.get("connection"), source.get("label"), source.get("detail"))).lower()
        found = [one for one in found if needle in text(one)]
    found.sort(key=lambda one: (one.get("updated") or 0, one["id"]), reverse=True)
    offset = max(0, int(offset or 0))
    limit = max(0, int(limit or 0))
    chosen = found[offset:offset + limit] if limit else found[offset:]
    return {"sessions": [described(root, one) for one in chosen], "total": len(found),
            "more": offset + len(chosen) < len(found)}


def delete(root, session_id):
    """Remove a conversation and everything kept of it. Refuses while a turn
    is being taken: a folder removed from under it is a turn that fails for no
    reason it can give."""
    load(root, session_id)
    with lock(root, session_id):
        shutil.rmtree(directory(root, session_id), ignore_errors=True)
    return session_id


# -- who is taking a turn -------------------------------------------------------
@contextmanager
def lock(root, session_id):
    """Hold a session for one turn. Raises ``SessionError(kind="busy")`` when
    another process has it: a conversation is one thing said at a time."""
    path = os.path.join(directory(root, session_id), ".lock")
    try:
        handle = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, PRIVATE_FILE), "r+b")
    except OSError as exc:
        raise SessionError("cannot open %s: %s" % (path, exc))
    with handle:
        try:
            if os.name == "nt":
                import msvcrt
                handle.write(b"\0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SessionError("The agent is still answering in this session.",
                               "busy") from None
        try:
            yield
        finally:
            try:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                pass


def held(root, session_id):
    """Whether a turn is being taken in this session right now."""
    try:
        if not os.path.exists(os.path.join(directory(root, session_id), ".lock")):
            return False
        with lock(root, session_id):
            return False
    except SessionError as exc:
        return exc.kind == "busy"


# -- what was said ----------------------------------------------------------------
class Record:
    """The record of one turn being written: ``events.jsonl``, appended to.

    Each event gets its place in the conversation (``seq``), when it happened
    and which turn it belongs to, and is on disk before anybody is told of it -
    so what a window showed is never more than what the file holds.
    """

    def __init__(self, root, session_id, turn):
        self.path = os.path.join(directory(root, session_id), "events.jsonl")
        self.turn = turn
        self.seq = _last_seq(self.path)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, PRIVATE_FILE)
        self._handle = os.fdopen(descriptor, "a", encoding="utf-8")

    def add(self, event):
        self.seq += 1
        # When it happened: now, unless whoever recorded a turn taken elsewhere
        # knows better.
        entry = dict(event, seq=self.seq, at=event.get("at") or time.time(), turn=self.turn)
        self._handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._handle.flush()
        return entry

    def close(self):
        self._handle.close()


def _last_seq(path):
    """The ``seq`` of the last whole event in the file; 0 when there is none."""
    last = 0
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    last = max(last, int(json.loads(line).get("seq") or 0))
                except (ValueError, TypeError, AttributeError):
                    continue            # a line cut off by a crash
    except OSError:
        pass
    return last


def events(root, session_id, after=0, limit=0):
    """The events of a conversation after ``seq`` ``after``, oldest first.

    ``limit`` (0 for all) keeps the *newest* that many: a long conversation is
    opened at its end, and the rest is asked for when somebody scrolls up.
    """
    path = os.path.join(directory(root, session_id), "events.jsonl")
    found = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and int(entry.get("seq") or 0) > int(after or 0):
                    found.append(entry)
    except OSError:
        return []
    limit = max(0, int(limit or 0))
    return found[-limit:] if limit else found


def append_turn(root, session_id, turn, events):
    """Record a turn taken somewhere else - its events already known.

    For whoever runs an agent's program itself rather than through
    ``runner.send`` and wants the conversation record to have it: ``turn`` is
    what the session's line for it says (``started``, ``ended``, ``state``,
    ``mode``, ``model``, ...), and ``events`` what happened in it, the person's
    message first. Numbered, written and saved under the session's lock, as a
    turn taken here would be. Returns the turn as recorded.
    """
    with lock(root, session_id):
        session = load(root, session_id)
        number = len(session.get("turns") or []) + 1
        turn = dict(turn, n=number)
        record = Record(root, session_id, number)
        try:
            for event in events or []:
                record.add(event)
        finally:
            record.close()
        session.setdefault("turns", []).append(turn)
        session.update(updated=turn.get("ended") or time.time(),
                       state=turn.get("state") or "",
                       cost_usd=round(sum(one.get("cost_usd") or 0
                                          for one in session["turns"]), 6))
        if turn.get("provider_session"):
            session["provider_session"] = turn["provider_session"]
        save(root, session)
    return turn


def activity(root, limit=500):
    """Every turn of every session, the newest first: the integration's log.

    Read off the sessions rather than kept beside them. Each entry is one turn
    with what its session says about where it ran and who asked.
    """
    found = []
    for session in listing(root)["sessions"]:
        for turn in session.get("turns") or []:
            found.append({
                "session": session["id"], "title": session.get("title", ""),
                "connection": session.get("connection", ""),
                "connection_name": session.get("connection_name", ""),
                "kind": session.get("kind", ""), "folder": session.get("folder", ""),
                "source": dict(session.get("source") or {}), **turn})
    found.sort(key=lambda one: one.get("started") or 0, reverse=True)
    limit = max(0, int(limit or 0))
    return found[:limit] if limit else found
