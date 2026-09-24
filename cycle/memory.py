"""What the project remembers between runs.

A run today starts knowing nothing. It cannot ask whether this task was already
done, whether a previous attempt got halfway, how much of a budget has been
spent on it, or whether another run is working on it right now - the ``${...}``
scope has four roots and every one of them is about the run in progress. So a
cycle that is meant to pick work up where it was left reads as a cycle that
does everything from scratch every time, and the difference only shows when it
redoes an afternoon's work.

This is the store that answers those questions. Deliberately small, and shaped
after ``cycle/secrets.py``, which solved the same problem one layer down: a
keyed file under the user's own data, an atomic write, a path in Settings, and
a GUI that never opens it because it depends on PySide6 and nothing else.

**Not encrypted, and that is the point.** Secrets are hidden because reading
them is the harm. Memory is the opposite: "why did it skip that task" is a
question somebody will ask, and the answer being a JSON file they can open is
worth more than any confidentiality it does not need. Do not put a credential
in it - that is what the other file is for.

**Keys mean whatever the cycle says they mean.** The core does not decide what
a task is: a cycle file composes a key out of its own variables, and two cycles
that disagree about identity simply do not collide. Putting "a task is a Jira
issue in a repository on a branch" in here would make the engine's opinion
about somebody's workflow load-bearing.

**Locked, unlike the secrets store.** Two concurrent writes there lose one, and
for a credential somebody typed that is a bad day. Here it would be a claim
that two runs both believe they hold, which is the one thing a claim exists to
prevent - so every mutation takes a lock file first.
"""

import errno
import json
import os
import time

import runtime_paths

#: Where the store lives when nobody has said otherwise.
FILE_NAME = "cyclememory.json"

#: The lock taken around every mutation. Beside the file, so moving the store
#: moves its lock with it.
LOCK_NAME = "cyclememory.lock"

#: How long to keep trying for the lock before giving up. Every write here is
#: small; a wait longer than this means something is wrong rather than busy.
LOCK_TIMEOUT = 10.0

#: How long a lock file may sit untouched before it is treated as left behind
#: by something that died. Generous, because breaking a live lock is worse
#: than waiting: a process paused longer than this - a laptop asleep mid-write -
#: could still have its lock broken under it, and that is the honest limit of
#: doing this with a file rather than with a database.
LOCK_STALE = 120.0

#: How long a claim is good for when the caller does not say. Long enough for a
#: cycle that builds something, short enough that a machine that crashed does
#: not hold a task hostage until somebody notices.
CLAIM_SECONDS = 3600.0


class MemoryError_(Exception):
    """The store could not be read or written."""


# -- where it is --------------------------------------------------------------
def default_path():
    """Beside the rest of the user's data, never in a checkout."""
    return os.path.join(runtime_paths.user_data_root(), FILE_NAME)


def resolve_path(configured=""):
    """The path in force: what Settings says, or the default."""
    configured = (configured or "").strip()
    return os.path.expanduser(configured) if configured else default_path()


# -- reading ------------------------------------------------------------------
def load(path=None):
    """Every record, as ``{key: record}``. A missing file is an empty store.

    Never raises for an absent file: that is the ordinary state before anything
    has been remembered, and a cycle asking "have I done this" should hear "no"
    rather than an error.
    """
    path = resolve_path(path)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as exc:
        raise MemoryError_("cannot read %s: %s" % (path, exc))
    if not isinstance(document, dict):
        raise MemoryError_("%s has to contain an object" % path)
    found = document.get("records")
    return found if isinstance(found, dict) else {}


def recall(key, path=None):
    """One record's fields, or ``{}`` when nothing was ever remembered."""
    try:
        record = load(path).get(str(key)) or {}
    except MemoryError_:
        return {}
    fields = record.get("fields")
    return dict(fields) if isinstance(fields, dict) else {}


def known(key, path=None):
    """Whether anything is remembered about this key at all."""
    try:
        return str(key) in load(path)
    except MemoryError_:
        return False


def keys(prefix="", path=None):
    """Every key, or every key under a prefix. Sorted, for a readable listing."""
    try:
        return sorted(one for one in load(path) if one.startswith(prefix or ""))
    except MemoryError_:
        return []


def describe(key, path=None):
    """One record with its timestamps, for something that displays it."""
    try:
        record = load(path).get(str(key))
    except MemoryError_:
        record = None
    if not record:
        return {}
    return {"key": str(key), "fields": dict(record.get("fields") or {}),
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "claim": dict(record.get("claim") or {})}


# -- writing ------------------------------------------------------------------
def remember(key, fields, path=None):
    """Merge ``fields`` into this key's record. Returns the record's fields.

    Merged rather than replaced, because two steps of one cycle remember
    different things about the same task and neither should erase the other.
    A field set to ``None`` is removed, which is how something stops being
    remembered without forgetting the whole record.
    """
    key = str(key)
    with _change(path) as store:
        record = store.setdefault(key, {"created_at": time.time(), "fields": {}})
        held = record.setdefault("fields", {})
        for name, value in (fields or {}).items():
            if value is None:
                held.pop(name, None)
            else:
                held[name] = value
        record["updated_at"] = time.time()
        return dict(held)


