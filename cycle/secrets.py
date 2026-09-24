"""Where a cycle's secret variables live, which is never in the cycle file.

A cycle declares that a variable is secret; the value is kept here, in the
user's own data, and the cycle file carries nothing but the name::

    cycles/nightly.yaml          variables: {token: {secret: true}}
    <secrets file>               nightly: {token: <encrypted>}

That split is the whole point. ``cycles/`` is committed and shipped inside the
build, so a value written there travels to everyone who ever clones or installs
the project - which is exactly how a live API key ended up in a cycle file
already. A cycle with secret variables can be committed and handed to a
colleague; they supply their own values.

**What the encryption is and is not.** Values are encrypted with Fernet, and the
key sits beside the file at mode 0600. That means a secret is not readable at a
glance - not in a screen share, not by a backup tool, not by something that
slurps a JSON file and posts it somewhere. It does **not** protect against
somebody who already has your user account, because the key is readable by that
account by necessity: the core has to decrypt without asking anybody anything in
the middle of a run. Anyone reading this should size their trust accordingly;
the honest summary is "kept out of the project and out of plain sight", not
"safe from an attacker on this machine".

The GUI never touches this file. It depends on PySide6 and nothing else - no
``cryptography`` - so it asks the core, exactly as it does for cycle files.
"""

import json
import os
import stat

import runtime_paths

#: Both files live together, and the key is useless without the store.
FILE_NAME = "cyclesecrets.json"
KEY_NAME = "cyclesecrets.key"

#: Owner read/write only. The same posture ``users.json`` takes, and for the
#: same reason - this file grows real credentials.
PRIVATE = 0o600


class SecretsError(Exception):
    """The secrets file could not be read or written."""


def default_path():
    """Where the store goes when nobody has said otherwise.

    Beside the rest of the user's data, never in a checkout - the one place a
    file of credentials must not be.
    """
    return os.path.join(runtime_paths.user_data_root(), FILE_NAME)


def resolve_path(configured=""):
    """The path in force: what Settings says, or the default."""
    configured = (configured or "").strip()
    return os.path.expanduser(configured) if configured else default_path()


# -- the key ------------------------------------------------------------------
def _key_path(path):
    return os.path.join(os.path.dirname(os.path.abspath(path)), KEY_NAME)


def _key(path, make=True):
    """The key for this store, made once and then read.

    Written before any secret is, and with the same permissions: a key file
    somebody else can read makes the encryption decorative.
    """
    from cryptography.fernet import Fernet

    where = _key_path(path)
    try:
        with open(where, "rb") as handle:
            found = handle.read().strip()
        if found:
            return found
    except OSError:
        pass
    if not make:
        return b""

    generated = Fernet.generate_key()
    directory = os.path.dirname(where) or "."
    os.makedirs(directory, exist_ok=True)
    # Opened O_EXCL at 0600 rather than written and chmod-ed after: between
    # those two there is a moment when the key is world-readable, and that
    # moment is the whole attack.
    try:
        descriptor = os.open(where, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE)
    except FileExistsError:
        with open(where, "rb") as handle:     # somebody beat us to it
            return handle.read().strip()
    except OSError as exc:
        raise SecretsError("cannot write the key %s: %s" % (where, exc))
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(generated)
    return generated


# -- the store ----------------------------------------------------------------
def load(path=None):
    """Every secret, decrypted, as ``{cycle id: {name: value}}``.

    A missing file is an empty store, not an error - it is the ordinary state
    before anybody has set a secret. A value that will not decrypt is dropped
    rather than raised on: one unreadable entry must not make every other
    secret unreachable, and the run that needs it will say so itself.
    """
    from cryptography.fernet import Fernet, InvalidToken

    path = resolve_path(path)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as exc:
        raise SecretsError("cannot read %s: %s" % (path, exc))
    if not isinstance(document, dict):
        raise SecretsError("%s has to contain an object" % path)

    key = _key(path, make=False)
    if not key:
        raise SecretsError("the key for %s is missing, so nothing in it can be "
                           "read. Set the secrets again." % path)
    cipher = Fernet(key)

    found = {}
    for cycle_id, entries in (document.get("secrets") or {}).items():
        if not isinstance(entries, dict):
            continue
        for name, value in entries.items():
            try:
                found.setdefault(cycle_id, {})[name] = cipher.decrypt(
                    str(value).encode("utf-8")).decode("utf-8")
            except (InvalidToken, ValueError, TypeError):
                continue
    return found


