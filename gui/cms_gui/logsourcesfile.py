"""Reading, validating and writing ``logsources.json`` - the core's own log config.

The schema is the launcher's, unchanged: an object with ``connections`` (where to
run a reader: locally or over ssh) and ``logs`` (what to read, for which
environments). The GUI is a form over that file and nothing more, so the CLI keeps
working on it untouched.

Validation deliberately mirrors ``engine.serverlog.parse_config``: the GUI must
never write a file the launcher would then refuse to read. It is *returned* rather
than raised, though, so the editor can show every problem at once and let a row sit
half-typed - which is the one place this differs from the engine's own loader.

Saving is atomic (temp file + replace) with a one-slot backup, the same as
``usersfile``: an http log's ``headers`` can carry an access token, so an
interrupted write must leave the previous file intact rather than a truncated one.
"""

import datetime
import json
import os
import re

from . import core

# Keys the launcher reads, plus "added" - which it does not, and never will:
# engine.serverlog sweeps every key it does not know into `extra` and ignores it.
# It is here so a row that carries one keeps it as a field rather than as an
# unknown, because the editor sorts on it. Anything else in an entry (comments,
# notes) is preserved as-is when a row round-trips through the editor.
CONNECTION_KEYS = ("name", "type", "host", "user", "identity", "port", "options",
                   "added")
LOG_KEYS = ("name", "connection", "envs", "env", "type", "path", "container",
            "unit", "url", "headers", "format", "timestamp", "level", "tz",
            "default", "project", "added")

CONNECTION_TYPES = ("local", "ssh")
LOG_TYPES = ("file", "docker", "journal", "http")

# Mirrors engine.serverlog: presets named after the SHAPE of a line, plus the
# application names people actually look for. Nothing here is coupled to one
# backend - a shape no preset describes is served by CUSTOM_FORMAT below.
FORMAT_SHAPES = ("iso", "slash", "clf", "syslog", "none")
FORMAT_ALIASES = {
    "python": "iso", "odoo": "iso", "django": "iso", "flask": "iso",
    "fastapi": "iso", "uvicorn": "iso", "celery": "iso", "gunicorn": "iso",
    "rails": "iso", "node": "iso", "pino": "iso", "winston": "iso",
    "dotnet": "iso", "spring": "iso", "docker": "iso",
    "nginx": "slash", "go": "slash",
    "apache": "clf", "access": "clf",
    "journal": "syslog", "systemd": "syslog", "rsyslog": "syslog",
    "raw": "none", "plain": "none",
}
FORMATS = tuple(FORMAT_SHAPES) + tuple(sorted(FORMAT_ALIASES))

#: What a log's format reads as in the editor when it carries its own regexes
#: rather than naming a preset. Never written to the file - `timestamp`/`level`
#: are what the launcher reads, and their presence is what makes it custom.
CUSTOM_FORMAT = "custom"

#: A one-line example of each shape, so the form can say what it is asking for.
FORMAT_EXAMPLES = {
    "iso": "2026-08-19 10:53:09,123  /  2026-08-19T10:53:09.123Z",
    "slash": "2026/08/19 10:53:09 [error] ...",
    "clf": '... [19/Aug/2026:10:53:09 +0300] "GET / HTTP/1.1" 500',
    "syslog": "Aug 19 10:53:09 host app[42]: ...",
    "none": "(no timestamp in the line - each line is stamped as it arrives)",
}

#: What each log type needs filled in, and what to call it in the form.
TARGET_FIELD = {"file": "path", "docker": "container", "journal": "unit",
                "http": "url"}


class LogSourcesFileError(Exception):
    pass


def stamp():
    """When a row was added, as it is written down: ``2026-08-27T14:03:11``.

    Seconds and no offset, because this is only ever read back to put the newest
    row on top - and a string that sorts as a string is what makes that a sort
    rather than a parse. It serves ``services.json`` as well as this file: the two
    are edited on one page, which shows rows from both, so they have to agree
    about the clock down to the format.
    """
    return datetime.datetime.now().replace(microsecond=0).isoformat()