def forget(key, path=None):
    """Remove one record entirely. True when there was one."""
    key = str(key)
    with _change(path) as store:
        return store.pop(key, None) is not None


def bump(key, name, by=1, path=None):
    """Add to a counter and return what it now is.

    The budget primitive, and the reason it lives here rather than in a plugin:
    read-modify-write from two runs loses one of them, and a budget that
    silently forgets an attempt is a budget that does not bound anything.
    """
    key = str(key)
    with _change(path) as store:
        record = store.setdefault(key, {"created_at": time.time(), "fields": {}})
        held = record.setdefault("fields", {})
        try:
            now = int(held.get(name, 0)) + int(by)
        except (TypeError, ValueError):
            now = int(by)
        held[name] = now
        record["updated_at"] = time.time()
        return now


# -- claims -------------------------------------------------------------------
def claim(key, owner, seconds=CLAIM_SECONDS, path=None):
    """Take this key for ``owner``. Returns ``(ok, holder)``.

    ``holder`` is whoever has it when the answer is no, so a run can say "this
    is already being done by X" rather than only that it could not start.

    A claim expires: a machine that crashed mid-run must not hold a task until
    somebody notices. Taking it again as the same owner extends it, which is
    what a run that was resumed needs.
    """
    key, owner = str(key), str(owner)
    with _change(path) as store:
        record = store.setdefault(key, {"created_at": time.time(), "fields": {}})
        held = record.get("claim") or {}
        now = time.time()
        if (held.get("owner") and held.get("owner") != owner
                and float(held.get("until") or 0) > now):
            return False, str(held["owner"])
        record["claim"] = {"owner": owner, "since": held.get("since", now),
                           "until": now + float(seconds)}
        record["updated_at"] = now
        return True, owner


def release(key, owner, path=None):
    """Give up a claim. Only the holder may, so a late process cannot free it.

    Returns True when this owner held it. A claim that had already expired and
    been taken by somebody else is *not* released - the newcomer's work is not
    this run's to cancel.
    """
    key, owner = str(key), str(owner)
    with _change(path) as store:
        record = store.get(key)
        if not record:
            return False
        if (record.get("claim") or {}).get("owner") != owner:
            return False
        record.pop("claim", None)
        record["updated_at"] = time.time()
        return True


def holder(key, path=None):
    """Who holds this key right now, or "" when nobody does."""
    try:
        record = load(path).get(str(key)) or {}
    except MemoryError_:
        return ""
    held = record.get("claim") or {}
    if not held.get("owner") or float(held.get("until") or 0) <= time.time():
        return ""
    return str(held["owner"])


# -- the file ------------------------------------------------------------------
class _change(object):
    """Load, hand over, save - with the file locked for the whole of it.

    A context manager rather than a pair of functions so there is no way to
    write a read-modify-write that forgets the lock: the only way to change
    anything is to be inside one.
    """

    def __init__(self, path):
        self.path = resolve_path(path)
        self._lock = None
        self._store = None

    def __enter__(self):
        self._lock = _take_lock(self.path)
        self._store = load(self.path)
        return self._store

    def __exit__(self, kind, value, trace):
        try:
            if kind is None:
                _save(self._store, self.path)
        finally:
            _drop_lock(self._lock)
        return False


def _save(store, path):
    """Write every record back, atomically."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    payload = {"version": 1, "records": store}
    temp = os.path.join(directory, ".%s.tmp" % os.path.basename(path))
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except OSError as exc:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise MemoryError_("cannot write %s: %s" % (path, exc))


def _lock_path(path):
    return os.path.join(os.path.dirname(os.path.abspath(path)), LOCK_NAME)


def _take_lock(path, timeout=LOCK_TIMEOUT):
    """Hold the lock file, waiting for whoever has it. Returns the path.

    ``O_EXCL`` rather than ``flock``: the same code has to work on Windows,
    where the POSIX call is not there. The cost is that a lock left behind by
    something that died has to be aged out rather than released by the kernel -
    see ``LOCK_STALE`` for what that costs.
    """
    where = _lock_path(path)
    os.makedirs(os.path.dirname(where) or ".", exist_ok=True)
    deadline = time.time() + timeout
    while True:
        try:
            descriptor = os.open(where, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            os.close(descriptor)
            return where
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise MemoryError_("cannot lock %s: %s" % (where, exc))
        if _is_stale(where):
            _drop_lock(where)
            continue
        if time.time() >= deadline:
            raise MemoryError_(
                "%s has been locked by something else for %ds. If nothing is "
                "running, delete it." % (where, timeout))
        time.sleep(0.05)


def _is_stale(where):
    try:
        return time.time() - os.path.getmtime(where) > LOCK_STALE
    except OSError:
        return False            # gone already, which is what we wanted


def _drop_lock(where):
    if not where:
        return
    try:
        os.unlink(where)
    except OSError:
        pass
