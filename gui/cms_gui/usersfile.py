"""Reading, validating and writing ``users.json`` - the core's own config file.

The schema is the launcher's, unchanged: a JSON array of objects with ``env``,
``class``, ``login``, ``password`` and an optional ``run-tests``. The GUI is a
form over that file and nothing more, so the CLI keeps working on it untouched.

The GUI reads it as *people* and *where they sign in*. A person - a
:class:`Credential` - is every row with the same login, password and class; each
of their rows puts them in one environment (the row's ``env``, the
environment's account key) with the scenarios they run there. One with no
environment yet is a single row whose ``env`` is empty, which the launcher reads
as an account no ``--env`` selects. The functions below change people and
places on a list of rows, so the Credentials table (which saves when told to)
and the Environments page (which saves at once, through :func:`change`) work on
the file the same way.

Validation deliberately mirrors ``session_launcher.load_users`` /
``_parse_tests_field``: the GUI must never write a file the launcher would then
refuse to read. Saving is atomic (temp file + replace) with a one-slot backup,
because this file holds the only copy of the passwords.
"""

import collections
import json
import os
import re
from urllib.parse import urlsplit

# Keys the launcher reads. Anything else in an entry (comments, notes) is
# preserved as-is when a row round-trips through the editor.
KNOWN_KEYS = ("env", "class", "login", "password", "run-tests", "tests")


class UsersFileError(Exception):
    pass


class UserRow:
    """One config entry, plus whatever unknown keys came with it."""

    def __init__(self, env="", cls="", login="", password="", tests=(), extra=None):
        self.env = env
        self.cls = cls
        self.login = login
        self.password = password
        self.tests = list(tests)
        self.extra = dict(extra or {})

    @classmethod
    def from_entry(cls, entry):
        if not isinstance(entry, dict):
            raise UsersFileError("every entry must be a JSON object, got %r" % (entry,))
        # "prefix" is the retired name for "env"; the launcher still reads it.
        env = entry.get("env", entry.get("prefix", "")) or ""
        field = "run-tests" if "run-tests" in entry else "tests"
        tests = parse_tests(entry.get(field))
        extra = {k: v for k, v in entry.items()
                 if k not in KNOWN_KEYS and k != "prefix"}
        return cls(env=env, cls=entry.get("class", "") or "",
                   login=entry.get("login", "") or "",
                   password=entry.get("password", "") or "",
                   tests=tests, extra=extra)

    def to_entry(self):
        entry = dict(self.extra)
        entry["env"] = self.env
        entry["class"] = self.cls
        entry["login"] = self.login
        entry["password"] = self.password
        if self.tests:
            entry["run-tests"] = list(self.tests)
        return entry

    def tests_text(self):
        return ", ".join(self.tests)

    def copy(self):
        return UserRow(self.env, self.cls, self.login, self.password,
                       list(self.tests), dict(self.extra))