class ConnectionRow:
    """One server, plus whatever unknown keys came with it."""

    def __init__(self, name="", type="local", host="", user="", identity="",
                 port="", options=(), extra=None, added=""):
        self.name = name
        self.type = type or "local"
        # When this row was created, or "" for one written before the editor
        # stamped them. It decides where the row sits on the page and nothing
        # else - see the sort in cms_gui.pages.services.
        self.added = added or ""
        self.host = host
        self.user = user
        self.identity = identity
        self.port = port
        self.options = list(options)
        self.extra = dict(extra or {})

    @classmethod
    def from_entry(cls, entry):
        if not isinstance(entry, dict):
            raise LogSourcesFileError("every connection must be a JSON object, "
                                      "got %r" % (entry,))
        options = entry.get("options", [])
        if isinstance(options, str):
            options = [options]
        return cls(name=entry.get("name", "") or "",
                   type=entry.get("type", "local") or "local",
                   host=entry.get("host", "") or "",
                   user=entry.get("user", "") or "",
                   identity=entry.get("identity", "") or "",
                   port=entry.get("port") or "",
                   options=list(options),
                   added=entry.get("added", "") or "",
                   extra={k: v for k, v in entry.items() if k not in CONNECTION_KEYS})

    def to_entry(self):
        entry = dict(self.extra)
        entry["name"] = self.name
        entry["type"] = self.type
        # Only when the row has one: a file written before this existed should
        # not grow a key it never had just for being opened.
        if self.added:
            entry["added"] = self.added
        # Only what this type actually uses: a local connection carrying a host
        # field reads as if it might connect somewhere, and it never will.
        if self.type == "ssh":
            entry["host"] = self.host
            if self.user:
                entry["user"] = self.user
            if self.identity:
                entry["identity"] = self.identity
            if str(self.port).strip():
                entry["port"] = int(self.port)
            if self.options:
                entry["options"] = list(self.options)
        return entry

    def describe(self):
        if self.type == "local":
            return "this machine"
        target = "%s@%s" % (self.user, self.host) if self.user else self.host
        return "ssh %s" % (target or "?")

    def copy(self):
        return ConnectionRow(self.name, self.type, self.host, self.user,
                             self.identity, self.port, list(self.options),
                             dict(self.extra), self.added)


class LogRow:
    """One log stream, plus whatever unknown keys came with it."""

    def __init__(self, name="", connection="", envs=(), type="file", target="",
                 format="iso", default=False, headers=None, timestamp=None,
                 level=None, tz="", project="", extra=None, added=""):
        self.name = name
        self.connection = connection
        #: When this row was created; "" for one that predates the stamp. Only
        #: the page's own ordering reads it.
        self.added = added or ""
        self.envs = list(envs)
        self.type = type or "file"
        # One field in the form, because a log has exactly one of path /
        # container / unit / url and which one is decided by its type.
        self.target = target
        self.format = format or "iso"
        self.default = bool(default)
        self.headers = dict(headers or {})
        # The escape hatch for a backend no preset describes. These MUST survive a
        # round trip: they used to be dropped on save, which meant opening the
        # editor once silently destroyed a hand-written custom format.
        self.timestamp = dict(timestamp) if timestamp else None
        self.level = dict(level) if level else None
        # Which clock the timestamps were written by. Separate from the format,
        # because the shape of a line and the machine's timezone are two different
        # questions and only the second changes per deployment - a backend logging
        # UTC on a machine that is not is the commonest reason nothing matches.
        self.tz = tz or ""
        # Which project this log belongs to on the Services & Logs page. The
        # launcher has never heard of it and does not need to: engine.serverlog
        # sweeps every key it does not know into LogSource.extra and ignores it,
        # so a log grouped here is still exactly the log --server-log reads.
        # Blank is not a gap to be filled - a log that belongs to no project is a
        # perfectly ordinary log, and shows under "Unassigned".
        self.project = project or ""
        self.extra = dict(extra or {})

    @property
    def is_custom(self):
        return bool(self.timestamp or self.level)

    def format_label(self):
        """What to show in the format column: the preset, or that it is custom."""
        return CUSTOM_FORMAT if self.is_custom else self.format

    @classmethod
    def from_entry(cls, entry):
        if not isinstance(entry, dict):
            raise LogSourcesFileError("every log must be a JSON object, got %r"
                                      % (entry,))
        envs = entry.get("envs", entry.get("env", []))
        if isinstance(envs, str):
            envs = [envs]
        kind = entry.get("type", "file") or "file"
        return cls(name=entry.get("name", "") or "",
                   connection=entry.get("connection", "") or "",
                   envs=[e for e in envs if isinstance(e, str)],
                   type=kind,
                   target=entry.get(TARGET_FIELD.get(kind, "path"), "") or "",
                   format=entry.get("format", "iso") or "iso",
                   default=bool(entry.get("default")),
                   headers=entry.get("headers") or {},
                   timestamp=entry.get("timestamp"),
                   level=entry.get("level"),
                   tz=entry.get("tz", "") or "",
                   project=entry.get("project", "") or "",
                   added=entry.get("added", "") or "",
                   extra={k: v for k, v in entry.items() if k not in LOG_KEYS})

    def to_entry(self):
        entry = dict(self.extra)
        entry["name"] = self.name
        entry["connection"] = self.connection
        entry["envs"] = list(self.envs)
        entry["type"] = self.type
        entry[TARGET_FIELD.get(self.type, "path")] = self.target
        entry["format"] = self.format
        if self.default:
            entry["default"] = True
        if self.headers:
            entry["headers"] = dict(self.headers)
        # Written back verbatim. Dropping these on save is how a custom format
        # gets destroyed by opening the editor.
        if self.timestamp:
            entry["timestamp"] = dict(self.timestamp)
        if self.level:
            entry["level"] = dict(self.level)
        if self.tz and self.tz != "local":
            entry["tz"] = self.tz
        # Only when set: an unassigned log should read the same in the file as it
        # did before this page existed.
        if self.project:
            entry["project"] = self.project
        if self.added:
            entry["added"] = self.added
        return entry

    def envs_text(self):
        return ", ".join(self.envs)

    def copy(self):
        return LogRow(self.name, self.connection, list(self.envs), self.type,
                      self.target, self.format, self.default, dict(self.headers),
                      dict(self.timestamp) if self.timestamp else None,
                      dict(self.level) if self.level else None,
                      self.tz, self.project, dict(self.extra), self.added)