def save(store, path=None):
    """Write every secret back, encrypted. Returns the path."""
    from cryptography.fernet import Fernet

    path = resolve_path(path)
    cipher = Fernet(_key(path))
    payload = {"version": 1, "secrets": {}}
    for cycle_id, entries in (store or {}).items():
        for name, value in (entries or {}).items():
            payload["secrets"].setdefault(cycle_id, {})[name] = cipher.encrypt(
                str(value).encode("utf-8")).decode("ascii")

    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    temp = os.path.join(directory, ".%s.tmp" % os.path.basename(path))
    try:
        # Created private from the first byte, for the reason the key is.
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, PRIVATE)
    except OSError as exc:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise SecretsError("cannot write %s: %s" % (path, exc))
    return path


# -- one at a time ------------------------------------------------------------
def get(cycle_id, name, path=None):
    """One secret's value, or "" when it has never been set."""
    try:
        return load(path).get(cycle_id, {}).get(name, "")
    except SecretsError:
        return ""


def set_secret(cycle_id, name, value, path=None):
    """Write one secret. An empty value removes it."""
    try:
        store = load(path)
    except SecretsError:
        store = {}
    if value:
        store.setdefault(cycle_id, {})[name] = value
    else:
        store.get(cycle_id, {}).pop(name, None)
    return save(store, path)


def copy_to(source, target, names_=(), path=None):
    """Give ``target`` the secrets ``source`` has. Returns the names copied.

    For the ordinary case of one credential shared by several cycles: the same
    Jira token is wanted by every cycle that reads Jira, and typing it again
    for each is how one of them ends up with last month's.

    The value is decrypted and re-encrypted **inside this call**. It is not
    returned, not printed and not passed on a command line - which is the
    whole reason this is a store operation rather than a read followed by a
    write by whoever wanted it.

    Anything already set on the target is left alone. Overwriting a credential
    somebody deliberately made different would be the one outcome nobody could
    have asked for; to replace one, delete it first.
    """
    source, target = str(source or ""), str(target or "")
    if not source or not target or source == target:
        return []
    try:
        store = load(path)
    except SecretsError:
        return []
    have = store.get(source) or {}
    wanted = [one for one in (names_ or sorted(have)) if one in have]
    already = store.get(target) or {}

    copied = []
    for name in wanted:
        if name in already:
            continue
        store.setdefault(target, {})[name] = have[name]
        copied.append(name)
    if copied:
        save(store, path)
    return copied


def delete(cycle_id, name=None, path=None):
    """Forget one secret, or every secret of one cycle."""
    return set_secret(cycle_id, name, "", path) if name else _drop(cycle_id, path)


def _drop(cycle_id, path):
    try:
        store = load(path)
    except SecretsError:
        return resolve_path(path)
    store.pop(cycle_id, None)
    return save(store, path)


def names(cycle_id, path=None):
    """Which secrets of this cycle have a value set. Never the values."""
    try:
        return sorted(load(path).get(cycle_id, {}))
    except SecretsError:
        return []


def describe(cycle_id, declared, path=None):
    """Which declared secrets are set and which are still empty.

    What a form shows: never a value, only whether there is one. Reading a
    secret to decide how to draw a row would put it somewhere it does not need
    to be.
    """
    have = set(names(cycle_id, path))
    return [{"name": name, "set": name in have} for name in sorted(declared)]


def is_private(path):
    """True when the file is readable by its owner alone. For a warning row."""
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return True                 # nothing there yet is nothing exposed
    return not mode & (stat.S_IRWXG | stat.S_IRWXO)