def parse_tests(value):
    """Normalize a ``run-tests`` value the way the launcher does."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise UsersFileError("'run-tests' must be a list or a comma-separated string")
    ids = []
    for item in value:
        if not isinstance(item, str):
            raise UsersFileError("'run-tests' entries must be scenario ids")
        ids.extend(part.strip() for part in item.split(",") if part.strip())
    return ids


def load(path):
    """Read ``users.json`` into rows. Missing file = no rows (not an error)."""
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except ValueError as exc:
        raise UsersFileError("%s is not valid JSON: %s" % (path, exc))
    except OSError as exc:
        raise UsersFileError("cannot read %s: %s" % (path, exc))
    if not isinstance(data, list):
        raise UsersFileError("%s must be a JSON array of users." % path)
    return [UserRow.from_entry(entry) for entry in data]


def validate(rows):
    """Every problem the launcher would exit on, as a list of messages.

    Returned rather than raised: the editor shows them all at once instead of
    stopping at the first, and a row can be half-typed without an exception.
    """
    problems = []
    seen = {}
    for index, row in enumerate(rows, start=1):
        where = "row %d" % index
        if row.login:
            where += " (%s)" % row.login
        for field, value in (("class", row.cls), ("login", row.login),
                             ("password", row.password)):
            if not str(value).strip():
                problems.append("%s: %s is required." % (where, field))
        for test in row.tests:
            # The launcher exits on this one: the selector is singular, and
            # "tags:x" silently becomes a scenario id that does not exist.
            if test.startswith("tags:"):
                problems.append("%s: run-tests has %r; the selector is singular - "
                                "use 'tag:%s'." % (where, test, test[len("tags:"):]))
        key = (row.env, row.login)
        if row.login and key in seen and not row.env:
            problems.append("%s: %s is already in the list, without an environment, "
                            "in row %d - give one of them an environment, or another "
                            "login." % (where, row.login, seen[key]))
        elif row.login and key in seen:
            problems.append("%s: env+login duplicates row %d - that pair names the "
                            "profile folder and must be unique." % (where, seen[key]))
        elif row.login:
            seen[key] = index
    problems.extend(env_problems([row.env for row in rows]))
    return problems


def _split_env(value):
    """session_launcher.split_env_value: a bare host:port parsed as one."""
    return urlsplit(value if "://" in value else "//" + value)


def _env_alias(value):
    """session_launcher.env_alias, mirrored: the GUI does not import the core."""
    split = _split_env(value)
    host = (split.hostname or value.strip()).lower()
    if ":" in host or re.fullmatch(r"[0-9.]+", host):
        return host
    return host.split(".")[0]


def env_problems(values):
    """What in these ``env`` values would stop the launcher reading the file.

    Each is a crash in the core rather than a message, and it takes every launch
    and the environment list down with it, so it is caught here before the file
    is written: a ``{{...}}`` placeholder or an unreadable port (env_origin
    raises on the port), and the same server spelled two ways
    (build_environments asserts every environment ends up with its own name -
    ``localhost:8069`` and ``http://localhost:8069/`` cannot). The naming
    mirrors session_launcher.build_environments step for step.
    """
    problems = []
    distinct = []
    for value in values:
        if value and value not in distinct:
            distinct.append(value)
    readable = []
    for value in distinct:
        if "{{" in value or "}}" in value:
            problems.append("env %r has a {{...}} placeholder, which the launcher "
                            "cannot read. Put variables in the environment's "
                            "address instead." % value)
            continue
        try:
            _split_env(value).port
        except ValueError:
            problems.append("env %r has a port the launcher cannot read." % value)
            continue
        readable.append(value)
    names = {value: _env_alias(value) for value in readable}
    groups = {}
    for value, alias in names.items():
        groups.setdefault(alias, []).append(value)
    for alias, group in groups.items():
        if len(group) < 2:
            continue
        for value in group:
            netloc = _split_env(value).netloc.lower()
            names[value] = netloc if netloc and netloc != alias else value
    by_name = {}
    for value, name in names.items():
        by_name.setdefault(name, []).append(value)
    for name, group in by_name.items():
        if len(group) > 1:
            problems.append("env %s are the same server written %d ways; the "
                            "launcher cannot tell them apart. Write them the same."
                            % (" and ".join(repr(v) for v in group), len(group)))
    return problems


def save(path, rows):
    """Write the rows back, atomically, keeping one backup of what was there.

    The file holds the only copy of every password, so it is written to a temp
    file in the same directory and then moved into place: an interrupted save
    leaves the previous file intact rather than a truncated one.
    """
    problems = validate(rows)
    if problems:
        raise UsersFileError("\n".join(problems))
    if not path:
        raise UsersFileError("No config path configured.")
    payload = [row.to_entry() for row in rows]
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    temp = os.path.join(directory, ".%s.tmp" % os.path.basename(path))
    try:
        with open(temp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        if os.path.exists(path):
            backup = path + ".bak"
            try:
                if os.path.exists(backup):
                    os.remove(backup)
                os.replace(path, backup)
            except OSError:
                pass          # a missing backup must not block the save itself
        os.replace(temp, path)
    except OSError as exc:
        raise UsersFileError("cannot write %s: %s" % (path, exc))
    finally:
        if os.path.exists(temp):
            try:
                os.remove(temp)
            except OSError:
                pass
    return path


def fingerprint(path):
    """(mtime, size) - enough to notice the file changed underneath the editor."""
    try:
        stat = os.stat(path)
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


# -- people and where they sign in --------------------------------------------
class Credential(collections.namedtuple("Credential", "role login password indices")):
    """One person: a login, its password and role, and the rows that hold them."""

    @property
    def key(self):
        """What tells this person from another on the same list of rows."""
        return (self.login, self.password, self.role) if self.login \
            else ("", self.indices[0], "")

    def envs(self, rows):
        """The account keys of the environments this person is in, in file order."""
        return [rows[index].env for index in self.indices if rows[index].env]


def credentials(rows):
    """Every person on ``rows``, in the order they first appear.

    Rows with the same login, password and class are one person in several
    environments. A row with no login yet is a person of its own - it is being
    typed - rather than one with every other blank row.
    """
    found = collections.OrderedDict()
    for index, row in enumerate(rows):
        key = (row.login, row.password, row.cls) if row.login else ("", index, "")
        found.setdefault(key, []).append(index)
    return [Credential(rows[indices[0]].cls, rows[indices[0]].login,
                       rows[indices[0]].password, tuple(indices))
            for indices in found.values()]


def credential(rows, key):
    """The person ``key`` names on ``rows``, or None."""
    for person in credentials(rows):
        if person.key == key:
            return person
    return None


def _person(rows, key):
    person = credential(rows, key)
    if person is None:
        raise UsersFileError("%s is no longer in users.json." % (key[0] or "That user"))
    return person


def members(rows, env):
    """``[(index, row)]`` of the accounts filed under ``env``, in file order."""
    return [(index, row) for index, row in enumerate(rows) if env and row.env == env]


def assign(rows, key, env, tests=()):
    """Put a person in ``env``; nothing happens if they are there already.

    A person with no environment takes this one on their row; anyone else gets
    a row of their own here, last among the accounts of the file, so they open
    after the ones already there.
    """
    person = _person(rows, key)
    if env in person.envs(rows):
        return False
    clash = [row for _index, row in members(rows, env) if row.login == person.login]
    if clash:
        raise UsersFileError("%s is already in this environment with another password "
                             "or role." % person.login)
    loose = [index for index in person.indices if not rows[index].env]
    if loose:
        rows[loose[0]].env = env
        rows[loose[0]].tests = list(tests)
        return True
    row = rows[person.indices[0]].copy()
    row.env, row.tests = env, list(tests)
    rows.append(row)
    return True


def unassign(rows, key, env):
    """Take a person out of ``env``. The last place they were in leaves them
    in the list without an environment rather than out of it."""
    person = _person(rows, key)
    here = [index for index in person.indices if rows[index].env == env]
    if not here:
        return False
    if len(here) == len([index for index in person.indices if rows[index].env]):
        keep = here.pop(0)
        rows[keep].env, rows[keep].tests = "", []
    for index in sorted(here, reverse=True):
        del rows[index]
    return True


def update_credential(rows, key, role=None, login=None, password=None):
    """Change a person's role, login or password on every row of theirs."""
    person = _person(rows, key)
    for index in person.indices:
        row = rows[index]
        if role is not None:
            row.cls = role
        if login is not None:
            row.login = login
        if password is not None:
            row.password = password
    return True


def remove_credential(rows, key):
    """Take a person out of the file, from every environment."""
    person = _person(rows, key)
    for index in sorted(person.indices, reverse=True):
        del rows[index]
    return True


def set_tests(rows, env, login, tests):
    """The scenarios ``login`` runs in ``env``."""
    for _index, row in members(rows, env):
        if row.login == login:
            row.tests = list(tests)
            return True
    raise UsersFileError("%s is not in this environment." % login)


def copy_env(rows, source, target):
    """Everyone in ``source``, with their scenarios, also in ``target``."""
    for _index, row in members(rows, source):
        clone = row.copy()
        clone.env = target
        rows.append(clone)
    return True


def drop_env(rows, env):
    """Everyone out of ``env``: whoever was only there stays, without one."""
    for person in [person for person in credentials(rows) if env in person.envs(rows)]:
        unassign(rows, person.key, env)
    return True


def change(path, apply):
    """Read users.json afresh, ``apply(rows)``, and save it if it is still valid.

    Returns what ``apply`` returned. Raises UsersFileError - with the file
    untouched - when it cannot be read, or the result is one the launcher would
    refuse.
    """
    if not path:
        raise UsersFileError("There is no users.json to write to: set its path in "
                             "Settings -> Config.")
    rows = load(path)
    result = apply(rows)
    save(path, rows)
    return result