def load(path):
    """Read ``logsources.json`` into rows. Missing file = nothing (not an error)."""
    if not path or not os.path.exists(path):
        return [], []
    try:
        # utf-8-sig: a Windows shell writes a byte-order mark, and strict utf-8
        # then reads it as content - the trap that once lost saved Launch
        # configurations (see the CHANGELOG entry about a configuration that
        # could vanish).
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except ValueError as exc:
        raise LogSourcesFileError("%s is not valid JSON: %s" % (path, exc))
    except OSError as exc:
        raise LogSourcesFileError("cannot read %s: %s" % (path, exc))
    if not isinstance(data, dict):
        raise LogSourcesFileError("%s must be a JSON object with 'connections' "
                                  "and 'logs'." % path)
    connections = data.get("connections", [])
    logs = data.get("logs", [])
    if not isinstance(connections, list) or not isinstance(logs, list):
        raise LogSourcesFileError("%s: 'connections' and 'logs' must both be "
                                  "JSON arrays." % path)
    return ([ConnectionRow.from_entry(e) for e in connections],
            [LogRow.from_entry(e) for e in logs])


def validate(connections, logs):
    """Every problem the launcher would refuse the file for, as messages.

    Returned rather than raised, so the editor shows them all at once instead of
    stopping at the first and a row can be half-typed without an exception.
    """
    problems = []
    seen_connections = {}
    for index, row in enumerate(connections, start=1):
        where = "connection %d" % index
        if row.name:
            where += " (%s)" % row.name
        if not row.name.strip():
            problems.append("%s: a name is required." % where)
        if row.type not in CONNECTION_TYPES:
            problems.append("%s: unknown type %r. Known: %s."
                            % (where, row.type, ", ".join(CONNECTION_TYPES)))
        if row.type == "ssh" and not row.host.strip():
            problems.append("%s: an ssh connection needs a host." % where)
        if str(row.port).strip():
            try:
                int(row.port)
            except (TypeError, ValueError):
                problems.append("%s: port must be a number." % where)
        if row.name and row.name in seen_connections:
            problems.append("%s: duplicates connection %d - names must be unique."
                            % (where, seen_connections[row.name]))
        elif row.name:
            seen_connections[row.name] = index

    seen_logs = {}
    for index, row in enumerate(logs, start=1):
        where = "log %d" % index
        if row.name:
            where += " (%s)" % row.name
        if not row.name.strip():
            problems.append("%s: a name is required." % where)
        if not row.connection.strip():
            problems.append("%s: pick a connection." % where)
        elif row.connection not in seen_connections:
            problems.append("%s: no connection named %r. Defined: %s."
                            % (where, row.connection,
                               ", ".join(sorted(seen_connections)) or "none"))
        if row.type not in LOG_TYPES:
            problems.append("%s: unknown type %r. Known: %s."
                            % (where, row.type, ", ".join(LOG_TYPES)))
        if not row.envs:
            problems.append("%s: name at least one environment (the same string "
                            "users.json uses)." % where)
        if row.type in ("file", "docker", "http") and not str(row.target).strip():
            problems.append("%s: %s is required." % (where, TARGET_FIELD[row.type]))
        if not row.is_custom and row.format not in FORMATS:
            problems.append("%s: unknown format %r. Known: %s. Any other backend "
                            "works by giving your own timestamp/level patterns."
                            % (where, row.format, ", ".join(FORMATS)))
        # A custom pattern that will not compile is not a typo the launcher can
        # survive: LineParser raises on it, and the log never starts.
        for field, spec in (("timestamp", row.timestamp), ("level", row.level)):
            if not spec:
                continue
            pattern = spec.get("regex", "")
            if not pattern:
                problems.append("%s: custom %s needs a regex." % (where, field))
                continue
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                problems.append("%s: custom %s regex does not compile (%s)."
                                % (where, field, exc))
                continue
            if compiled.groups < 1:
                # timestamp_of/level_of read group(1); a pattern with no group
                # would hand the whole match to strptime.
                problems.append("%s: custom %s regex must capture the value in a "
                                "group, e.g. (\\d{4}-\\d{2}-\\d{2}...)."
                                % (where, field))
        for env in row.envs:
            # The rule that makes --server-log=NAME resolvable. The same name on
            # another environment is fine, and is how "app" exists everywhere.
            key = (env, row.name)
            if row.name and key in seen_logs:
                problems.append("%s: %r is already defined for %r in log %d - names "
                                "must be unique per environment."
                                % (where, row.name, env, seen_logs[key]))
            elif row.name:
                seen_logs[key] = index
    return problems


def save(path, connections, logs):
    """Write the rows back, atomically, keeping one backup of what was there."""
    problems = validate(connections, logs)
    if problems:
        raise LogSourcesFileError("\n".join(problems))
    if not path:
        raise LogSourcesFileError("No log sources path configured.")
    payload = {"connections": [row.to_entry() for row in connections],
               "logs": [row.to_entry() for row in logs]}
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
        raise LogSourcesFileError("cannot write %s: %s" % (path, exc))
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


#: The user's own directory, the same one ``services.json`` defaults into.
USER_DIR_NAME = core.USER_DIR_NAME


def default_path():
    """Where ``logsources.json`` goes when nobody has said otherwise.

    Under the user's own directory rather than wherever the launcher happens to
    resolve its config to. From a source checkout that is the checkout itself,
    which is how this file ended up needing a ``.gitignore`` entry to stay out of
    somebody's commit - the same reason ``services.json`` moved.

    Unlike that one, this file is *not* the GUI's alone: ``--server-log`` reads
    it. So wherever it ends up, the path has to travel with every call the GUI
    makes into the core (``--log-sources``), or the file being edited and the
    file being read come apart.
    """
    return os.path.join(core.home_folder(), "logsources.json")


def resolve_path(configured, reported=""):
    """(path to read, path to write). They differ only while migrating.

    Settings wins; otherwise the default. ``reported`` is what ``--describe``
    said the core resolved to - read when there is nothing at the new location,
    so an upgrade does not look like having lost the file. The next Save writes
    the new one; the old is left alone rather than deleted, because it is still
    somebody's file and nothing here has to destroy it to be correct.
    """
    target = os.path.expanduser((configured or "").strip() or default_path())
    if os.path.exists(target):
        return target, target
    old = os.path.expanduser((reported or "").strip())
    if old and os.path.abspath(old) != os.path.abspath(target) \
            and os.path.exists(old):
        return old, target
    return target, target
