#!/usr/bin/env python3
"""Open one isolated Chrome window per test user.

Each --user-data-dir is a separate Chrome profile, so every user can be logged
in at the same time. A tiny generated extension auto-fills and submits the
login form per profile, using that user's own password (once per tab, so wrong
credentials are never resubmitted in a loop).

The user list lives in a JSON config file (users.json next to this script by
default; override with --config=FILE). Each entry carries its own login, window
class and password, plus an optional env (see below). Run --init-users-json
once to scaffold a starter config (it won't overwrite an existing one). Launch a subset
with --env=NAME and/or --filter-users=login1,login2 (defaults = everything).
To launch a single user without editing the config, pass --user=LOGIN
(with --password=PASS when the config does not already know that login).

The "env" field is the environment a user belongs to - a URL or host:port, e.g.
"localhost:8069" or "https://app-dev.example.com/". --env selects one by short
name (--env=dev matches "app-dev") and supplies that environment's URL, so --url
only has to be typed to override it. Everything --env implies is a default: any
flag you pass explicitly wins over it.

Each user's profile folder is "<env>-<login>", so the env+login pair must be
unique (the same login can repeat under different envs - separate environments).
--user-session overrides the folder's prefix for the whole run.

By default the script stays in the foreground and holds a reference to every
launched window; press CTRL+C to close all of them at once. Pass --detach to
fire-and-forget instead (windows survive the script exiting, old behavior).

Profiles + generated extensions live under a sessions dir (user_sessions next to
this script by default; override with --sessions-dir=DIR).

Pass --user-session=PREFIX to override every profile folder's prefix under the
sessions dir. This keeps separate sessions from sharing logins. The per-user
login is appended automatically.

The URL can be passed as --url=URL or as a positional argument.

Each profile also gets that user's single login/password saved in Chrome's own
password manager, encrypted with this platform's Chrome key: on Linux the windows
run with --password-store=basic so cookies and saved passwords share one
deterministic key, while on macOS the key comes from the login Keychain ("Chrome
Safe Storage") - the first run there pops a Keychain prompt, choose Always Allow.

The auto-login helper is installed straight into each profile (files under
Default/Extensions plus a Preferences entry) rather than via --load-extension, which
recent Chrome (137+) blocks. By default every usable extension vendored in
extensions/ is installed the same way (a broken one is skipped with a warning, never
stopping the launch). --extensions overrides that: "none" for nothing, or a
comma-separated list of local names, Chrome Web Store names/ids, or name=id.
--extensions=list shows what is available.

Usage: python session_launcher.py [--detach] [--env=NAME] [--user-session=PREFIX]
           [--sessions-dir=DIR] [--config=FILE] [--extensions=LIST]
           [--user=LOGIN --password=PASS] (--env=NAME | --url=URL | <URL>)
   e.g. python session_launcher.py --env=dev
        python session_launcher.py --url=http://localhost:8069/web/login
"""
import atexit
import base64
import collections
import ctypes
import ctypes.util
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

import runtime_paths

log = logging.getLogger("session_launcher")

# engine.events once --events is given, else None. The module is imported lazily
# (a plain launch must not pay for the engine package), but the window-lifecycle
# emitters below are module-level functions, so they reach it through here.
_events = None


def _emit(kind, **fields):
    """Emit a launcher lifecycle event; a no-op unless --events was given."""
    if _events is not None:
        _events.emit(kind, **fields)


def _emit_server_lines(session, log_name, lines):
    """Announce one batch of backend log lines belonging to one window.

    Batched rather than one event per line: a busy backend writes faster than a
    front-end can parse single JSON objects off a pipe, and the batch is what the
    hub already collects. engine.serverlog caps a batch's size; the ring buffer it
    keeps for the report artifact is not capped the same way, so a burst is trimmed
    on screen and still lands whole on disk.
    """
    _emit("serverlog.lines", session=session, log=log_name,
          lines=[{"ts": round(entry.ts, 3), "level": entry.level, "text": entry.text}
                 for entry in lines])


# Where things live. In a source checkout every one of these is the checkout
# itself, so the project stays self-contained and movable; in an installed build
# the read-only resources come out of the bundle and everything writable moves to
# ~/QAVector, so an upgrade cannot touch it. See runtime_paths.
# The sessions dir and the user config file can be overridden on the command line.
SCRIPT_DIR = runtime_paths.app_root()


def version():
    """The installed version, or "dev" when running from a source checkout.

    pyproject.toml holds the only copy; this reads it back rather than keeping a
    second one here to drift out of step. A frozen build has no installed
    distribution to look up, so it falls back to the VERSION file the build script
    writes into the bundle from that same pyproject.toml.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version as _version
        return _version("qavector")
    except Exception:
        return runtime_paths.bundled_version() or "dev (not installed)"


DEFAULT_SESSIONS_DIR = runtime_paths.sessions_dir()
DEFAULT_CONFIG = runtime_paths.config_path()
# Unpacked extensions kept in-tree: extensions/<name>/manifest.json. Installed with
# no network access at all, and editable - see extensions/README.md.
EXTENSIONS_DIR = runtime_paths.extensions_dir()

# Where a bare URL should land, and which paths count as "the login page".
#
# The launcher opens the signed-in entry point rather than the login form, so an
# existing session is reused; with no session the app redirects to its own login
# page, where the auto-login extension takes over. The defaults suit apps that serve
# their client from /web - change these two for an app that uses other paths.
LANDING_PATH = "/web"
LOGIN_PATHS = ("", "/", "/web/login", "/web/login/")

# Starter config written by --init-users-json: one example user with an inline
# _comment doc (the loader ignores keys it doesn't use). The config holds
# passwords and is git-ignored, so this scaffolds a fresh copy to edit.
#
# Built with json.dumps rather than written out as a string: this file must load
# as-is, and hand-quoted JSON is exactly how it stopped doing so before. Note
# "run-tests" is OMITTED, not written empty - an empty list is rejected, on the
# grounds that a field meaning nothing should not be there. See
# test_scaffolded_config_loads, which scaffolds and then loads.
_USERS_TEMPLATE_ENTRY = {
    "_comment": (
        "one object per user - env=the environment this user belongs to (a URL or "
        "host:port; --env selects it, and it also names the profile folder), "
        "class=window label, login=username, password=that user's password. Add an "
        "optional \"run-tests\": [\"scenario_id\"] to give this user its own scenarios "
        "under --run-tests=config. The env+login pair names the folder and must be "
        "unique. Copy this line per user."
    ),
    "env": "localhost:8069",
    "class": "User 1",
    "login": "login1",
    "password": "secret1",
}
DEFAULT_USERS_TEMPLATE = json.dumps([_USERS_TEMPLATE_ENTRY], indent=2) + "\n"


def init_users_json(config_path):
    """Write a starter users.json to config_path, then exit.

    Creates the file only when it does not already exist, so real credentials
    are never overwritten.
    """
    if os.path.exists(config_path):
        sys.exit("%s already exists; not overwriting." % config_path)
    with open(config_path, "w", encoding="utf-8") as fh:
        fh.write(DEFAULT_USERS_TEMPLATE)
    sys.exit("Wrote starter config to %s - edit it, then run again." % config_path)


# One configured user. `tests` is that user's own scenario ids, run by
# --run-tests=config so a role does not have to be paired with its test by hand.
User = collections.namedtuple("User", "env cls login password tests")
User.__new__.__defaults__ = ((),)   # tests is optional

# Config paths already nudged about the old "prefix" field name (once per run).
_WARNED_LEGACY = set()


def _parse_tests_field(value, where, field):
    """Normalize a config "run-tests" value to a tuple of scenario ids.

    Accepts a JSON list or a comma-separated string, and splits commas inside list
    items too, so every spelling below means the same thing - matching how
    --run-tests reads on the command line:

        "run-tests": ["access_agent", "tag:repro"]
        "run-tests": "access_agent,tag:repro"
        "run-tests": ["access_agent,tag:repro"]
    """
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        sys.exit("%s: %r must be a list or a comma-separated string, got %r"
                 % (where, field, value))
    ids = []
    for item in value:
        if not isinstance(item, str):
            sys.exit("%s: %r entries must be scenario ids, got %r" % (where, field, item))
        for part in item.split(","):
            part = part.strip()
            if not part:
                continue
            # The engine's selector is `tag:NAME`, one tag per entry - "tags:" (or
            # several tags in one entry) silently becomes a scenario id that does
            # not exist, and the run fails much later with "flow not found".
            if part.startswith("tags:"):
                sys.exit("%s: %r has %r; the tag selector is singular - use %r, one "
                         "tag per entry." % (where, field, part, "tag:" + part[len("tags:"):]))
            ids.append(part)
    if not ids:
        sys.exit("%s: %r is empty; drop the field instead." % (where, field))
    return tuple(ids)


def _read_config(config_path):
    """Read and parse the users JSON file; raises OSError/ValueError on failure."""
    with open(config_path, encoding="utf-8") as fh:
        return json.load(fh)


def load_users(config_path):
    """Load the user list from a JSON config file.

    The file is a JSON array of objects, each with class (window class / profile
    label), login and password, plus an optional env - the environment the user
    belongs to (--env selects it; --user-session overrides the folder name it
    produces). Every user carries its own password - there is no shared default.
    The profile folder is <env>-<login>, so that pair must be unique. Returns a
    list of (env, class, login, password) tuples.

    "prefix" is still accepted as the old name for "env".
    """
    try:
        data = _read_config(config_path)
    except (OSError, ValueError) as exc:
        sys.exit("Could not read config %s: %s" % (config_path, exc))
    if not isinstance(data, list) or not data:
        sys.exit("Config %s must be a non-empty JSON array of users." % config_path)
    users = []
    seen = set()
    legacy = False
    for i, entry in enumerate(data):
        try:
            env = entry.get("env", entry.get("prefix", ""))  # optional
            login = entry["login"]
            # "run-tests" mirrors the CLI flag; "tests" is accepted as a shorthand.
            field = "run-tests" if "run-tests" in entry else "tests"
            tests = _parse_tests_field(entry.get(field), "Config entry %d" % i, field)
            record = User(env, entry["class"], login, entry["password"], tests)
        except (KeyError, TypeError, AttributeError):
            sys.exit("Config entry %d must have class, login and password: %r" % (i, entry))
        # Both spellings on one entry is a typo we must not paper over: picking
        # either one silently files the profile under the wrong folder.
        if "env" in entry and "prefix" in entry and entry["env"] != entry["prefix"]:
            sys.exit("Config entry %d has both 'env' (%r) and the old 'prefix' (%r); "
                     "keep only 'env'." % (i, entry["env"], entry["prefix"]))
        legacy = legacy or ("env" not in entry and "prefix" in entry)
        # The profile folder is <env>-<login>, so that pair must be unique. The
        # same login under different envs is fine (that is the whole point).
        if (env, login) in seen:
            sys.exit("Config has duplicate env+login %r / %r; that pair names "
                     "the profile folder and must be unique." % (env, login))
        seen.add((env, login))
        users.append(record)
    if legacy and config_path not in _WARNED_LEGACY:
        # load_users runs more than once per launch (environment list, then rows);
        # the nudge is per config file, not per read.
        _WARNED_LEGACY.add(config_path)
        log.info("%s still uses the old 'prefix' field; it now reads as 'env' - "
                 "rename it when convenient.", config_path)
    return users


def session_dir_for(session_prefix, entry_env, login):
    """Build the profile-folder name "<env>-<login>" as one safe path segment.

    --user-session (session_prefix) overrides the config's per-entry env. Path
    separators in the env (e.g. from a URL like "https://host/") are flattened so
    the result stays a single directory; ":" and the like are kept. The login is
    sanitized the same way ad-hoc --user profiles are.

    On Windows the characters NTFS rejects go too - "localhost:8069" cannot be a
    folder name there. That rule is deliberately platform-conditional: applying it
    everywhere would rename every profile folder an existing Linux install already
    has, silently orphaning its logged-in sessions.
    """
    prefix = session_prefix or entry_env
    safe_prefix = re.sub(r"[/\\]+", "_", prefix)
    if os.name == "nt":
        safe_prefix = re.sub(r'[:*?"<>|]+', "_", safe_prefix).rstrip(". ")
    safe_login = re.sub(r"[^A-Za-z0-9._-]", "_", login) or "user"
    return "%s-%s" % (safe_prefix, safe_login) if safe_prefix else safe_login


# One environment, derived from the distinct "env" values in the config: alias is
# the short name --env matches, value is the config string (which also names the
# profile folder), origin is the URL --env supplies, count is how many users use it.
Env = collections.namedtuple("Env", "alias value origin count")

# Hosts that are served over plain http when the env value carries no scheme.
_LOCAL_HOSTS = frozenset(("localhost", "127.0.0.1", "::1"))


def split_env_value(value):
    """urlsplit an env value, which may or may not carry a scheme.

    urlsplit("localhost:8069") reads "localhost" as the SCHEME and leaves netloc
    empty - so a bare host:port has to be parsed protocol-relative instead, or
    every localhost env resolves to the nonsense origin "localhost://".
    """
    return urlsplit(value if "://" in value else "//" + value)


def _is_host_like(split):
    """True when an env value looks addressable (so it can supply a URL).

    A plain label like "myprofile" is a legal env value - it names the profile
    folder - but there is no URL to derive from it.
    """
    host = split.hostname
    if not host:
        return False
    return bool(split.scheme) or split.port is not None or "." in host or host in _LOCAL_HOSTS


def env_origin(value):
    """Derive the origin an env points at ("" when the value is not host-like).

    No trailing slash, port preserved - the same shape as the origin computed from
    --url, so the two can be compared directly. Local hosts default to http, every
    other host to https.
    """
    split = split_env_value(value)
    if not _is_host_like(split):
        return ""
    scheme = split.scheme
    if not scheme:
        host = split.hostname
        scheme = "http" if host in _LOCAL_HOSTS or host.endswith(".localhost") else "https"
    return "%s://%s" % (scheme, split.netloc)


def env_alias(value):
    """Short name for an env value: the host's first DNS label, lowercased.

    "https://app-dev.example.com/" -> "app-dev", "localhost:8069" -> "localhost".
    IP literals keep the whole host, so 127.0.0.1 does not collapse to "127".
    """
    split = split_env_value(value)
    host = (split.hostname or value.strip()).lower()
    if ":" in host or re.fullmatch(r"[0-9.]+", host):
        return host  # IPv6 literal or IPv4 address - the labels are not names
    return host.split(".")[0]


def build_environments(values):
    """Turn the config's env values into a list of Env, in first-seen order.

    Aliases must be unique for --env to resolve, so any collision (two hosts
    sharing a first label, e.g. localhost:8069 and localhost:8070) falls back to
    the full netloc for that group, then to the raw value.
    """
    counts = collections.OrderedDict()
    for value in values:
        if value:
            counts[value] = counts.get(value, 0) + 1
    envs = [Env(env_alias(v), v, env_origin(v), n) for v, n in counts.items()]
    by_alias = collections.defaultdict(list)
    for env in envs:
        by_alias[env.alias].append(env)
    for alias, group in by_alias.items():
        if len(group) < 2:
            continue
        for i, env in enumerate(group):
            netloc = split_env_value(env.value).netloc.lower()
            retry = netloc if netloc and netloc != alias else env.value
            envs[envs.index(env)] = env._replace(alias=retry)
    aliases = [e.alias for e in envs]
    assert len(aliases) == len(set(aliases)), "env aliases must be unique: %r" % (aliases,)
    return envs


def environments_from_config(config_path, strict=True):
    """Build the environment list from a config file.

    strict=False returns [] instead of exiting when the config cannot be read, for
    the paths where the config is a nice-to-have (an ad-hoc --user run that was
    given its own --password) rather than the thing being asked for.
    """
    if strict:
        return build_environments([u.env for u in load_users(config_path)])
    try:
        data = _read_config(config_path)
    except (OSError, ValueError) as exc:
        log.debug("Could not read %s for environment names: %s", config_path, exc)
        return []
    if not isinstance(data, list):
        return []
    values = []
    for entry in data:
        if isinstance(entry, dict):
            values.append(entry.get("env", entry.get("prefix", "")) or "")
    return build_environments(values)


def load_log_sources(path=None):
    """The parsed logsources.json. Raises engine.serverlog.ServerLogError if broken.

    Imported here rather than at module scope: a launch without --server-log must
    not pay for the engine package, which is the same reason engine.events is
    reached through the lazy global above.
    """
    from engine import serverlog
    return serverlog.load_config(path or runtime_paths.logsources_path())


def server_log_inventory(path=None):
    """``[{name, env, connection, type, target, default}]`` for --describe.

    Flattened one row per (log, environment) pair so a front-end can filter the
    picker by the environment the user chose without knowing this file's shape. A
    config that cannot be read is not an error here - the rest of the inventory is
    still worth answering - so it comes back as an empty list plus the reason.
    """
    try:
        config = load_log_sources(path)
    except Exception as exc:   # noqa: BLE001 - reported as data, not raised
        return [], "%s: %s" % (type(exc).__name__, exc)
    rows = []
    for source in config.logs:
        for env in source.envs:
            rows.append({"name": source.name, "env": env,
                         "connection": source.connection.name,
                         "type": source.type, "target": source.describe(),
                         "default": source.default})
    return rows, ""


def format_server_logs(path=None):
    """What --server-log=list prints: every configured log, by environment."""
    path = path or runtime_paths.logsources_path()
    try:
        config = load_log_sources(path)
    except Exception as exc:   # noqa: BLE001 - this IS the error report
        return "Cannot read %s:\n  %s" % (path, exc)
    if not config.logs:
        return ("No server logs configured.\n"
                "Copy logsources.example.json to %s and describe your connections "
                "and log files there." % path)
    lines = ["Server logs from %s:" % path]
    for env in config.envs():
        lines.append("")
        lines.append("  %s" % env)
        for source in config.for_env(env):
            lines.append("    %-16s %s" % (source.name + (" *" if source.default else ""),
                                           source.describe()))
    lines.append("")
    lines.append("  * streamed by a bare --server-log. Name the others to add them")
    lines.append("    (--server-log=app,nginx), or --server-log=all for every one.")
    return "\n".join(lines)


def show_server_log(name, lines=None, path=None):
    """Read one configured log and answer what it holds, as JSON.

    Replaces the old "test this connection" probe, which could only say whether a
    follow stayed alive. Showing the log answers that and the question behind it -
    is this the right file, is anything in it, does it look the way the format
    expects - and it answers instantly instead of watching for a few seconds.

    ``lines`` is how many from the end, or None for as much as the byte budget in
    engine.serverlog allows. Never raises: the failure IS the answer.
    """
    from engine import serverlog
    try:
        config = load_log_sources(path)
    except Exception as exc:   # noqa: BLE001 - reported as data
        return {"log": name, "ok": False, "error": str(exc), "lines": []}
    matches = [s for s in config.logs if s.name == name]
    if not matches:
        return {"log": name, "ok": False, "lines": [],
                "error": "no log named %r in %s"
                         % (name, path or runtime_paths.logsources_path())}
    source = matches[0]
    try:
        found, truncated = serverlog.read_lines(source, tail=lines)
    except Exception as exc:   # noqa: BLE001 - catching this IS the point
        return {"log": name, "target": source.describe(), "ok": False,
                "error": str(exc), "lines": []}
    return {"log": name, "target": source.describe(), "ok": True, "error": "",
            "lines": found, "truncated": truncated,
            # An empty log is not a broken one - say so rather than leaving a
            # front-end to read "no lines" as a failure.
            "empty": not found}


def resolve_server_logs(env_values, requested, path=None):
    """The logs this run streams. Never fatal - reports and returns [] instead.

    A backend log is a diagnostic, and a diagnostic that stops ten windows from
    opening is worse than no diagnostic at all. A name that does not resolve, a
    logsources.json with a mistake in it, no config at all: each is worth saying
    loudly once, and none of them is a reason for the launch not to happen. That
    is the same promise engine.serverlog makes about an unreachable host, and the
    same one --extensions already keeps about an extension that will not load.
    """
    from engine import serverlog
    path = path or runtime_paths.logsources_path()
    try:
        config = load_log_sources(path)
    except serverlog.ServerLogError as exc:
        log.warning("--server-log: %s. Continuing without server logs.", exc)
        return []
    if not config.logs:
        log.warning("--server-log: no logs configured in %s. Copy "
                    "logsources.example.json there and describe your servers, or "
                    "use the GUI's Log sources page; --server-log=list shows what "
                    "is configured. Continuing without server logs.", path)
        return []
    # strict=False: a misspelled name is reported, and the logs that DID resolve
    # are still streamed. Losing the other three because one was wrong would be a
    # second surprise on top of the first.
    try:
        return serverlog.resolve(config, env_values, requested, strict=False)
    except serverlog.ServerLogError as exc:
        log.warning("--server-log: %s. Continuing without server logs.", exc)
        return []


def describe(config_path, flows_dir=None, sessions_dir=None, reports_dir=None,
             log_sources_path=None, cycles_dir=None):
    """Everything a front-end needs to populate its pickers, as one dict.

    ``--describe`` prints this as JSON and exits. It exists so a GUI never has to
    re-implement the config and flow parsing that lives here: the launcher stays
    the single source of truth for what environments, users, scenarios and
    extensions exist, and a change here reaches every front-end for free.

    Passwords are deliberately absent - only ``has_password`` says whether one is
    configured. Anything that cannot be read degrades to an empty list plus an
    entry in ``warnings``, so one broken piece never denies the caller the rest.
    """
    warnings = []
    users, envs = [], []
    try:
        rows = load_users(config_path)
    except SystemExit as exc:          # load_users reports config errors this way
        rows = []
        warnings.append(str(exc))
    for user in rows:
        users.append({"env": user.env, "class": user.cls, "login": user.login,
                      "tests": list(user.tests), "has_password": bool(user.password),
                      "profile": session_dir_for("", user.env, user.login)})
    for env in build_environments([u.env for u in rows]):
        envs.append({"alias": env.alias, "value": env.value,
                     "origin": env.origin, "count": env.count})

    scenarios, blocks, selectors = [], [], {}
    # ``flows_dir`` stays None unless it was given, so the loader resolves it the
    # way a run will - the user's own tree, then the bundled one. Pinning it to a
    # single directory here would hide every scenario the user has written and,
    # worse, make the bundled tree look writable to the editor. What gets
    # reported is where a new scenario would be written.
    reported_flows_dir = flows_dir or runtime_paths.user_flows_dir()
    try:
        # Lazy: the loader needs pyyaml, which a plain launch never installs.
        from engine import compiler, flowfile, loader
        skipped = loader._SKIP_TAGS
        for scenario_id in loader.discover_scenarios(flows_dir, include_templates=True):
            try:
                flow = loader.load_flow(scenario_id, flows_dir)
            except Exception as exc:
                warnings.append("scenario %s: %s" % (scenario_id, exc))
                continue
            tags = list(flow.tags)
            # Which tree it came from decides whether the editor may touch it:
            # anything bundled with the app is replaced on the next upgrade.
            writable = flowfile.is_writable(flow.source, flows_dir)
            row = {"id": scenario_id, "name": flow.name,
                   "description": flow.description, "tags": tags,
                   "path": flow.source,
                   "source": "user" if writable else "bundled",
                   "writable": writable,
                   # what --run-tests=all would actually run
                   "in_all": not (set(tags) & skipped)}
            # What stands between it and --no-browser: the page steps it takes,
            # its use: blocks included. [] means it can run with no browser. Left
            # out when it cannot be worked out, which a front-end must read as
            # "needs one" - a scenario nobody can vouch for is not browser-free.
            try:
                row["browser_actions"] = compiler.browser_actions(scenario_id, flows_dir)
            except Exception:  # noqa: BLE001 - the compile error is the runner's to report
                pass
            scenarios.append(row)
        # The blocks a scenario reaches through `use:`. Not runnable on their own,
        # so they are not scenarios - but an editor showing `use: access.open_app`
        # with no way to open it is showing an alias with nothing behind it.
        for block_id, path in sorted(loader.block_files(flows_dir).items()):
            try:
                flow = loader.load_flow(block_id, flows_dir)
            except Exception as exc:
                warnings.append("block %s: %s" % (block_id, exc))
                continue
            writable = flowfile.is_writable(path, flows_dir)
            blocks.append({"id": block_id, "name": flow.name,
                           "description": flow.description,
                           "tags": list(flow.tags), "path": path,
                           "source": "user" if writable else "bundled",
                           "writable": writable})
        # name -> selector, merged across trees, so the editor can say what
        # `click: menu_settings` will actually look for.
        selectors = loader.load_selectors(flows_dir)
    except ImportError as exc:
        warnings.append("scenarios unavailable (%s); install the 'flows' extra." % exc)

    log_sources, log_sources_error = server_log_inventory(log_sources_path)
    if log_sources_error:
        warnings.append("server logs unavailable (%s)" % log_sources_error)

    extensions = []
    for name, path in sorted(local_extensions().items()):
        ok, reason = validate_local_extension(path)
        extensions.append({"name": name, "kind": "local", "value": path,
                           "usable": ok, "reason": reason})
    for name, ext_id in sorted(KNOWN_EXTENSIONS.items()):
        extensions.append({"name": name, "kind": "store", "value": ext_id,
                           "usable": True, "reason": ""})

    return {
        "version": version(),
        "script": os.path.abspath(__file__),
        "config_path": config_path,
        "flows_dir": reported_flows_dir,
        "reports_dir": reports_dir or runtime_paths.reports_dir(),
        "sessions_dir": sessions_dir or DEFAULT_SESSIONS_DIR,
        # The front-end needs to be able to say "install Chrome" before the user
        # tries to launch anything, so the answer travels with the inventory.
        "chrome": describe_chrome(),
        "log_levels": ["DEBUG", "INFO", "WARNING", "ERROR"],
        # The step grammar, straight from the compiler, so a scenario editor can
        # offer exactly the actions that exist and know the shape of each one's
        # arguments without keeping its own copy of the list to drift out of step.
        "flow_actions": flow_actions(),
        # The same idea one level up: every cycle on the machine, and every
        # plugin a cycle step may name, so a front-end builds its own editor
        # from what this core actually has rather than from its own copy of the
        # list. Both degrade to empty when the engine extra is not installed.
        "cycles": cycle_inventory(cycles_dir),
        "cycle_plugins": cycle_plugins(),
        "overlay_components": list(_OVERLAY_COMPONENTS),
        "report_artifacts": list(_REPORT_ARTIFACTS),
        "report_screen_modes": list(_REPORT_SCREEN_MODES),
        # One row per (log, environment) pair, so a front-end can filter its
        # --server-log picker by the environment chosen without parsing
        # logsources.json itself. Empty when none are configured.
        # What was actually read, so a front-end showing the path shows the one
        # in force rather than the default it may have been told to ignore.
        "log_sources_path": log_sources_path or runtime_paths.logsources_path(),
        "log_sources": log_sources,
        "tags": sorted({tag for s in scenarios for tag in s["tags"]}),
        "envs": envs,
        "users": users,
        "scenarios": scenarios,
        "blocks": blocks,
        "selectors": selectors,
        "extensions": extensions,
        "warnings": warnings,
    }


def flow_actions():
    """Every step action, grouped by the shape of its arguments.

    Empty when the engine is not installed - a plain launch never needs it, and
    --describe degrades rather than failing when the flows extra is missing.
    """
    try:
        from engine import compiler
    except ImportError:
        return {}
    return {
        # target only, and the target is a named selector or raw CSS
        "selector_only": sorted(compiler.SELECTOR_ONLY),
        # target and value both required
        "selector_and_value": sorted(compiler.SELECTOR_AND_VALUE),
        # value only; there is nothing to point at
        "value_only": sorted(compiler.VALUE_ONLY),
        # target is a URL, never a selector
        "url_target": sorted(compiler.URL_TARGET),
        # target is a "Project/Service" reference from services.json
        "service_target": sorted(compiler.SERVICE_TARGET),
        # ...and the value is a regex over its output, or a criterion's name
        "service_target_and_value": sorted(compiler.SERVICE_TARGET_AND_VALUE),
        # composition: the target is another flow's id
        "use": [compiler.USE],
        # accepted by wait_for; anything else is Playwright's default
        "states": ["visible", "attached", "hidden", "detached"],
    }


def cycle_plugins():
    """Every cycle plugin's metadata: what it takes, produces, and touches.

    The same bargain ``flow_actions`` makes for the step grammar, one level up.
    A front-end builds a step's configuration form from these fields, so the
    form and the validation behind it read one table and cannot drift apart -
    and a plugin added here appears in the editor without the editor changing.

    Empty when the engine extra is not installed, like everything else in
    --describe: a plain launch has no use for it and must not pay for it.
    """
    try:
        from cycle import registry
    except ImportError:
        return []
    try:
        return registry.describe()
    except Exception:     # noqa: BLE001 - one bad plugin must not deny the rest
        return []


def cycle_inventory(cycles_dir=None):
    """Every cycle on the machine, enough of each to list it.

    Deliberately the cheap half: parse problems travel, per-plugin settings
    validation does not. --describe reads every cycle on every start and the
    splash waits on it, so the expensive check stays in --cycle-show, which is
    asked for one cycle at a time.
    """
    try:
        from cycle import cyclefile
    except ImportError:
        return []
    try:
        return cyclefile.list_cycles(cycles_dir)
    except Exception:     # noqa: BLE001 - a broken tree is an empty list
        return []


def run_flow_command(command, source, flows_dir=None):
    """Serve one --flow-show/save/delete/import call: print JSON, then exit.

    The scenario file format belongs to the engine, and the GUI depends on PySide6
    and nothing else - it cannot read or write YAML. So these four commands are
    the whole of the editing surface, and like --describe they always answer with
    JSON, even on failure: the caller is a program, and an exit code plus a
    plain-text message would leave it parsing error strings.
    """
    kind, argument = command
    try:
        # Lazy, like every other engine import here: a launch must not pay for
        # pyyaml just because these flags exist.
        from engine import flowfile
        if kind == "show":
            payload = flowfile.describe_flow(argument, flows_dir)
        elif kind == "delete":
            payload = flowfile.delete(argument, flows_dir)
        elif kind == "import":
            payload = flowfile.import_file(argument, flows_dir)
        elif kind == "save":
            payload = _save_flow(argument, source, flows_dir)
        elif kind == "selectors-show":
            payload = flowfile.describe_selectors(flows_dir)
        elif kind == "selectors-save":
            payload = _save_selectors(source, flows_dir)
        else:                                   # unreachable; kept honest anyway
            raise ValueError("unknown flow command %r" % kind)
    except Exception as exc:  # noqa: BLE001 - the report IS the error report
        json.dump({"ok": False, "id": argument,
                   "problems": ["%s: %s" % (type(exc).__name__, exc)]},
                  sys.stdout, indent=2, ensure_ascii=False, default=str)
        print()
        sys.exit(2)
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False, default=str)
    print()
    # "ok" is absent from a --flow-show payload, which is a read and cannot fail
    # here; only the writes report one.
    sys.exit(0 if payload.get("ok", True) else 1)


def _save_flow(flow_id, source, flows_dir=None):
    """--flow-save: read the JSON document at ``source`` and write the scenario.

    The document is either ``{"yaml": "..."}`` - the text someone typed - or
    ``{"meta": {...}, "steps": [...]}``, which is what a step editor and the
    recorder produce. One writer either way, so both go through the same
    validation and land in the same shape on disk.
    """
    from engine import flowfile
    if not source:
        return {"ok": False, "id": flow_id,
                "problems": ["--flow-save needs --from=FILE (a JSON document)"]}
    try:
        with open(source, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as exc:
        return {"ok": False, "id": flow_id,
                "problems": ["cannot read %s: %s" % (source, exc)]}
    if not isinstance(document, dict):
        return {"ok": False, "id": flow_id,
                "problems": ["%s must contain a JSON object" % source]}
    return flowfile.save(flow_id, flows_dir,
                         yaml_text=document.get("yaml"),
                         meta=document.get("meta"),
                         steps=document.get("steps"))


def _save_selectors(source, flows_dir=None):
    """--selectors-save: write the named-target map from a JSON {"yaml": ...}."""
    from engine import flowfile
    if not source:
        return {"ok": False,
                "problems": ["--selectors-save needs --from=FILE (a JSON document)"]}
    try:
        with open(source, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as exc:
        return {"ok": False, "problems": ["cannot read %s: %s" % (source, exc)]}
    if not isinstance(document, dict) or "yaml" not in document:
        return {"ok": False,
                "problems": ["%s must contain a JSON object with a \"yaml\" key"
                             % source]}
    return flowfile.save_selectors(document["yaml"], flows_dir)


def run_plugin_action(target, source):
    """Serve one --cycle-plugin-action call: print JSON, then exit.

    A plugin declares what it can be asked outside a run - is it signed in, can
    it reach its provider - and this is how a front-end asks. Deliberately
    generic: the core knows nothing about what any action means, so a plugin
    added later gets its setup offered in the interface without a line changing
    here. The alternative, a flag per plugin, is how Core ends up knowing about
    every plugin that ever ships.

    ``target`` is ``<plugin id>:<action key>``; ``source`` is an optional JSON
    file holding the step's settings, because whether a plugin is ready usually
    depends on how the step configured it.
    """
    plugin_id, _, key = str(target or "").partition(":")
    settings = {}
    if source:
        try:
            with open(source, encoding="utf-8") as handle:
                settings = json.load(handle)
        except (OSError, ValueError) as exc:
            _plugin_action_answer({"ok": False, "problems":
                                   ["cannot read %s: %s" % (source, exc)]}, 2)
    if not isinstance(settings, dict):
        _plugin_action_answer({"ok": False,
                               "problems": ["%s must contain a JSON object"
                                            % source]}, 2)
    try:
        from cycle import registry
        plugin = registry.get(plugin_id)
        if plugin is None:
            raise ValueError("no plugin called %r is installed" % plugin_id)
        answer = plugin.run_action(key, settings)
    except Exception as exc:  # noqa: BLE001 - the report IS the error report
        _plugin_action_answer({"ok": False, "plugin": plugin_id, "action": key,
                               "problems": ["%s: %s" % (type(exc).__name__, exc)]},
                              2)
    answer = dict(answer or {})
    answer.setdefault("ok", False)
    answer.update(plugin=plugin_id, action=key)
    _plugin_action_answer(answer, 0)


def _plugin_action_answer(payload, code):
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False, default=str)
    print()
    sys.exit(code)


def run_secret_command(command, source, secrets_path=None):
    """Serve one --cycle-secret-* call: print JSON, then exit.

    A secret's value never travels in ``argv``. ``ps`` shows the full command
    line of every process on the machine to every user on it, and shell history
    keeps it afterwards - so the value arrives the way ``--cycle-save`` sends a
    document, in a file named by ``--from``, and only the cycle and the variable
    name are on the line.

    Nothing here ever prints a value back. The only question a front-end has is
    "is this one set", and answering it with the secret itself would put it in a
    pipe, a log and a screen for no reason at all.
    """
    kind, argument = command
    cycle_id, _, name = str(argument or "").partition(":")
    try:
        from cycle import secrets
        path = secrets.resolve_path(secrets_path)
        if kind == "list":
            payload = {"ok": True, "cycle": cycle_id, "path": path,
                       "private": secrets.is_private(path),
                       "secrets": secrets.names(cycle_id, secrets_path)}
        elif kind == "set":
            if not name:
                raise ValueError("--cycle-secret-set needs CYCLE:NAME")
            secrets.set_secret(cycle_id, name, _secret_value(source),
                               secrets_path)
            payload = {"ok": True, "cycle": cycle_id, "name": name,
                       "path": path}
        elif kind == "copy":
            # CYCLE:OTHER rather than CYCLE:NAME here: the whole point is one
            # credential shared by several cycles, and naming the variable as
            # well would mean saying "jira_token" twice for the common case.
            if not name:
                raise ValueError("--cycle-secret-copy needs FROM:TO")
            copied = secrets.copy_to(cycle_id, name, path=secrets_path)
            payload = {"ok": True, "cycle": cycle_id, "to": name,
                       "copied": copied, "path": path}
        elif kind == "delete":
            secrets.delete(cycle_id, name or None, secrets_path)
            payload = {"ok": True, "cycle": cycle_id, "name": name,
                       "path": path}
        else:                                   # unreachable; kept honest anyway
            raise ValueError("unknown secret command %r" % kind)
    except Exception as exc:  # noqa: BLE001 - the report IS the error report
        json.dump({"ok": False, "cycle": cycle_id,
                   "problems": ["%s: %s" % (type(exc).__name__, exc)]},
                  sys.stdout, indent=2, ensure_ascii=False, default=str)
        print()
        sys.exit(2)
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False, default=str)
    print()
    sys.exit(0)


def _secret_value(source):
    """The value for --cycle-secret-set, read from the ``--from`` file.

    An empty or absent file means "clear it", which is how a front-end removes
    a secret it no longer wants without a second flag.
    """
    if not source:
        return ""
    try:
        with open(source, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ValueError("cannot read %s: %s" % (source, exc))
    if not isinstance(document, dict) or "value" not in document:
        raise ValueError("%s must contain a JSON object with a \"value\" key"
                         % source)
    return str(document["value"] or "")


def run_session_command(command, memory_path=None, cycles_dir=None):
    """Serve one --cycle-session* call: print JSON, then exit.

    A session is every run of one cycle on one subject - see
    ``cycle/sessions.py``. Listing is how a front-end shows "what have these
    cycles been working on"; deleting removes the runs, their index rows and
    the memory record the subject names, and says exactly which.
    """
    kind, argument = command
    try:
        from cycle import sessions
        if kind == "list":
            payload = {"ok": True,
                       "sessions": sessions.sessions(cycle_id=argument,
                                                     memory_path=memory_path,
                                                     cycles_dir=cycles_dir)}
        elif kind == "delete":
            payload = dict(sessions.delete(argument, memory_path=memory_path),
                           ok=True)
        else:                                   # unreachable; kept honest anyway
            raise ValueError("unknown session command %r" % kind)
    except Exception as exc:  # noqa: BLE001 - the report IS the error report
        json.dump({"ok": False, "id": argument,
                   "problems": [str(exc) if exc.__class__.__name__ == "SessionError"
                                else "%s: %s" % (type(exc).__name__, exc)]},
                  sys.stdout, indent=2, ensure_ascii=False, default=str)
        print()
        sys.exit(2)
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False, default=str)
    print()
    sys.exit(0)


def _plan_document(source):
    """``{"title", "url"}`` from the ``--from`` file, or {} without one."""
    if not source:
        return {}
    with open(source, encoding="utf-8") as handle:
        document = json.load(handle)
    return document if isinstance(document, dict) else {}


def run_watch_command(command, cycles_dir=None, secrets_path=None, memory_path=None,
                      source=None):
    """Serve one --cycle-watch* call: print JSON, then exit.

    ``--cycle-watch=CYCLE[:TRIGGER]`` asks the queue a trigger watches what is
    new since it last looked; ``--cycle-watch-seen=CYCLE[:TRIGGER]:KEY``
    records one key as seen so it is not offered again. Neither starts a run:
    that is the person's decision, made in the application.
    """
    kind, argument = command
    try:
        from cycle import loader, watch
        parts = argument.split(":")
        if kind == "planned":
            from cycle import planner
            payload = {"ok": True, "planned": planner.items(memory_path)}
        elif kind == "unplan":
            from cycle import planner
            payload = {"ok": True, "id": argument,
                       "removed": planner.remove(argument, memory_path, cycles_dir)}
        elif kind == "plan":
            if len(parts) < 2:
                raise ValueError("--cycle-watch-plan needs CYCLE:KEY or "
                                 "CYCLE:TRIGGER:KEY")
            cycle_id, key = parts[0], parts[-1]
            trigger_id = parts[1] if len(parts) > 2 else ""
            about = _plan_document(source)
            cycle = loader.load_cycle(cycle_id, cycles_dir)
            payload = {"ok": True, "cycle": cycle_id, "key": key,
                       "planned": watch.plan(cycle, key, about.get("title", ""),
                                             about.get("url", ""), trigger_id,
                                             memory_path)}
        elif kind == "check":
            cycle_id, trigger_id = parts[0], (parts[1] if len(parts) > 1 else "")
            cycle = loader.load_cycle(cycle_id, cycles_dir)
            payload = dict(watch.check(cycle, trigger_id, secrets_path=secrets_path,
                                       memory_path=memory_path,
                                       env=dict(os.environ)), ok=True)
        else:
            if len(parts) < 2:
                raise ValueError("--cycle-watch-seen needs CYCLE:KEY or "
                                 "CYCLE:TRIGGER:KEY")
            cycle_id, key = parts[0], parts[-1]
            trigger_id = parts[1] if len(parts) > 2 else ""
            cycle = loader.load_cycle(cycle_id, cycles_dir)
            payload = {"ok": True, "cycle": cycle_id, "key": key,
                       "seen": watch.mark_seen(cycle, key, trigger_id, memory_path)}
    except Exception as exc:  # noqa: BLE001 - the report IS the error report
        json.dump({"ok": False, "id": argument,
                   "problems": [str(exc) if exc.__class__.__name__ in (
                       "WatchError", "CycleNotFound") else
                       "%s: %s" % (type(exc).__name__, exc)]},
                  sys.stdout, indent=2, ensure_ascii=False, default=str)
        print()
        sys.exit(2)
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False, default=str)
    print()
    sys.exit(0)


def run_memory_command(command, source, memory_path=None):
    """Serve one --cycle-memory-* call: print JSON, then exit.

    What the project remembers is a plain file somebody can open, unlike the
    secrets store - so these exist to answer the question from a script rather
    than to protect anything. Forgetting is offered because a record that is
    wrong is worse than none: a cycle that believes a task is done will not do
    it again.
    """
    kind, argument = command
    try:
        from cycle import memory
        path = memory.resolve_path(memory_path)
        if kind == "list":
            payload = {"ok": True, "path": path,
                       "keys": memory.keys(argument, memory_path)}
        elif kind == "show":
            payload = dict(memory.describe(argument, memory_path) or
                           {"key": argument, "fields": {}}, ok=True, path=path)
        elif kind == "forget":
            payload = {"ok": True, "key": argument, "path": path,
                       "forgotten": memory.forget(argument, memory_path)}
        else:                                   # unreachable; kept honest anyway
            raise ValueError("unknown memory command %r" % kind)
    except Exception as exc:  # noqa: BLE001 - the report IS the error report
        json.dump({"ok": False, "key": argument,
                   "problems": ["%s: %s" % (type(exc).__name__, exc)]},
                  sys.stdout, indent=2, ensure_ascii=False, default=str)
        print()
        sys.exit(2)
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False, default=str)
    print()
    sys.exit(0)


def run_cycle_command(command, source, cycles_dir=None):
    """Serve one --cycle-show/save/delete/import/list call: print JSON, then exit.

    Written from :func:`run_flow_command` and answering the same way, because it
    has the same job one level up: the cycle file format belongs to the core,
    the GUI depends on PySide6 and nothing else, and the caller is a program - so
    every one of these answers with JSON whether or not it worked, rather than
    leaving somebody parsing an error string out of stderr.
    """
    kind, argument = command
    try:
        # Lazy, like every other core import here: a plain launch must not pay
        # for pyyaml just because these flags exist.
        from cycle import cyclefile, registry
        if kind == "show":
            payload = cyclefile.describe_cycle(argument, cycles_dir, registry)
        elif kind == "list":
            payload = {"cycles": cyclefile.list_cycles(cycles_dir, registry)}
        elif kind == "delete":
            payload = cyclefile.delete(argument, cycles_dir)
        elif kind == "import":
            payload = cyclefile.import_file(argument, cycles_dir, registry)
        elif kind == "save":
            payload = _save_cycle(argument, source, cycles_dir)
        else:                                   # unreachable; kept honest anyway
            raise ValueError("unknown cycle command %r" % kind)
    except Exception as exc:  # noqa: BLE001 - the report IS the error report
        json.dump({"ok": False, "id": argument,
                   "problems": ["%s: %s" % (type(exc).__name__, exc)]},
                  sys.stdout, indent=2, ensure_ascii=False, default=str)
        print()
        sys.exit(2)
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False, default=str)
    print()
    # "ok" is absent from a --cycle-show or --cycle-list payload, which are
    # reads and cannot fail here; only the writes report one.
    sys.exit(0 if payload.get("ok", True) else 1)


def _save_cycle(cycle_id, source, cycles_dir=None):
    """--cycle-save: read the JSON document at ``source`` and write the cycle.

    The document is either ``{"yaml": "..."}`` - the text somebody typed - or
    the cycle document itself, which is what a form produces. One writer either
    way, so both go through the same validation and land in the same shape.
    """
    from cycle import cyclefile, registry
    if not source:
        return {"ok": False, "id": cycle_id,
                "problems": ["--cycle-save needs --from=FILE (a JSON document)"]}
    try:
        with open(source, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as exc:
        return {"ok": False, "id": cycle_id,
                "problems": ["cannot read %s: %s" % (source, exc)]}
    if not isinstance(document, dict):
        return {"ok": False, "id": cycle_id,
                "problems": ["%s must contain a JSON object" % source]}
    if "yaml" in document:
        return cyclefile.save(cycle_id, cycles_dir, yaml_text=document["yaml"],
                              registry=registry)
    return cyclefile.save(cycle_id, cycles_dir, document=document,
                          registry=registry)


def run_cycle_mode(cycle_id, cycles_dir=None, flows_dir=None, variables=None,
                   jobs=None, control=False, reports_dir=None,
                   secrets_path=None, memory_path=None, only=None,
                   start_from="", resume="", reuse_from=""):
    """Run one cycle to completion. Returns the exit code.

    Handled before the Chrome launch loop, because a cycle opens no windows of
    its own: the step that drives a browser is ``scenario.run``, and it opens
    what it needs when it gets there.

    The control channel is the same one a scenario run uses, set up the same
    way - so a cycle's service steps reach the GUI through the bridge that
    already exists, and ``cycle.stop`` arrives on the same stdin reader. With no
    control channel the service steps say so immediately instead of waiting out
    a timeout, which is what makes a cycle runnable from a terminal.
    """
    from cycle import ask as cycle_ask
    from cycle import bus, checkpoints, cyclefile, executor, model, registry
    from cycle import loader as cycle_loader
    from cycle import run as run_mod
    from cycle import workspace as workspace_mod
    from engine import services as engine_services

    try:
        cycle, raw = cycle_loader.read_cycle(cycle_id, cycles_dir)
    except Exception as exc:  # noqa: BLE001 - a missing or malformed file
        log.error("%s", exc)
        _emit("cycle.run.end", cycle=cycle_id, status="error",
              message=str(exc), exit_code=2)
        return 2

    problems = model.problems(cycle, registry=registry, raw=raw)
    if problems:
        # Refused loudly and before anything starts. A cycle that half-runs and
        # then stops on a typo has already started services somebody now has to
        # find and stop by hand.
        log.error("%s cannot run:", cycle_id)
        for problem in problems:
            log.error("  %s", problem)
        _emit("cycle.run.end", cycle=cycle_id, status="error",
              message="; ".join(problems[:3]), exit_code=2)
        return 2

    chosen, refusal = _cycle_selection(cycle, only, start_from)
    if resume and (only or start_from or variables or reuse_from):
        refusal = "Resume cannot be combined with partial selection or variable overrides."
    if resume and (workspace_mod.safe_id(resume) != resume or resume in (".", "..")):
        refusal = "Resume requires a run ID, not a path."
    if reuse_from and (not chosen or workspace_mod.safe_id(reuse_from) != reuse_from):
        refusal = "--cycle-reuse requires a run ID and --cycle-only or --cycle-from."
    reuse = (run_mod.load(workspace_mod.path_of(reuse_from)) if reuse_from and not refusal
             else run_mod.latest_of(cycle.id) if chosen else None)
    if reuse_from and (reuse is None or reuse.cycle_id != cycle.id):
        refusal = "No saved run %s for cycle %s." % (reuse_from, cycle.id)
    if not refusal and chosen:
        absent = executor.missing_for(cycle, chosen, reuse)
        if absent:
            refusal = ("%s reads what %s produced, and %s did not run in %s. "
                       "Run the cycle once before running part of it."
                       % (", ".join(sorted(chosen)), ", ".join(absent),
                          "they" if len(absent) > 1 else "it",
                          reuse.id if reuse is not None
                          else "any earlier run of this cycle"))
    if refusal:
        # Before anything starts, like every other refusal here. The
        # alternative is a step failing several minutes in on a reference that
        # does not exist - true, unhelpful, and after the expensive part.
        log.error("%s", refusal)
        _emit("cycle.run.end", cycle=cycle_id, status="error",
              message=refusal, exit_code=2)
        return 2

    run_id = resume or workspace_mod.new_run_id(cycle_id)
    workspace = (workspace_mod.path_of(run_id) if resume
                 else workspace_mod.create(run_id))
    if resume and not os.path.isfile(run_mod.metadata_path(workspace)):
        message = "No saved run named %s." % resume
        log.error("%s", message)
        _emit("cycle.run.end", cycle=cycle_id, status="error", message=message, exit_code=2)
        return 2
    log.info("Run %s -> %s", run_id, workspace)
    # The same event a scenario run sends, and for the same reason: it is the
    # only way anything watching finds out where this run's files are being
    # written. Without it a cycle's reports, its agents' transcripts and the
    # JSON a Jira step saved were on disk and unreachable - History had no
    # directory to remember and the Artifacts page had nothing to open.
    _emit("run.dir", dir=workspace)

    values = _cycle_secrets(cycle, secrets_path)

    # The run's own copy of what it said, in its own directory. Unconditional,
    # unlike the stream below: a run started from a terminal is as worth
    # reading afterwards as one started from a front-end, and this is what
    # lets either read back a run nothing was watching at the time.
    record = bus.JsonlObserver(workspace, run_id)
    observers = [run_mod.Persister(), record]
    if _events is not None and _events.enabled():
        observers.append(bus.EventObserver(run_id))

    if control:
        # The identical two lines a --run-tests run uses: a reader on stdin for
        # the answers, and the service bridge turned on only when both halves of
        # the pipe are live. Asking a person rides the same pipe and is turned
        # on by the same condition - there is somewhere to ask only when there
        # is both an event out and a channel back.
        threading.Thread(target=read_commands, args=({},), daemon=True).start()
        engine_services.configure(_events is not None and _events.enabled())
        cycle_ask.configure(_events is not None and _events.enabled())

    try:
        run = executor.run_cycle(
            cycle, workspace, variables_=variables, jobs=jobs,
            observer=bus.Tee(observers), flows_dir=flows_dir,
            cycles_dir=cycles_dir, run_id=run_id, secrets=values,
            memory_path=memory_path, only=chosen, reuse=reuse, resume=bool(resume))
    except checkpoints.CheckpointError as exc:
        log.error("%s", exc)
        _emit("cycle.run.end", cycle=cycle_id, status="error", message=str(exc), exit_code=2)
        return 2
    finally:
        # Whatever happened, release anything still blocked waiting for the GUI
        # to answer a service request - the same thing a scenario run does -
        # and anyone still waiting on a question nobody is going to answer now.
        engine_services.abandon_all()
        cycle_ask.abandon_all()
        record.close()

    _print_cycle_summary(run)
    # run.finished as well, so a front-end that already settles on that event
    # for scenario runs needs no new handling to notice a cycle has ended.
    _emit("run.finished", run=run_id, status=run.status,
          exit_code=run.exit_code)
    return run.exit_code


def _cycle_selection(cycle, only, start_from):
    """``(step ids to run, why not)`` for --cycle-only and --cycle-from.

    Empty ids mean the whole cycle, which is the ordinary case and not a
    selection at all. A name that is not a step is refused rather than
    quietly ignored: somebody who mistyped a step id meant to run something.
    """
    from cycle import model        # lazy, like everything else the cycle needs

    known = {step.id for step in cycle.steps}
    if start_from and only:
        return set(), "--cycle-only and --cycle-from ask for two different things."
    if start_from:
        if start_from not in known:
            return set(), "%s is not a step in %s." % (start_from, cycle.id)
        return model.downstream(cycle, {start_from}), ""
    named = set(only or ())
    unknown = sorted(named - known)
    if unknown:
        return set(), "%s %s not a step in %s." % (
            ", ".join(unknown), "is" if len(unknown) == 1 else "are", cycle.id)
    return named, ""


def _cycle_secrets(cycle, secrets_path):
    """The declared secrets of this cycle, decrypted, for the run alone.

    Warns about a name the cycle declares and the store has no value for,
    instead of refusing: the run may not reach that step at all, and a cycle
    someone has just been handed should be able to tell them which values it
    wants rather than only that it will not start.
    """
    declared = tuple(getattr(cycle, "secrets", ()) or ())
    if not declared:
        return {}
    try:
        from cycle import secrets as secrets_mod
        held = secrets_mod.load(secrets_path).get(cycle.id, {})
    except Exception as exc:  # noqa: BLE001 - a run without them still runs
        log.warning("cannot read the secrets store: %s", exc)
        held = {}
    missing = [name for name in declared if not held.get(name)]
    if missing:
        log.warning("%s has no value set for: %s", cycle.id, ", ".join(missing))
    return {name: held.get(name, "") for name in declared}


def _print_cycle_summary(run):
    """The end of a run, on stderr where the rest of the log goes."""
    tally = run.tally()
    log.info("%s %s in %.1fs - %d passed, %d failed, %d skipped",
             run.cycle_id, run.status, run.duration_ms / 1000.0,
             tally.get("success", 0),
             tally.get("failed", 0) + tally.get("timeout", 0),
             tally.get("skipped", 0))
    for step_id, step in run.steps.items():
        if step.status != "success":
            log.info("  %s: %s%s", step_id, step.status,
                     " - %s" % step.message if step.message else "")
    log.info("Run directory: %s", run.workspace)


def _shortening_example(envs):
    """An alias shortening that really is unambiguous, for the help text.

    Prefers the tail of a hyphenated alias ("app-dev" -> "dev"), which is how
    these get typed in practice; falls back to a unique 3-letter head.
    """
    for env in sorted(envs, key=lambda e: "-" not in e.alias):  # hyphenated first
        for candidate in (env.alias.rsplit("-", 1)[-1], env.alias[:3]):
            if candidate != env.alias and sum(candidate in e.alias for e in envs) == 1:
                return candidate, env.alias
    return None, None


def format_environments(envs, config_path):
    """Render the known environments as a help block (used by every env error)."""
    if not envs:
        return "No environments are defined in %s (no 'env' values)." % config_path
    alias_w = max(len(e.alias) for e in envs)
    origin_w = max(len(e.origin or "-") for e in envs)
    lines = ["Known environments (from %s):" % config_path]
    for env in sorted(envs, key=lambda e: e.alias):
        lines.append("  %-*s   %-*s   env %r  (%d user%s)"
                     % (alias_w, env.alias, origin_w, env.origin or "-", env.value,
                        env.count, "" if env.count == 1 else "s"))
    short, full = _shortening_example(envs)
    if short:
        lines.append("Any unambiguous shortening works, e.g. --env=%s for %s." % (short, full))
    return "\n".join(lines)


def resolve_environment(name, envs, config_path):
    """Resolve --env to exactly one Env, or exit with the list of known ones.

    Staged: exact match on alias/value/origin/host first (so an old --filter-prefix
    string still works verbatim), then alias prefix, then alias substring. A stage
    that matches several is ambiguous and stops there rather than falling through -
    silently picking one is how the wrong windows get launched.
    """
    listing = format_environments(envs, config_path)
    wanted = name.strip().lower()
    if not wanted:
        sys.exit("--env requires a name, e.g. --env=dev.\n%s" % listing)
    stages = (
        lambda e: wanted in (e.alias, e.value.lower(), e.origin.lower(),
                             (split_env_value(e.value).hostname or "").lower()),
        lambda e: e.alias.startswith(wanted),
        lambda e: wanted in e.alias,
    )
    for match in stages:
        hits = [e for e in envs if match(e)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            sys.exit("--env=%r matches %d environments: %s. Use a longer name.\n%s"
                     % (name, len(hits), ", ".join(sorted(e.alias for e in hits)), listing))
    extra = ""
    if wanted == "all":
        extra = ("\n--env selects exactly one environment; omit it to launch every "
                 "environment at once.")
    sys.exit("--env: no environment matches %r.\n%s%s" % (name, listing, extra))


def normalize_url(url):
    """Point a bare/login URL at /web; the app redirects to the login form itself.

    If we go to /web the app uses the saved session; with no session it redirects
    to its login page, where our extension logs us in.
    """
    split = urlsplit(url)
    path = split.path
    if path in LOGIN_PATHS:
        path = LANDING_PATH
    return split._replace(path=path).geturl()


def parse_filter_list(flag, value):
    """Parse a comma-separated --filter-* value; None means "no filtering".

    "all" (or the flag being absent) keeps everything, but an explicitly EMPTY
    value is an error: --filter-users="$VAR" with an unset VAR used to silently
    mean "all", which is how a one-user run turned into every user in the config.
    """
    cleaned = [part.strip() for part in value.split(",") if part.strip()]
    if not cleaned:
        sys.exit('%s= is empty. Omitting the flag already means "all"; pass %s=all to keep '
                 "everyone, or --env=NAME to pick one environment. (An unset shell variable "
                 "is the usual cause.)" % (flag, flag))
    if len(cleaned) == 1 and cleaned[0].lower() == "all":
        return None
    return cleaned


def lookup_scope(selected_env, origin, envs):
    """Which env's rows an ad-hoc --user is resolved against ("" = the whole config).

    --env picks it outright; failing that, a --url whose origin belongs to a known
    environment picks that one, so `--user=X --url=<dev>` still lands in dev.
    """
    if selected_env is not None:
        return selected_env.value
    for env in envs:
        if env.origin and env.origin.lower() == (origin or "").lower():
            return env.value
    return ""


def env_shared_password(users, scope):
    """The password an environment uses, i.e. the one a majority of its rows share.

    Test environments are provisioned with one password across their users (with
    the odd admin outlier), so a login that is not in the config can still be
    launched with the environment's own credentials. None when there is no strict
    majority - then the caller has to ask for --password.
    """
    rows = [u for u in users if not scope or u.env == scope]
    if not rows:
        return None
    counts = collections.Counter(u.password for u in rows)
    password, hits = counts.most_common(1)[0]
    if hits * 2 <= len(rows):
        return None  # no strict majority (e.g. a 1-1 split) - do not guess
    return password, hits, len(rows)


def adhoc_session_prefix(selected_env, origin, envs, url):
    """Folder prefix for an ad-hoc --user run, so its profile is per-environment.

    Ad-hoc profiles used to be the bare login, which meant one folder shared by
    every environment: running the same login against dev and then localhost
    overwrote the session each time. Reuse the environment's own config value so
    an ad-hoc run and a config-driven run for the same user+env share one profile.
    """
    scope = lookup_scope(selected_env, origin, envs)
    if scope:
        return scope
    return urlsplit(url).netloc


def resolve_user_row(cli_user, cli_password, users, selected_env, origin, envs,
                     config_path, url):
    """Build the single (env, class, login, password) row for an ad-hoc --user run.

    --env supplies the record and --user overrides its login, so the password comes
    from the environment unless one was passed explicitly:
      1. --password wins outright.
      2. Otherwise the login's own config row (its class comes along too).
      3. Otherwise the environment's shared password - a login missing from the
         config is not an error, the env still holds the credentials for it.
    """
    session_prefix = adhoc_session_prefix(selected_env, origin, envs, url)
    if cli_password is not None:
        return User(session_prefix, cli_user, cli_user, cli_password)

    scope = lookup_scope(selected_env, origin, envs)
    rows = [u for u in users if u.login == cli_user and (not scope or u.env == scope)]
    if len(rows) == 1:
        row = rows[0]
        return row._replace(env=session_prefix or row.env)
    if len(rows) > 1:
        # Same login in several environments and nothing to pick between them.
        sys.exit("--user=%s exists in %d environments (%s). Pass --env to choose one, "
                 "or --password to launch it ad-hoc.\n%s"
                 % (cli_user, len(rows), ", ".join(env_alias(u.env) for u in rows),
                    format_environments(envs, config_path)))

    shared = env_shared_password(users, scope)
    if shared is None:
        where = "%r" % scope if scope else "the config"
        sys.exit("--user=%s is not in %s for %s, and its users do not share one password. "
                 "Pass --password." % (cli_user, config_path, where))
    password, hits, total = shared
    log.info("%r is not in %s for %s; using the password shared by %d of its %d users.",
             cli_user, os.path.basename(config_path),
             selected_env.alias if selected_env else (scope or "the config"), hits, total)
    return User(session_prefix, cli_user, cli_user, password)


def apply_password_override(users, cli_password):
    """Replace every selected row's password with an explicitly passed --password.

    Same precedence as everywhere else: what you typed beats what the config holds.
    """
    return [u._replace(password=cli_password) for u in users]


def _bad_option_message(arg, config_path):
    """Explain an option the parser cannot use, with a hint for the known cases.

    Every one of these used to fall through to the positional URL slot, so the
    launcher opened Chrome on the flag itself instead of failing.
    """
    if arg.startswith("--filter-prefix"):
        return "--filter-prefix was replaced by --env.\n%s" % format_environments(
            environments_from_config(config_path, strict=False), config_path)
    if arg == "--env":
        return "--env needs a value, e.g. --env=dev.\n%s" % format_environments(
            environments_from_config(config_path, strict=False), config_path)
    if arg == "--run-tests":
        return ("--run-tests needs a value:\n"
                "  --run-tests=all            every scenario in flows/scenarios\n"
                "  --run-tests=config         each user's own 'run-tests' field\n"
                "  --run-tests=ID[,ID...]     named scenarios, or tag:NAME")
    if arg == "--cycle-var":
        return ("--cycle-var needs NAME=VALUE, e.g. --cycle-var=branch=main.\n"
                "Repeat it for each variable a cycle takes.")
    if arg == "--cycle-jobs":
        return "--cycle-jobs needs a number: how many steps may run at once."
    if arg in ("--user", "--password", "--filter-users", "--url", "--config",
               "--sessions-dir", "--user-session", "--log-level", "--report-level",
               "--report-screen", "--jobs", "--flows-dir", "--reports-dir",
               "--extensions", "--cycles-dir", "--cycle-run", "--cycle-show",
               "--cycle-save", "--cycle-delete", "--cycle-import",
               "--cycle-secret-list", "--cycle-secret-set",
               "--cycle-secret-copy",
               "--cycle-secret-delete", "--cycle-secrets-file",
               "--cycle-memory-show", "--cycle-memory-forget",
               "--cycle-memory-file", "--cycle-runs-dir", "--cycle-watch",
               "--cycle-watch-seen", "--cycle-watch-plan", "--cycle-plan-remove"):
        return "%s needs a value: %s=VALUE (note the '=', not a space)." % (arg, arg)
    return ("Unknown option %r. Run --help for the full list." % arg)


def _windows_chrome_paths():
    """Where Chrome installs itself on Windows, which is never on PATH.

    The registry key is the authoritative answer (it is what ShellExecute uses);
    the fixed locations cover a machine where it is missing or unreadable, including
    the per-user install that needs no administrator.
    """
    try:
        import winreg
    except ImportError:
        return
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(
                    root,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") as key:
                value = winreg.QueryValue(key, None)
        except OSError:
            continue
        if value:
            yield value
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            yield os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")


def _chrome_candidates():
    """Everything that might be a Chrome, best first, each one only once.

    The same installation is usually reachable by several routes - the App Paths
    key and %PROGRAMFILES% name one file between them - and every candidate gets
    probed, so a duplicate is a probe nobody needs.
    """
    seen = set()
    for path in _chrome_candidate_paths():
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            yield path


def _chrome_candidate_paths():
    """The raw candidate list, before duplicates are dropped."""
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                 "chrome"):
        path = shutil.which(name)
        if path:
            yield path
    # macOS keeps the binary inside an .app bundle, which is not on PATH.
    if sys.platform == "darwin":
        for path in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                     os.path.expanduser("~/Applications/Google Chrome.app/Contents/"
                                        "MacOS/Google Chrome"),
                     "/Applications/Chromium.app/Contents/MacOS/Chromium"):
            if os.path.exists(path):
                yield path
    if os.name == "nt":
        for path in _windows_chrome_paths():
            if os.path.exists(path):
                yield path


def find_chrome():
    """The browser to launch, or None.

    A candidate that answers ``--version`` wins over one that does not, because
    being on PATH is not the same as being a browser: Ubuntu's `chromium-browser`
    is a 2 KB shim that only redirects to a snap, and on a machine without snapd
    it launches nothing. Preferring a version-answering binary picks the real
    Chrome when both are installed; when nothing answers, the first candidate is
    still returned, so an environment that merely blocks --version behaves as it
    always has and the caller reports the failure (see describe_chrome).
    """
    first = None
    for path in _chrome_candidates():
        if first is None:
            first = path
        if _chrome_version_string(path):
            return path
    return first


def chrome_missing_message():
    """What to tell someone who has no Chrome, in the terms of their platform.

    An installed build is used by people who did not choose to install Python and
    will not read a traceback, so the failure has to name the fix.
    """
    if os.name == "nt":
        how = "Install Google Chrome from https://www.google.com/chrome/"
    elif sys.platform == "darwin":
        how = ("Install Google Chrome from https://www.google.com/chrome/ "
               "and drag it into /Applications")
    else:
        # Not "apt install chromium-browser": on Ubuntu 22.04 that package is a
        # shim that only redirects to a snap, and installing it produces something
        # that looks like a browser on PATH and launches nothing.
        how = ("Download the .deb from https://www.google.com/chrome/ and install "
               "it with:\n"
               "  sudo apt install ./google-chrome-stable_current_amd64.deb\n"
               "(Chromium also works:  sudo snap install chromium)")
    return "Google Chrome was not found on this computer.\n\n%s" % how


def describe_chrome():
    """{path, version, message} for --describe: is there a usable browser?

    A non-empty ``message`` means no, and says how to fix it, so a front-end can
    show the answer without knowing anything about platforms. Note that a path
    with no version is also a "no": that is what Ubuntu's snap-transitional
    `chromium-browser` shim looks like, and launching it opens nothing.
    """
    path = find_chrome()
    if not path:
        return {"path": "", "version": "", "message": chrome_missing_message()}
    version_line, problem = _chrome_version(path)
    if not version_line:
        # Windows reads the version off the file, so a blank answer there means a
        # file without a readable version, not a shim that redirects to a snap -
        # and telling a Windows user about snapd helps nobody. Either way the
        # reason is named: "does not say what it is" sends nobody anywhere.
        why = ("%s exists but does not say what it is: %s." % (path, problem)
               if os.name == "nt" else
               "%s exists but does not run - it is most likely Ubuntu's snap "
               "placeholder rather than a browser." % path)
        return {"path": path, "version": "",
                "message": "%s\n\n%s" % (why, chrome_missing_message())}
    return {"path": path, "version": version_line, "message": ""}


def _windows_file_version(path):
    """(banner, problem) for a .exe: its version resource, or why there is none.

    Reads the file rather than running it. Nothing is started, nothing is
    waited for, and the answer is the same number the browser would print.

    Every failure comes back as a *reason* rather than a bare "", because the
    two are not the same thing and only one of them is worth showing a user:
    "this file has no version resource" is information, "the call failed" is a
    bug in this function.
    """
    # Spelled with plain ctypes types rather than ctypes.wintypes: the import
    # here is inside a function, so PyInstaller's analyser never sees it and the
    # submodule does not have to be in the bundle for this to work.
    import ctypes
    LPCWSTR = ctypes.c_wchar_p
    DWORD, UINT, WORD = ctypes.c_uint32, ctypes.c_uint, ctypes.c_uint16
    # By absolute path, and that is not fussiness. PyInstaller hooks ctypes and
    # redirects a bare library name to a file of that name inside the bundle if
    # one is there - and the bundle contains VERSION, the build's own version
    # stamp. Windows paths being case-insensitive, "version" matched it, and
    # every frozen build loaded a 7-byte text file instead of the system DLL.
    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    try:
        version_dll = ctypes.WinDLL(os.path.join(system32, "version.dll"),
                                    use_last_error=True)
    except OSError as exc:
        return "", "version.dll would not load (%s)" % exc
    version_dll.GetFileVersionInfoSizeW.argtypes = [LPCWSTR, ctypes.POINTER(DWORD)]
    version_dll.GetFileVersionInfoW.argtypes = [LPCWSTR, DWORD, DWORD, ctypes.c_void_p]
    version_dll.VerQueryValueW.argtypes = [ctypes.c_void_p, LPCWSTR,
                                           ctypes.POINTER(ctypes.c_void_p),
                                           ctypes.POINTER(UINT)]
    size = version_dll.GetFileVersionInfoSizeW(path, None)
    if not size:
        return "", "no version resource (GetFileVersionInfoSize said %d, error %d)" % (
            size, ctypes.get_last_error())
    block = ctypes.create_string_buffer(size)
    if not version_dll.GetFileVersionInfoW(path, 0, size, block):
        return "", "GetFileVersionInfo failed (error %d)" % ctypes.get_last_error()

    def query(sub_block):
        """One value out of the resource, as text, or ""."""
        pointer, length = ctypes.c_void_p(), UINT()
        if not version_dll.VerQueryValueW(block, sub_block,
                                          ctypes.byref(pointer), ctypes.byref(length)):
            return ""
        return ctypes.wstring_at(pointer, length.value).strip("\x00").strip()

    # The strings live under the file's own language/codepage, which has to be
    # read out of the translation table first - "040904b0" is common but not a
    # given, and guessing it is how this returns "" on a perfectly good exe.
    pointer, length = ctypes.c_void_p(), UINT()
    if not version_dll.VerQueryValueW(block, "\\VarFileInfo\\Translation",
                                      ctypes.byref(pointer), ctypes.byref(length)):
        return "", "the version resource carries no translation table"
    language, codepage = ctypes.cast(
        pointer, ctypes.POINTER(WORD * 2)).contents
    prefix = "\\StringFileInfo\\%04x%04x\\" % (language, codepage)
    number = query(prefix + "ProductVersion") or query(prefix + "FileVersion")
    if not number:
        return "", "the version resource names no version (%s)" % prefix.strip("\\")
    return ("%s %s" % (query(prefix + "ProductName") or "Chrome", number)).strip(), ""


def _windows_chrome_version_from_layout(path):
    """Chrome's version taken from the folder it installs beside its .exe.

    Chrome unpacks each release into ``Application\\<version>\\`` next to
    chrome.exe, so the number is readable with nothing but a directory listing.
    A second opinion for when the version resource cannot be read - which is
    every browser that is not Chrome, and any Chrome whose resource is
    unreadable for reasons this program cannot do anything about.
    """
    try:
        beside = os.listdir(os.path.dirname(path) or ".")
    except OSError:
        return ""
    versions = sorted(
        (name for name in beside if re.match(r"^\d+\.\d+\.\d+\.\d+$", name)),
        key=lambda name: [int(part) for part in name.split(".")])
    return "Google Chrome %s" % versions[-1] if versions else ""


def _chrome_version_string(chrome):
    """Chrome's own version banner, or "" when it will not say."""
    return _chrome_version(chrome)[0]


def _chrome_version(chrome):
    """(banner, problem): the browser's version, or why it could not be had.

    The problem travels with the answer so describe_chrome can say something
    true about a browser it cannot identify, instead of one guess for every
    cause.
    """
    if os.name == "nt":
        # Windows Chrome does not implement --version. The switch is ignored and
        # the browser starts instead: the call never answers, it opens a window.
        # Anything that wants to know what is installed would open one every time
        # it asked - and --describe asks twice, once per candidate.
        try:
            banner, problem = _windows_file_version(chrome)
        except Exception as exc:     # a version resource is optional, not a promise
            banner, problem = "", "%s: %s" % (type(exc).__name__, exc)
        if banner:
            return banner, ""
        # The resource could not be read; Chrome still names its version in the
        # folder beside the executable, and that is enough to answer with.
        from_layout = _windows_chrome_version_from_layout(chrome)
        if from_layout:
            log.debug("Read %s from the install layout: %s", chrome, problem)
            return from_layout, ""
        log.debug("Could not read the version of %s: %s", chrome, problem)
        return "", problem
    try:
        out = subprocess.run([chrome, "--version"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, timeout=10,
                             env=runtime_paths.clean_subprocess_env()).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return "", "it would not run (%s)" % exc
    out = (out or "").strip()
    return out, "" if out else "it printed no version"


#: Windows cannot register an extension by writing the profile's preferences -
#: Chrome's tamper protection strips the entry - so the install goes over CDP
#: instead, which needs a debug port on every launch rather than only under
#: --run-tests. The port is loopback-only and Chrome picks it (port 0).
EXTENSIONS_NEED_CDP = os.name == "nt"


# Chrome/webkit timestamps are microseconds since 1601-01-01.
_CHROME_EPOCH_US = 11644473600000000


# --password-store is a Linux-only switch: macOS keys off the login Keychain and
# Windows off DPAPI, so passing it there is meaningless.
_PASSWORD_STORE_ARGS = ["--password-store=basic"] if sys.platform.startswith("linux") else []

# prctl(2) option number for PR_SET_PDEATHSIG (Linux, stable across arches).
_PR_SET_PDEATHSIG = 1

# Execution-overlay (HUD) components accepted by --execution-overlay. Keep in
# sync with engine.overlay.KNOWN_COMPONENTS (defined here too so a plain launch
# never imports the engine just to validate the flag).
_OVERLAY_COMPONENTS = ("tree", "progress", "status", "logs", "highlight", "notifications")

# Same deal for the report flags: mirrors engine.artifacts.ARTIFACTS / SCREEN_MODES
# so --describe can advertise the choices without importing the engine.
_REPORT_ARTIFACTS = ("console", "dom", "result", "screen", "url")
_REPORT_SCREEN_MODES = ("start", "each", "finish")

# --server-log values that are words rather than log names. Deliberately the same
# vocabulary as --extensions: "all", "none" and "list" already mean these things
# here, and a second spelling for the same idea is a thing to remember for nothing.
_SERVER_LOG_ALL = "all"
_SERVER_LOG_NONE = "none"
_SERVER_LOG_LIST = "list"
# The bare flag spelled out. A front-end builds a command line from a form, and
# "leave the value off entirely" is not something a {flag: value} mapping can
# express - so the thing the bare flag means gets a name of its own.
_SERVER_LOG_DEFAULT = "default"


def _preexec_die_with_parent():
    """Child-side hook: ask the kernel to signal us when our parent launcher dies.

    Runs in the forked child just before exec (Linux only). We start a fresh session
    (setsid) so the whole window tree can still be killpg'd / survive --detach, then
    arm PR_SET_PDEATHSIG so Chrome gets a SIGTERM the moment the launcher process
    goes away - even a SIGKILL, which no Python handler can catch. This is what makes
    VS Code's "Stop" button (which hard-kills the debuggee) close the windows too.
    """
    os.setsid()  # own session/process group (replaces start_new_session=True)
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
    except (OSError, AttributeError):
        pass  # non-Linux libc or prctl missing: best-effort, skip silently


# Windows job-object constants (winnt.h). The extended-limit struct is passed by
# class 9, and bit 0x2000 of its LimitFlags is what makes the kernel kill every
# member when the last handle to the job closes.
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

#: Created once, on first use, and never closed on purpose: the handle closing is
#: precisely the signal that kills the windows, so it must live exactly as long as
#: the launcher process does.
_windows_job = None


def _windows_kill_on_close_job():
    """A job object whose members die when this launcher does. None off Windows.

    Windows has no PR_SET_PDEATHSIG. What it has is a job: put every browser in
    one, mark it kill-on-close, and the kernel tears the whole set down when the
    last handle goes - which happens when this process exits, however it exits,
    including a hard kill no Python handler could catch. That is the same promise
    the Linux path makes, made by the other operating system's own mechanism.

    Deliberately not used with --detach, where the windows are meant to outlive
    us. Best-effort: a machine that refuses the call gets windows that may
    outlive a hard kill, which is what it would have had anyway.
    """
    global _windows_job
    if os.name != "nt":
        return None
    if _windows_job is not None:
        return _windows_job
    try:
        import ctypes.wintypes as wintypes

        class _IoCounters(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                        ("WriteOperationCount", ctypes.c_ulonglong),
                        ("OtherOperationCount", ctypes.c_ulonglong),
                        ("ReadTransferCount", ctypes.c_ulonglong),
                        ("WriteTransferCount", ctypes.c_ulonglong),
                        ("OtherTransferCount", ctypes.c_ulonglong)]

        class _BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class _ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _BasicLimits),
                        ("IoInfo", _IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
                job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits), ctypes.sizeof(limits)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
    except (OSError, AttributeError, ImportError) as exc:
        log.debug("No kill-on-close job object (%s); windows may outlive a hard "
                  "kill of the launcher.", exc)
        _windows_job = False        # asked and answered; do not try again
        return None
    _windows_job = job
    return job


def _adopt_into_windows_job(proc):
    """Put one launched browser into the kill-on-close job, if there is one."""
    job = _windows_kill_on_close_job()
    handle = getattr(proc, "_handle", None)
    if not job or handle is None:
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not kernel32.AssignProcessToJobObject(job, int(handle)):
            log.debug("Could not adopt pid %s into the job object (%s).",
                      proc.pid, ctypes.get_last_error())
    except (OSError, AttributeError):
        pass

# Keychain items Chrome/Chromium store their macOS "Safe Storage" secret under.
_MACOS_KEYCHAIN_ITEMS = (("Chrome Safe Storage", "Chrome"),
                         ("Chromium Safe Storage", "Chromium"))


def _macos_safe_storage_key():
    """Derive Chrome's password-store key from the macOS login Keychain.

    On macOS Chrome does not use a fixed secret: it generates a random password once
    per install and keeps it in the login Keychain as "Chrome Safe Storage", then
    stretches it with 1003 PBKDF2 rounds. Reading it the first time pops a Keychain
    permission dialog - choose "Always Allow" so later runs are silent.
    """
    for service, account in _MACOS_KEYCHAIN_ITEMS:
        try:
            done = subprocess.run(
                ["security", "find-generic-password", "-w", "-s", service, "-a", account],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, text=True)
        except (subprocess.CalledProcessError, OSError):
            continue  # item absent (e.g. Chromium vs Chrome) or access denied
        secret = done.stdout.strip()
        if secret:
            return hashlib.pbkdf2_hmac("sha1", secret.encode("utf-8"), b"saltysalt", 1003, 16)
    raise RuntimeError("could not read the Chrome Safe Storage key from the macOS "
                       "Keychain (run Chrome once, then allow the Keychain prompt)")


def _encrypt_password(plaintext):
    """Encrypt a password the way this platform's Chrome password store does (v10).

    Same envelope everywhere - AES-128-CBC, PKCS7 padding, fixed IV, "v10" prefix -
    but the key differs: Linux with --password-store=basic derives it from the fixed
    password "peanuts" in a single PBKDF2 round, while macOS uses the random Keychain
    secret and 1003 rounds. Using the wrong one writes a blob Chrome cannot decrypt.

    Windows is not supported here: Chrome there uses AES-256-GCM under a per-install
    key that is itself DPAPI-encrypted in the profile's Local State, a different
    envelope entirely. seed_password skips the step rather than writing a blob Chrome
    would silently fail to read - the auto-login extension is what actually signs in,
    so only Chrome's saved-passwords list stays empty.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    if sys.platform == "darwin":
        key = _macos_safe_storage_key()
    else:
        key = hashlib.pbkdf2_hmac("sha1", b"peanuts", b"saltysalt", 1, 16)
    iv = b" " * 16
    data = plaintext.encode("utf-8")
    pad = 16 - (len(data) % 16)
    data += bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return b"v10" + enc.update(data) + enc.finalize()


def _ensure_login_db(chrome, profile):
    """Return the path to this profile's 'Login Data', creating it if absent.

    We let Chrome build the DB (headless, throwaway) so the schema/version always
    match the installed Chrome, then we inject into it. No-op once it exists.
    """
    db = os.path.join(profile, "Default", "Login Data")
    if os.path.exists(db):
        return db
    proc = subprocess.Popen(
        [
            chrome,
            "--headless=new",
            "--user-data-dir=%s" % profile,
            *_PASSWORD_STORE_ARGS,
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=runtime_paths.clean_subprocess_env(),
    )
    for _ in range(40):  # up to ~10s for Chrome to create the file
        if os.path.exists(db):
            break
        time.sleep(0.25)
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    return db


def seed_password(chrome, profile, origin, login, password):
    """Save exactly one credential in this profile's Chrome password manager.

    Any existing entry for the same site is removed first, so the isolated window's
    password manager holds only this user's login/password. The value is encrypted with
    this platform's Chrome key (see _encrypt_password) so Chrome can decrypt it.

    A no-op on Windows, where that key is not reproducible from outside Chrome.
    """
    if os.name == "nt":
        log.info("Windows: skipping the saved-password step (Chrome's password "
                 "store is not writable from outside). Auto-login is unaffected.")
        return
    db = _ensure_login_db(chrome, profile)
    if not os.path.exists(db):
        log.warning("Could not create Login Data; skipping password save.")
        return
    realm = origin + "/"
    login_url = origin + LOGIN_PATHS[2]   # the app's login page
    now = int(time.time() * 1_000_000) + _CHROME_EPOCH_US
    con = sqlite3.connect(db)
    try:
        # one credential per site: drop any prior entry for this origin first
        con.execute("DELETE FROM logins WHERE signon_realm = ?", (realm,))
        con.execute(
            "INSERT INTO logins ("
            "origin_url, action_url, username_element, username_value,"
            "password_element, password_value, submit_element, signon_realm,"
            "date_created, blacklisted_by_user, scheme, date_password_modified"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                login_url, login_url, "login", login,
                "password", _encrypt_password(password), "", realm,
                now, 0, 0, now,
            ),
        )
        con.commit()
    finally:
        con.close()


def set_profile_name(profile, name):
    """Name the Chrome profile so the label shows in the top-right profile pill.

    Persists across page navigation (unlike the title bar, which follows the page).
    Merged into the existing Preferences so a logged-in session is not wiped.
    Only takes effect on the next launch, so run this while the window is closed.
    """
    default = os.path.join(profile, "Default")
    os.makedirs(default, exist_ok=True)
    prefs_path = os.path.join(default, "Preferences")
    prefs = {}
    if os.path.exists(prefs_path):
        try:
            with open(prefs_path, encoding="utf-8") as fh:
                prefs = json.load(fh)
        except (ValueError, OSError):
            prefs = {}
    prefs.setdefault("profile", {})["name"] = name
    # Force Chrome to "Continue where you left off" so transient session cookies are not
    # deleted on exit. The tabs it would otherwise restore are wiped by clear_previous_tabs,
    # so this keeps the login without piling up tabs.
    if "session" not in prefs:
        prefs["session"] = {}
    prefs["session"]["restore_on_startup"] = 1
    # Chrome nags about an "unclean shutdown" and offers to restore tabs when it was killed
    # (which is how close_all/CTRL+C stops it). Mark the last exit clean to suppress that.
    prefs["profile"]["exit_type"] = "Normal"
    prefs["profile"]["exited_cleanly"] = True
    with open(prefs_path, "w", encoding="utf-8") as fh:
        json.dump(prefs, fh)


def clear_previous_tabs(profile):
    """Remove Chrome's saved tab/session state so the next launch opens a single tab.

    restore_on_startup=1 keeps session cookies alive but would also reopen every tab from
    last time. The tab list lives in these Session/Tabs files; cookies live in the separate
    Cookies DB, so deleting these gives one clean tab while leaving the login untouched.
    Run this only while the window is closed.
    """
    default = os.path.join(profile, "Default")
    for name in ("Current Session", "Current Tabs", "Last Session", "Last Tabs"):
        try:
            os.remove(os.path.join(default, name))
        except OSError:
            pass  # missing (fresh profile) or locked; nothing to clear
    # Newer Chrome keeps rolling snapshots under Default/Sessions/ instead.
    sessions_dir = os.path.join(default, "Sessions")
    if os.path.isdir(sessions_dir):
        for fname in os.listdir(sessions_dir):
            if fname.startswith(("Session_", "Tabs_")):
                try:
                    os.remove(os.path.join(sessions_dir, fname))
                except OSError:
                    pass


def clear_devtools_port(profile):
    """Drop the previous run's DevToolsActivePort before relaunching this profile.

    Chrome removes the file on a clean exit, but a window killed outright (SIGKILL
    on the close-timeout path, or PR_SET_PDEATHSIG when the launcher dies) leaves it
    behind holding a dead port. The engine's wait_for_devtools polls this file and
    would return that stale port on its FIRST poll - attaching to nothing, or worse,
    to whatever process the OS has since handed that port to, which can be another
    session's Chrome. Run this only while the window is closed.
    """
    try:
        os.remove(os.path.join(profile, "DevToolsActivePort"))
    except OSError:
        pass  # fresh profile, or already cleaned up by a clean exit


# Default login-form selectors, used when nothing overrides them. Names are the
# common convention; an app that uses different ones (username/email, an id instead
# of a name) overrides them. They live here rather than in the extension source so
# retargeting is a config change, not a JavaScript edit - the source reads them from
# the generated config.js.
DEFAULT_LOGIN_SELECTORS = {
    "login": 'input[name="login"]',
    "password": 'input[name="password"]',
    "submit": 'button[type="submit"]',
}

# The auto-login extension's SOURCE. Underscore-prefixed so local_extensions()
# skips it: it is machinery, not something to install with --extensions (it is
# useless without the per-profile credentials written alongside it).
AUTOLOGIN_SRC = os.path.join(EXTENSIONS_DIR, "_autologin")


def write_autologin_extension(ext_dir, origin, login, password, key_b64=None,
                              selectors=None, src_dir=None):
    """Install the auto-login extension into ext_dir, with this user's credentials.

    The behaviour lives in extensions/_autologin/ as ordinary, editable, lintable
    files. Only the per-profile parts are generated here:

      config.js  - credentials + selectors, loaded BEFORE content.js in the same
                   content_scripts entry, so they share one isolated world. Secrets
                   stay out of the checked-in source and exist only inside the profile.
      manifest   - the source manifest with this user's name, origin match and key.

    key_b64 is the base64 DER public key; embedding it gives the extension a stable,
    key-derived id (see install_autologin_extension) so Chrome can load it from the
    profile without --load-extension, which recent Chrome (137+) blocks.
    """
    src = src_dir or AUTOLOGIN_SRC
    if os.path.isdir(ext_dir):
        shutil.rmtree(ext_dir, ignore_errors=True)   # refresh, so source edits apply
    shutil.copytree(src, ext_dir)

    with open(os.path.join(ext_dir, "manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    manifest["name"] = "Auto-login (%s)" % login
    for entry in manifest.get("content_scripts", []):
        entry["matches"] = [origin + "/*"]
    if key_b64:
        manifest["key"] = key_b64
    with open(os.path.join(ext_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)

    config = {
        "login": login,
        "password": password,
        "selectors": dict(DEFAULT_LOGIN_SELECTORS, **(selectors or {})),
    }
    with open(os.path.join(ext_dir, "config.js"), "w", encoding="utf-8") as fh:
        # json.dumps handles every escaping question a password can raise.
        fh.write("var AUTOLOGIN = %s;\n" % json.dumps(config))


def _extension_id_from_key(pub_der):
    """Chrome's extension id: first 32 hex chars of SHA256(DER public key), mapped
    onto a-p (each hex nibble 0-f -> a-p). This is the same id Chrome derives from
    the manifest "key", so writing that key gives us a stable, predictable id.
    """
    digest = hashlib.sha256(pub_der).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def _profile_extension_key(ext_root, key_file_name):
    """Return a profile-local base64 DER public key, generating+caching it once.

    The key only exists to give an extension a stable id (no CRX is ever signed), so
    we keep just the public half in key_file_name and reuse it on later runs - that
    way the profile keeps one folder per extension instead of piling up a new id each
    launch. A distinct key_file_name per extension keeps their ids from colliding.
    """
    key_file = os.path.join(ext_root, key_file_name)
    if os.path.exists(key_file):
        with open(key_file, encoding="utf-8") as fh:
            return fh.read().strip()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    b64 = base64.b64encode(der).decode("ascii")
    with open(key_file, "w", encoding="utf-8") as fh:
        fh.write(b64)
    return b64


def _merge_extension_pref(profile, ext_id, entry):
    """Merge one extensions.settings entry into the profile's Preferences file.

    Chrome ingests it on startup and re-saves it into its tamper-protected Secure
    Preferences, so the extension sticks. Re-planting the same entry every launch is
    idempotent, so callers may run each launch (this merges into the Preferences file
    set_profile_name already wrote, keeping the rest).
    """
    prefs_path = os.path.join(profile, "Default", "Preferences")
    prefs = {}
    if os.path.exists(prefs_path):
        try:
            with open(prefs_path, encoding="utf-8") as fh:
                prefs = json.load(fh)
        except (ValueError, OSError):
            prefs = {}
    prefs.setdefault("extensions", {}).setdefault("settings", {})[ext_id] = entry
    with open(prefs_path, "w", encoding="utf-8") as fh:
        json.dump(prefs, fh)


def install_autologin_extension(profile, origin, login, password):
    """Install the auto-login extension straight into the profile, no --load-extension.

    Recent Chrome (137+) refuses to load unpacked extensions from the command line
    ("Installation is not enabled"), so instead we register it the way a normally
    installed extension is: the files under Default/Extensions/<id>/<ver>/ plus an
    entry in the profile's Preferences. Chrome ingests that on startup and re-saves
    it into its tamper-protected Secure Preferences, so the extension persists and
    loads on every later launch - the same "prepare it once in the profile" approach
    used for the seeded password (see seed_password).

    Re-planting the same entry every run is idempotent, so this is safe to call each
    launch (like set_profile_name, which it merges into the same Preferences file).
    """
    ext_root = os.path.join(profile, "Default", "Extensions")
    os.makedirs(ext_root, exist_ok=True)
    key_b64 = _profile_extension_key(ext_root, ".autologin_key")
    ext_id = _extension_id_from_key(base64.b64decode(key_b64))
    version = "1.0"
    version_dir = os.path.join(ext_root, ext_id, version + "_0")
    write_autologin_extension(version_dir, origin, login, password, key_b64=key_b64)

    # The extension only touches its own site, so it just needs that host permission.
    host = [origin + "/*"]
    perms = {"api": [], "explicit_host": [], "manifest_permissions": [], "scriptable_host": host}
    entry = {
        "active_permissions": perms,
        "granted_permissions": perms,
        # location 1 = INTERNAL: Chrome treats it as a regular installed extension,
        # not an unpacked/command-line one, so the 137+ side-load block never applies.
        "location": 1,
        "state": 1,  # enabled
        "from_webstore": False,
        "was_installed_by_default": False,
        "was_installed_by_oem": False,
        "path": "%s/%s_0" % (ext_id, version),
        "manifest": json.load(open(os.path.join(version_dir, "manifest.json"), encoding="utf-8")),
    }
    _merge_extension_pref(profile, ext_id, entry)
    return version_dir


# -- loading extensions the way Chrome still allows ---------------------------
# Planting an entry in the profile's Preferences (above) is how this has always
# worked, and on Windows it no longer does: extensions.settings is a protected
# preference, every entry is checked against an HMAC in Secure Preferences, and
# one written from outside has none - so Chrome strips it on the first launch.
# The files stay on disk and nothing is installed. --load-extension is refused
# too (Chrome 137+), including with DisableLoadExtensionCommandLineSwitch.
#
# What Chrome does still allow is CDP: Extensions.loadUnpacked, the call
# Playwright and Puppeteer use. It needs a debug port, so on Windows one is
# opened for every launch - see open_window.
#
# Deliberately hand-rolled rather than through playwright: a plain launch must
# not pay for the engine's dependencies (see the module docstring), and this is
# two frames on a loopback socket.


def _ws_frame(payload):
    """One masked text frame, which is the only kind a client may send."""
    data = payload.encode("utf-8")
    header = bytearray([0x81])          # FIN + text opcode
    mask = os.urandom(4)
    length = len(data)
    if length < 126:
        header.append(0x80 | length)
    elif length < (1 << 16):
        header.append(0x80 | 126)
        header += length.to_bytes(2, "big")
    else:
        header.append(0x80 | 127)
        header += length.to_bytes(8, "big")
    header += mask
    return bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data))


class _WebSocket:
    """The smallest websocket client that can carry CDP: text frames, no TLS."""

    def __init__(self, url, timeout=15):
        import socket
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        self._sock = socket.create_connection(
            (parts.hostname, parts.port or 80), timeout=timeout)
        self._sock.settimeout(timeout)
        self._buffer = b""
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = parts.path + ("?" + parts.query if parts.query else "")
        request = ("GET %s HTTP/1.1\r\n"
                   "Host: %s:%s\r\n"
                   "Upgrade: websocket\r\n"
                   "Connection: Upgrade\r\n"
                   "Sec-WebSocket-Key: %s\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n"
                   % (path, parts.hostname, parts.port or 80, key))
        self._sock.sendall(request.encode("ascii"))
        while b"\r\n\r\n" not in self._buffer:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise OSError("the debug port closed during the handshake")
            self._buffer += chunk
        head, _, rest = self._buffer.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n")[0]:
            raise OSError("the debug port refused the upgrade: %s"
                          % head.split(b"\r\n")[0].decode("latin-1"))
        self._buffer = rest
        self._next_id = 0

    def _recv_exactly(self, count):
        while len(self._buffer) < count:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise OSError("the debug port closed")
            self._buffer += chunk
        head, self._buffer = self._buffer[:count], self._buffer[count:]
        return head

    def _recv_frame(self):
        """One frame's (opcode, payload); server frames are never masked."""
        first, second = self._recv_exactly(2)
        length = second & 0x7F
        if length == 126:
            length = int.from_bytes(self._recv_exactly(2), "big")
        elif length == 127:
            length = int.from_bytes(self._recv_exactly(8), "big")
        if second & 0x80:                       # a server must not mask, but read it
            mask = self._recv_exactly(4)
            body = self._recv_exactly(length)
            body = bytes(b ^ mask[i % 4] for i, b in enumerate(body))
        else:
            body = self._recv_exactly(length)
        return first & 0x0F, body

    def call(self, method, params=None, timeout=30, session_id=None):
        """One CDP command, waiting past the events that arrive meanwhile.

        ``session_id`` addresses an attached target (flat mode), where it is a
        field of the message itself rather than one of the command's params.
        """
        self._next_id += 1
        message_id = self._next_id
        message = {"id": message_id, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        self._sock.sendall(_ws_frame(json.dumps(message)))
        deadline = time.time() + timeout
        payload = b""
        while time.time() < deadline:
            opcode, body = self._recv_frame()
            if opcode == 0x8:                   # close
                raise OSError("the debug port closed during %s" % method)
            if opcode == 0x9:                   # ping; nothing here needs a pong
                continue
            payload = payload + body if opcode == 0x0 else body
            try:
                message = json.loads(payload)
            except ValueError:
                continue                        # a fragment; wait for the rest
            payload = b""
            if message.get("id") != message_id:
                continue                        # an event, or another command
            if "error" in message:
                raise OSError("%s: %s" % (method, message["error"].get("message")))
            return message.get("result", {})
        raise OSError("%s did not answer in %ds" % (method, timeout))

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


def devtools_endpoint(profile, timeout=20):
    """The browser's ws:// endpoint, once Chrome has written its port.

    Chrome writes DevToolsActivePort only after the browser is up, so waiting
    for it is also how long a window takes to become addressable at all.
    """
    from urllib.request import urlopen
    port_file = os.path.join(profile, "DevToolsActivePort")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(port_file, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            if lines and lines[0].strip():
                port = int(lines[0].strip())
                url = "http://127.0.0.1:%d/json/version" % port
                with urlopen(url, timeout=5) as answer:
                    return json.loads(answer.read().decode("utf-8"))["webSocketDebuggerUrl"]
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(0.2)
    raise OSError("Chrome never reported a debug port for %s"
                  % os.path.basename(profile))


def load_extensions_over_cdp(profile, directories, timeout=20):
    """Load already-planted extension directories into a running Chrome.

    Returns the ids Chrome accepted. Every failure is survivable and logged:
    an extension that will not load must never stop a login.
    """
    if not directories:
        return []
    connection = _WebSocket(devtools_endpoint(profile, timeout=timeout))
    loaded = []
    try:
        for directory in directories:
            try:
                result = connection.call("Extensions.loadUnpacked", {"path": directory})
            except OSError as exc:
                log.warning("Chrome would not load %s: %s",
                            os.path.basename(directory), exc)
                continue
            if result.get("id"):
                loaded.append(result["id"])
        if loaded:
            # The tab is already sitting on the login form, and a content script
            # only runs on a document loaded after its extension. Without this
            # the auto-login extension arrives one navigation too late.
            _reload_first_page(connection)
    finally:
        connection.close()
    return loaded


def _reload_first_page(connection):
    """Reload the window's single tab, over the browser-level connection."""
    for target in connection.call("Target.getTargets").get("targetInfos", []):
        if target.get("type") != "page":
            continue
        session = connection.call("Target.attachToTarget",
                                  {"targetId": target["targetId"], "flatten": True})
        connection.call("Page.reload", session_id=session.get("sessionId"))
        return


# Chrome Web Store extensions that can be installed into every profile, by name.
# Nothing here is installed unless --extensions asks for it: an automated profile
# should carry what the run needs and nothing else, and each entry costs a fetch
# from Google on first use.
#
# A raw 32-character Web Store id is accepted too, so a fork can install any
# extension without editing this table.
#
# Entries are just convenience aliases for store ids; see extensions/README.md for
# what ships in this checkout and under whose licence.
KNOWN_EXTENSIONS = {
    "odoo_debug": "hmdmhilocobgohohpdpolmibjklfgkbi",
}

# Web Store ids are 32 chars from a-p (a base-16 alphabet shifted into letters).
_EXT_ID_RE = re.compile(r"^[a-p]{32}$")


# One resolved --extensions entry. kind is "local" (value = source directory) or
# "store" (value = a Chrome Web Store id to download).
Extension = collections.namedtuple("Extension", "name kind value")


def local_extensions(extensions_dir=None):
    """Unpacked extensions vendored in the project: {name: source directory}.

    A directory under extensions/ counts only if it has a manifest.json, so notes,
    README files and scratch folders sitting alongside are ignored.
    """
    root = extensions_dir or EXTENSIONS_DIR
    found = {}
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return found
    for name in names:
        if name.startswith("_"):
            continue        # machinery (e.g. _autologin), not user-installable
        path = os.path.join(root, name)
        if os.path.isfile(os.path.join(path, "manifest.json")):
            found[name] = path
    return found


def validate_local_extension(path):
    """(ok, reason) for an unpacked extension directory.

    Cheap, static checks only - enough to catch a truncated download, a stray
    directory or a manifest Chrome will refuse, so one broken extension is skipped
    with a warning instead of taking the whole launch down.
    """
    manifest_path = os.path.join(path, "manifest.json")
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except ValueError as exc:
        return False, "manifest.json is not valid JSON (%s)" % exc
    except OSError as exc:
        return False, "manifest.json is unreadable (%s)" % exc
    if not isinstance(manifest, dict):
        return False, "manifest.json is not a JSON object"
    missing = [k for k in ("manifest_version", "name", "version") if k not in manifest]
    if missing:
        return False, "manifest.json is missing %s" % ", ".join(missing)
    if manifest.get("manifest_version") not in (2, 3):
        return False, ("manifest_version %r is not supported by Chrome"
                       % manifest.get("manifest_version"))
    for entry in manifest.get("content_scripts") or []:
        for js in entry.get("js") or []:
            if not os.path.isfile(os.path.join(path, js)):
                return False, "content script %r is missing" % js
    return True, ""


def default_extensions(extensions_dir=None):
    """Every usable unpacked extension in the project - what a plain launch installs.

    Broken ones are dropped with a warning rather than failing the launch: a bad
    extension should cost you that extension, not your windows.
    """
    chosen = []
    for name, path in sorted(local_extensions(extensions_dir).items()):
        ok, reason = validate_local_extension(path)
        if ok:
            chosen.append(Extension(name, "local", path))
        else:
            log.warning("Skipping extension %s: %s (%s)", name, reason, path)
    return chosen


def format_extensions(extensions_dir=None):
    """The --extensions=list output: what is known, and how to use anything else."""
    local = local_extensions(extensions_dir)
    lines = []
    if local:
        lines.append("Unpacked extensions in this project (installed with no network):")
        width = max(len(n) for n in local)
        for name in sorted(local):
            lines.append("  %-*s  %s" % (width, name, local[name]))
        lines.append("")
    lines.append("Extensions fetched from the Chrome Web Store by name:")
    width = max((len(n) for n in KNOWN_EXTENSIONS), default=0)
    for name in sorted(KNOWN_EXTENSIONS):
        marker = "  (overridden by the local copy above)" if name in local else ""
        lines.append("  %-*s  %s%s" % (width, name, KNOWN_EXTENSIONS[name], marker))
    if not KNOWN_EXTENSIONS:
        lines.append("  (none)")
    lines += [
        "",
        "Without --extensions, EVERY usable extension above is installed. One that is",
        "broken or unsupported is skipped with a warning; the launch continues.",
        "  --extensions=none        install nothing",
        "  --extensions=all         the default set, stated explicitly",
        "  --extensions=a,b         exactly these",
        "",
        "To add your own: drop an unpacked extension in extensions/<name>/ (it needs a",
        "manifest.json). Nothing is downloaded, and the source is re-copied into each",
        "profile on every launch, so edits take effect.",
        "",
        "",
        "Any other extension: pass its 32-character Chrome Web Store id directly -",
        "no code change needed. The id is the last part of the store URL:",
        "  https://chromewebstore.google.com/detail/<slug>/hmdmhilocobgohohpdpolmibjklfgkbi",
        "                                                 ^--------- this ---------^",
        "  --extensions=hmdmhilocobgohohpdpolmibjklfgkbi",
        "",
        "You can also give an id a name inline, which is what the cache file and the",
        "log lines are called:",
        "  --extensions=react_devtools=fmkadmapgofadopljbjfkapdkoienihi",
        "",
        "To add a name permanently, put it in KNOWN_EXTENSIONS in session_launcher.py.",
    ]
    return "\n".join(lines)


def resolve_extension(entry, extensions_dir=None):
    """Map one --extensions entry to (name, webstore_id), or exit with the known list.

    Accepts, in order: "name=id" (name it inline), a friendly name from
    KNOWN_EXTENSIONS, or a raw Web Store id. The inline and raw forms mean a fork
    can install anything without editing this file.
    """
    key = entry.strip()
    if "=" in key:                       # name=id: self-documenting, no registry edit
        name, _sep, ext_id = key.partition("=")
        name, ext_id = name.strip(), ext_id.strip()
        if not name or not _EXT_ID_RE.match(ext_id):
            sys.exit("--extensions: %r is not name=<32-char store id>.\n%s"
                     % (entry, format_extensions(extensions_dir)))
        return Extension(name, "store", ext_id)
    local = local_extensions(extensions_dir)
    if key in local:
        # A local copy wins over the same name in the store table: that is how you
        # pin or patch a store extension without renaming every command that uses it.
        return Extension(key, "local", local[key])
    if key in KNOWN_EXTENSIONS:
        return Extension(key, "store", KNOWN_EXTENSIONS[key])
    if _EXT_ID_RE.match(key):
        return Extension(key, "store", key)   # raw id: cache file named after the id
    sys.exit("--extensions: unknown extension %r.\n%s"
             % (entry, format_extensions(extensions_dir)))


def _chrome_prodversion(chrome):
    """Best-effort Chrome major.minor for the CRX update query; falls back high.

    Through the same banner as everything else, so that asking for it cannot
    open a browser window on Windows (see _chrome_version_string).
    """
    match = re.search(r"(\d+\.\d+)", _chrome_version_string(chrome) or "")
    return match.group(1) if match else "150.0"


def download_crx(dest_path, ext_id, prodversion):
    """Download an extension's CRX from Chrome's update service to dest_path (cached).

    Skips the download when the file already exists, so the network is hit once per
    sessions dir. Raises on failure so the caller can warn and carry on without it.
    """
    if os.path.exists(dest_path):
        return dest_path
    from urllib.request import urlopen, Request
    url = ("https://clients2.google.com/service/update2/crx?response=redirect"
           "&acceptformat=crx2,crx3&prodversion=%s"
           "&x=id%%3D%s%%26installsource%%3Dondemand%%26uc" % (prodversion, ext_id))
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=60) as resp:
        data = resp.read()
    if data[:4] != b"Cr24":
        raise ValueError("update service did not return a CRX (%d bytes)" % len(data))
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    tmp = dest_path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, dest_path)
    return dest_path


def _crx_zip_bytes(crx):
    """Strip the CRX2/CRX3 header, returning the embedded ZIP bytes."""
    if crx[:4] != b"Cr24":
        raise ValueError("not a CRX file")
    version = int.from_bytes(crx[4:8], "little")
    if version == 3:
        header_len = int.from_bytes(crx[8:12], "little")
        return crx[12 + header_len:]
    if version == 2:
        pk_len = int.from_bytes(crx[8:12], "little")
        sig_len = int.from_bytes(crx[12:16], "little")
        return crx[16 + pk_len + sig_len:]
    raise ValueError("unsupported CRX version %d" % version)


def install_crx_extension(profile, crx_path, origin, key_file_name):
    """Install a downloaded CRX into the profile as an enabled, silent local extension.

    Chrome will not silently enable an extension that looks like it came from the Web
    Store or asks for broad host access, so before registering it we rewrite three
    things in its manifest: (1) re-key it under our own key -> a plain non-store id
    Chrome won't flag as a remote/webstore install, (2) drop its "update_url" (same
    reason, and it stops Chrome auto-updating it back to the store build), and
    (3) narrow any "<all_urls>" content-script / web-accessible matches to this
    session's origin, so it needs only that one host permission. Granted permissions
    are taken from the rewritten manifest, so there is no pending permission prompt.
    Uses the same profile-Preferences mechanism as install_autologin_extension.
    """
    import io
    import zipfile
    ext_root = os.path.join(profile, "Default", "Extensions")
    os.makedirs(ext_root, exist_ok=True)
    key_b64 = _profile_extension_key(ext_root, key_file_name)
    ext_id = _extension_id_from_key(base64.b64decode(key_b64))
    with open(crx_path, "rb") as fh:
        zf = zipfile.ZipFile(io.BytesIO(_crx_zip_bytes(fh.read())))
    manifest = json.loads(zf.read("manifest.json"))
    version = manifest.get("version", "0")
    version_dir = os.path.join(ext_root, ext_id, version + "_0")
    if not os.path.isdir(version_dir):  # unzip once; manifest is rewritten every run
        os.makedirs(version_dir)
        zf.extractall(version_dir)

    _register_extension(profile, ext_id, version_dir, version, manifest, key_b64, origin)
    return version_dir


def _register_extension(profile, ext_id, version_dir, version, manifest, key_b64, origin):
    """Re-key a third-party manifest, narrow it to this origin, and enable it.

    Shared by the CRX and local-source installers. Chrome will not silently enable
    an extension that looks like a Web Store install or asks for broad host access,
    so: re-key it under our own key (a plain non-store id), drop "update_url" (stops
    Chrome updating it back to the store build), and narrow any "<all_urls>" content
    script / web-accessible match down to this session's origin. Granted permissions
    are taken from the rewritten manifest, so nothing is left pending a prompt.
    """
    host = origin + "/*"
    manifest["key"] = key_b64
    manifest.pop("update_url", None)
    has_content_script = bool(manifest.get("content_scripts"))
    for cs in manifest.get("content_scripts", []):
        cs["matches"] = [host]
    for war in manifest.get("web_accessible_resources", []):
        if isinstance(war, dict) and "matches" in war:
            war["matches"] = [host]
    with open(os.path.join(version_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)

    perms = {
        "api": manifest.get("permissions", []),
        "explicit_host": [host] if manifest.get("host_permissions") else [],
        "manifest_permissions": [],
        "scriptable_host": [host] if has_content_script else [],
    }
    entry = {
        "active_permissions": perms,
        "granted_permissions": perms,
        "location": 1,  # INTERNAL (see install_autologin_extension)
        "state": 1,
        "from_webstore": False,
        "was_installed_by_default": False,
        "was_installed_by_oem": False,
        "path": "%s/%s_0" % (ext_id, version),
        "manifest": manifest,
    }
    _merge_extension_pref(profile, ext_id, entry)


def install_local_extension(profile, src_dir, origin, key_file_name):
    """Install an UNPACKED extension from a source directory in this project.

    The local counterpart of install_crx_extension: no Web Store, no network, and
    the source is yours to edit. Files are re-copied on every launch so editing the
    source and relaunching picks the change up, rather than silently reusing a stale
    copy planted under a version that did not change.
    """
    with open(os.path.join(src_dir, "manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    ext_root = os.path.join(profile, "Default", "Extensions")
    os.makedirs(ext_root, exist_ok=True)
    key_b64 = _profile_extension_key(ext_root, key_file_name)
    ext_id = _extension_id_from_key(base64.b64decode(key_b64))
    version = str(manifest.get("version", "0"))
    version_dir = os.path.join(ext_root, ext_id, version + "_0")
    if os.path.isdir(version_dir):
        shutil.rmtree(version_dir, ignore_errors=True)
    shutil.copytree(src_dir, version_dir)
    _register_extension(profile, ext_id, version_dir, version, manifest, key_b64, origin)
    return version_dir


def read_commands(windows_by_session):
    """Take one JSON command per stdin line, for as long as stdin is open.

    The other half of ``--events``: facts go out on stdout, instructions come in
    here. Opt-in through ``--control=-`` because a reader on stdin would
    otherwise swallow the keystrokes of anyone running this in a terminal.

    Four commands. ``stop-session`` does both halves of what stopping one
    window means: tell the engine to stop driving it (so it stops issuing steps
    rather than having them fail against a browser being torn down), then close
    the window itself. ``service.result`` is the answer to a ``service.request``
    event - the scenario asked the GUI to start or watch a service and a session
    thread is blocked waiting for this. ``ask.result`` is the same thing for a
    ``cycle.ask`` event - a person's answer to an approval gate. ``cycle.stop``
    asks a running cycle to
    stop, by the run id it announced. Unknown commands are ignored rather than
    fatal: a newer GUI talking to an older core should lose a feature, not the run.

    ``windows_by_session`` is ``{}`` in cycle mode, where there are no windows of
    this process's own - ``stop-session`` then simply finds nothing to close,
    which is the correct answer rather than a special case.
    """
    from engine import runner as engine_runner
    from engine import services as engine_services
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            log.warning("Ignoring an unreadable control line: %.80s", line)
            continue
        command = message.get("command")
        session = message.get("session")
        if command == "stop-session" and session:
            engine_runner.request_session_stop(session)
            _emit("session.stopping", session=session)
            proc = windows_by_session.get(session)
            if proc is not None:
                close_all([(session, proc)])
        elif command == "service.result":
            engine_services.deliver(message.get("id"), ok=message.get("ok"),
                                    status=message.get("status", ""),
                                    message=message.get("message", ""))
        elif command == "ask.result":
            # The same pipe as service.result, carrying a person's answer
            # instead of a supervisor's. Imported here for the same reason the
            # cycle imports below are: a scenario run never asks.
            from cycle import ask as cycle_ask
            cycle_ask.deliver(message.get("id"), answer=message.get("answer"),
                              who=message.get("who", ""),
                              revise=message.get("revise") is True,
                              feedback=message.get("feedback", ""))
        elif command == "cycle.stop":
            # Imported here rather than at the top of the function: a scenario
            # run never reaches this branch and must not pay for the import.
            from cycle import executor as cycle_executor
            asked = cycle_executor.request_stop(message.get("run_id"))
            log.info("Stopping %s", message.get("run_id") or "every cycle run")
            if not asked:
                log.warning("No cycle run called %r is going here.",
                            message.get("run_id"))
        else:
            log.warning("Ignoring an unknown control command: %r", command)


def open_window(chrome, profile, cls, login, url, debug_port, spawn_kwargs,
                bind_lifetime=False):
    """Start one Chrome on one profile and announce it. Returns the Popen.

    Split out of the launch loop because a --run-tests run may open a window long
    after its profile was prepared - see :class:`WindowSource`.

    ``bind_lifetime`` ties the window to this launcher: on Windows it joins the
    kill-on-close job, so a hard kill takes the browsers with it. False for
    --detach, where the windows are meant to outlive us. On Linux the same tie is
    made by the child itself through preexec_fn, so this does nothing there.
    """
    clear_devtools_port(profile)  # so --run-tests cannot attach to a dead/reused port
    log.info("Launching %-8s (login: %s)", cls, login)
    chrome_args = [
        chrome,
        "--user-data-dir=%s" % profile,
        "--class=%s" % cls,
        # Linux only: basic store => cookies AND saved passwords use the same
        # deterministic key, so the seeded credential is decryptable. macOS keys
        # off the login Keychain instead (see _encrypt_password).
        *_PASSWORD_STORE_ARGS,
        # The auto-login extension is installed into the profile itself
        # (install_autologin_extension), so no --load-extension is needed -
        # recent Chrome (137+) blocks that command-line switch anyway.
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
    ]
    if debug_port:
        # Flow-execution and recording only: open a CDP endpoint so the engine
        # can attach and drive this window. Port 0 = auto-pick a free port;
        # Chrome writes the chosen port to <profile>/DevToolsActivePort. A plain
        # launch never gets this switch, so its behaviour is unchanged - and the
        # port is unauthenticated, so it is not something to open by default.
        chrome_args.append("--remote-debugging-port=0")
    chrome_args.append(url)
    proc = subprocess.Popen(
        chrome_args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # own process group so we can close the whole window tree later; also
        # detaches the window so --detach can leave it running. In foreground
        # mode this also arms PR_SET_PDEATHSIG (see spawn_kwargs above).
        **spawn_kwargs,
    )
    # The Windows half of the same promise: on Linux the child arms
    # PR_SET_PDEATHSIG for itself in preexec_fn, which Windows has no equivalent
    # of, so the parent puts it in a kill-on-close job instead. Passed in rather
    # than read off spawn_kwargs, which on Windows looks identical with and
    # without --detach and so cannot answer "should this outlive us?".
    if bind_lifetime:
        _adopt_into_windows_job(proc)
    _emit("window.launched", login=login, cls=cls, pid=proc.pid,
          profile=profile, session=os.path.basename(profile))
    return proc


class WindowSource:
    """Opens and closes one window at a time, on the runner's worker threads.

    This is what makes ``--jobs`` mean *windows* rather than merely drivers. The
    old arrangement opened every window up front and let the runner decide how
    many to step at once, so eight accounts meant eight resident browsers no
    matter what ``--jobs`` said, and throttling could not hand a byte back. Here
    the runner holds a slot, asks for a window, runs that session's scenarios,
    and gives the window back - so a ceiling the load governor lowers turns into
    a browser that is not running.

    Only used when the windows are going to be closed anyway (``--close-after``);
    without it the user asked to keep them for inspection and every one stays up.
    """

    def __init__(self, chrome, url, spawn_kwargs, procs, stagger_s=0.4,
                 by_session=None, bind_lifetime=True, extension_dirs=None,
                 log_hub=None, session_envs=None):
        self._extension_dirs = extension_dirs or {}
        # Staged windows open here rather than in the launch loop, so this is where
        # they have to be registered with the log hub - and the correlation window
        # has to start now, not when the profile was prepared minutes ago. The
        # session tuple carries the URL origin, not the config's env string that
        # logsources.json is keyed by, hence the profile -> env map.
        self._log_hub = log_hub
        self._session_envs = session_envs or {}
        self._chrome = chrome
        self._url = url
        self._spawn_kwargs = spawn_kwargs
        self._bind_lifetime = bind_lifetime
        self._procs = procs          # shared with close_all; appended under the lock
        self._names = {}             # proc -> cls, so close() can name what it closes
        self._by_session = by_session   # session name -> Popen, for --control
        self._stagger_s = stagger_s
        self._lock = threading.Lock()

    def open(self, session):
        cls, profile, login = session[0], session[2], session[3]
        # Serialised, with the same pause the sequential loop used: several Chrome
        # cold starts landing together is its own load spike, and one arriving
        # inside another's profile setup is how DevToolsActivePort races appear.
        with self._lock:
            proc = open_window(self._chrome, profile, cls, login, self._url,
                               debug_port=True, spawn_kwargs=self._spawn_kwargs,
                               bind_lifetime=self._bind_lifetime)
            self._procs.append((cls, proc))
            self._names[proc] = cls
            if EXTENSIONS_NEED_CDP:
                _install_extensions_for(profile, self._extension_dirs.get(profile))
            if self._by_session is not None:
                self._by_session[os.path.basename(profile)] = proc
            if self._log_hub is not None:
                self._log_hub.add_session(os.path.basename(profile),
                                          self._session_envs.get(profile, ""))
            time.sleep(self._stagger_s)
        return proc

    def close(self, proc):
        """Graceful shutdown, waited on, so the slot's memory is really back.

        close_all is reused rather than reimplemented: the SIGTERM-then-wait
        dance it does is what flushes the login session to disk, and a window
        closed any other way loses it.
        """
        close_all([(self._names.get(proc, "session"), proc)])


def _install_extensions_for(profile, directories):
    """Ask a just-opened Chrome to load this profile's extensions.

    Windows only, and never fatal: a window without its extensions is a window
    the user still has, and the auto-login they lose is one they can do by hand.
    """
    if not directories:
        return
    try:
        loaded = load_extensions_over_cdp(profile, directories)
    except OSError as exc:
        log.warning("Could not install extensions into %s: %s. The window is "
                    "open, but auto-login will not run in it.",
                    os.path.basename(profile), exc)
        return
    log.info("Installed %d extension(s) into %s.", len(loaded),
             os.path.basename(profile))


def force_kill(proc):
    """Kill a browser that ignored SIGTERM, and its helpers with it.

    On POSIX the whole process group goes: by this point Chrome has stopped
    managing its renderers, and killing the leader alone leaves them behind.

    ``os.killpg`` and ``os.getpgid`` do not exist on Windows at all, and the
    ``except`` here used to name only ProcessLookupError and PermissionError -
    so the AttributeError went straight up and took the teardown with it. That
    is reachable on any non-POSIX platform the moment one window outlasts the
    15-second grace period. On Windows the browser and its children are held in
    a job object instead (see ``_windows_kill_on_close_job``), so terminating
    the one process is enough; ``proc.kill`` is also the honest fallback where
    the group call is refused.
    """
    if hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


def close_all(procs):
    """Close every launched window gracefully so the login session is flushed to disk.

    SIGTERM goes to the browser process only (proc.pid), which lets Chrome run its normal
    shutdown: write session cookies to the Cookies DB and close its own child processes.
    Signalling the whole process group at once (killpg) instead looks like a crash to Chrome
    and it skips that flush, which loses the login. Only a hung browser is force-killed, and
    only then is the group nuked to sweep up any orphaned helpers.
    """
    for cls, proc in procs:
        if proc.poll() is not None:
            continue  # already closed (e.g. user shut this window manually)
        log.info("Closing %s...", cls)
        _emit("window.closing", cls=cls, pid=proc.pid)
        try:
            proc.terminate()  # SIGTERM to the browser -> graceful Chrome shutdown
        except ProcessLookupError:
            pass
    # Give Chrome time to persist cookies before force-killing anything still alive.
    for cls, proc in procs:
        try:
            proc.wait(timeout=15)
            _emit("window.exited", cls=cls, pid=proc.pid, returncode=proc.returncode)
        except subprocess.TimeoutExpired:
            _emit("window.killed", cls=cls, pid=proc.pid)
            force_kill(proc)


def keep_open_until_closed(procs):
    """Block until every window is closed manually or a shutdown signal arrives.

    The VS Code "Stop" button (and any `kill` without CTRL+C) sends SIGTERM, not
    SIGINT, so the KeyboardInterrupt path below would never run and the windows
    would be orphaned (they launch in their own session, detached from us). Raise
    KeyboardInterrupt on SIGTERM too, so Stop cleans up exactly like CTRL+C does.
    """
    def _on_sigterm(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _on_sigterm)

    log.info("Windows are tracked in this session. Press CTRL+C (or Stop) to close all of them.")
    try:
        # poll() is already called every half-second here, so noticing a window
        # that went away costs nothing extra beyond remembering which ones we
        # have reported.
        reported = set()
        while any(proc.poll() is None for _cls, proc in procs):
            for cls, proc in procs:
                code = proc.poll()
                if code is not None and proc.pid not in reported:
                    reported.add(proc.pid)
                    _emit("window.exited", cls=cls, pid=proc.pid, returncode=code)
            time.sleep(0.5)
        for cls, proc in procs:
            if proc.pid not in reported:
                _emit("window.exited", cls=cls, pid=proc.pid, returncode=proc.poll())
        log.info("All windows were closed manually.")
    except KeyboardInterrupt:
        log.info("Shutdown signal received - closing all windows...")
        close_all(procs)
        log.info("Done.")


def _needing_a_browser(run_tests, users, flows_dir=None):
    """``[(scenario id, [actions])]``: what stops this run going without a browser.

    The scenarios are resolved the way the runner will resolve them - ``config`` is
    the selected users' own lists, anything else is the --run-tests value - and each
    is read with its use: blocks expanded. One that cannot be read at all is left
    to the runner, which reports it as the compile error it is, exactly as it would
    on a run with a browser.
    """
    from engine import compiler
    from engine.runner import _resolve_scenarios
    if run_tests == "config":
        wanted = [test for user in users for test in user.tests]
        scenario_ids = _resolve_scenarios(wanted, flows_dir)
    else:
        scenario_ids = _resolve_scenarios(run_tests, flows_dir)
    refused = []
    for scenario_id in scenario_ids:
        try:
            actions = compiler.browser_actions(scenario_id, flows_dir)
        except Exception:  # noqa: BLE001 - see above: the runner reports it
            continue
        if actions:
            refused.append((scenario_id, actions))
    return refused


def _print_help():
    """Print every command-line parameter with a one-line description and exit 0."""
    prog = os.path.basename(sys.argv[0])
    print("""\
Usage: %s [OPTIONS] (--env=NAME | --url=URL | <URL>)

Launch one Chrome window per configured user, each auto-logged-in via a
per-profile extension. With --run-tests it also drives flows and writes reports.

Options:
  --env=NAME                Launch one environment: matches the config's "env"
                            field by short name (--env=dev), and supplies that
                            environment's URL, so --url becomes optional.
                            Default: every environment.
  --url=URL                 Login URL (or pass it as a positional argument);
                            overrides the URL --env would supply.
  --user-session=PREFIX     Folder-name prefix for this run's profile dirs (keeps
                            separate sessions isolated); overrides the env.
  --sessions-dir=DIR        Where to store profiles + generated extensions
                            (default: user_sessions/ next to the script).
  --config=FILE             Path to the users JSON config (default: users.json).
  --init-users-json         Write a starter users.json (at --config's path) and
                            exit; never overwrites an existing file.
  --filter-users=LIST       Launch only these logins, comma-separated
                            (default: all).
  --user=LOGIN              Launch a single user instead of the config list; its
                            password comes from the config (or its environment)
                            unless --password is given.
  --password=PASS           Password to use, overriding the config's. With --user
                            it applies to that user; alone, to every selected one.
  --detach                  Fire-and-forget: leave windows running after exit.
  --extensions=LIST         Which extensions to install into every profile.
                            Default (no flag): every usable one in extensions/.
                            LIST is comma-separated names, raw 32-char store ids
                            or name=id; "none" installs nothing, "all" is the
                            default set, "list" prints what is available.
  --log-level=LEVEL         DEBUG/INFO/WARNING/ERROR (default: INFO; also via
                            the OPEN_USERS_LOG_LEVEL env var).
  --control=-               Accept JSON commands on stdin, one per line - for a
                            program driving this launcher. Currently
                            {"command":"stop-session","session":"NAME"}, which
                            stops driving that window and closes it while the
                            other windows carry on. Off by default: a reader on
                            stdin would eat a terminal user's keystrokes.
  --events=-|FILE           Emit a structured JSONL event stream for another
                            program (the GUI) to follow: windows launched, CDP
                            attached, every step, artifacts written, run summary.
                            "-" is stdout - log records go to stderr, so the two
                            never mix; anything else is a file to append to.
  --server-log=LIST         Stream the backend's own log next to the windows, each
                            session shown only the lines written after its window
                            opened. Which logs exist is described in
                            logsources.json (connections + log files); LIST is
                            comma-separated names, "all", "none", or "list" to
                            print what is configured. The bare flag - spelled
                            "default" when a value is needed - takes the logs
                            marked "default" for the environment being launched.
                            Works with or without --run-tests. Under --run-tests it
                            also writes each scenario's own slice of every streamed
                            log to reports/<run>/<session>/<scenario>/
                            server_log-NAME.log - always, pass or fail.
  --version, -V             Print the version and exit.
  --help, -h                Show this help and exit.

Editing scenarios (answer with JSON on stdout, then exit):
  --flow-show=ID            One scenario as JSON: its text, its parsed steps,
                            whether it can be edited, and anything it references
                            that does not resolve here.
  --flow-save=ID --from=F   Write a scenario from the JSON document in file F -
                            either {"yaml": "..."} or {"meta": {...},
                            "steps": [...]}. Validated by compiling it first;
                            nothing is written unless it compiles.
  --flow-delete=ID          Delete a scenario. Refuses the ones that ship with
                            the application - duplicate those instead.
  --flow-import=FILE        Copy a scenario file in, validating it first.
  --server-log-show=NAME    Read that configured log and answer JSON: its lines,
                            or the error. The last 500 by default;
                            --server-log-lines=N|all changes that. Also how you
                            find out an ssh host or a container works, before a
                            run depends on it.
  --log-sources=FILE        Where logsources.json is (default: beside the rest of
                            the config). Applies to every --server-log* flag and
                            to --describe, so the file you edit and the file a
                            run reads are the same one.
  --selectors-show          The named-target map as JSON: every name, what it
                            resolves to, and whether it is yours or the app's.
  --selectors-save --from=F Replace your selectors.yaml from the JSON document
                            in file F ({"yaml": "..."}). Names of your own
                            override the ones that ship with the application.

Cycles - one graph of steps: start a service, run a scenario, run a command,
write a report. The steps that do not depend on each other run at the same time.
  --cycle-list              Every cycle as JSON: its name, how many steps, and
                            anything wrong with it.
  --cycle-show=ID           One cycle as JSON: its text, the graph its steps
                            make, and its problems.
  --cycle-save=ID --from=F  Write a cycle from the JSON document in file F -
                            either {"yaml": "..."} or the document itself.
                            Validated first; nothing is written unless it holds.
  --cycle-delete=ID         Delete a cycle. Refuses the ones that ship with the
                            application - duplicate those instead.
  --cycle-import=FILE       Copy a cycle file in, validating it first.
  --cycles-dir=DIR          Where cycles live (default: beside the rest of your
                            data, then the ones that ship with the app).
  --cycle-secret-list=ID    Which of a cycle's secret variables have a value.
                            Names only - a value is never printed.
  --cycle-secret-set=ID:N --from=F
                            Set one secret from the JSON {"value": "..."} in
                            file F. The value is not on the command line: ps
                            shows that to everyone on the machine.
  --cycle-secret-copy=FROM:TO
                            Give cycle TO the secrets cycle FROM has, for the
                            ordinary case of one credential wanted by several
                            cycles. The value is decrypted and re-encrypted
                            inside the store - it is never printed and never on
                            a command line. A secret TO already has is left
                            alone; delete it first to replace it.
  --cycle-secret-delete=ID[:N]
                            Forget one secret, or every secret of one cycle.
  --cycle-secrets-file=PATH Where the encrypted store lives (default: beside
                            the rest of the user data).

  --cycle-memory-list[=P]   What the project remembers, as keys. P narrows to
                            keys starting with it.
  --cycle-memory-show=KEY   One record: what is remembered, when, and who holds
                            it right now.
  --cycle-memory-forget=KEY Forget one record. A record that is wrong is worse
                            than none - a cycle believing a task is done will
                            not do it again.
  --cycle-memory-file=PATH  Where the store lives (default: beside the rest of
                            the user data). Plain JSON, meant to be readable.
  --cycle-runs-dir=PATH     Where cycle runs are written and read back - by a
                            run, a resume and the session list alike (default:
                            cycle-runs under the user data).

  --cycle-watch=ID[:TRIGGER]
                            What is new in the queue a cycle's trigger watches,
                            since it last looked. The first check only
                            remembers what is there. Starts nothing.
  --cycle-watch-seen=ID[:TRIGGER]:KEY
                            Do not offer KEY again, and take it off the plan.
  --cycle-watch-plan=ID[:TRIGGER]:KEY --from=F
                            Keep KEY to look at later; F is {"title", "url"}.
  --cycle-watch-planned     Everything on the plan, across cycles, as JSON: new
                            tasks kept for later, and approvals a run stopped at
                            because nobody answered.
  --cycle-plan-remove=ID    Take one entry off the plan.

  --cycle-sessions[=ID]     What the cycles have been working on: every run of
                            one cycle on one subject, grouped, newest first.
                            ID narrows to one cycle. A cycle with no subject
                            gives one session per run.
  --cycle-session-delete=S  Delete one session: its run directories, their
                            index rows and the memory record its subject
                            names. Never a git branch or a Jira issue. Refused
                            while one of its runs is going.

  --cycle-plugin-action=P:A Ask a plugin something outside a run, as JSON: is it
                            signed in, can it reach its provider. --from=FILE
                            carries the step's settings. What a plugin can be
                            asked is in --describe under cycle_plugins.actions,
                            so this flag never grows a case per plugin.
                            Example: agent.review has sign_in_status and
                            sign_in, because an agent.review step using
                            framework=claude_cli needs no API key at all - it
                            uses the Claude Code CLI's own browser sign-in.

Running a cycle:
  --cycle-run=ID            Run it and exit with 0 if every step passed. Opens
                            no browser of its own: a scenario.run step opens
                            what it needs when it gets there. Cannot be combined
                            with --run-tests or --recorder.
  --cycle-jobs=N            How many steps may run at once (default: 4).
  --cycle-var=NAME=VALUE    Override one of the cycle's variables. Repeatable.
  --cycle-resume=RUN_ID     Continue this saved run in its workspace. Keep verified
                            completed results and retry unfinished work. Refuse
                            changed completed inputs or uncertain external calls.
  --cycle-reuse=RUN_ID      Pin the source for --cycle-only/--cycle-from to this
                            run instead of whichever run happened most recently.
  --cycle-only=ID[,ID]      Run only these steps. Everything else is taken from
                            the most recent run of this cycle - its outputs go
                            into scope, so a step that reads another's result
                            still finds one - rather than being run again. For
                            working on one part of a cycle without paying for
                            the rest of it.
  --cycle-from=ID           The same, for this step and everything that waits
                            on it: the plan was fine, redo it from here.
  --control=-               Accept commands on stdin, which is what lets a cycle
                            start and stop the services the GUI manages, and
                            what makes {"command": "cycle.stop"} work.

  Example:
    --cycle-run=nightly --cycle-var=branch=main --events=- --cycle-jobs=4

Recording:
  --recorder                Open ONE window with the Scenario Recorder shown in
                            it. Nothing is recorded until you ask: Capture Step
                            (or F2) picks one element and one action at a time,
                            and Finish writes the scenario.
                            --recorder=ID continues an existing scenario or names
                            a new one; the default is a timestamp.
                            Records one account at a time - you can only be
                            clicking in one window anyway - so select a single
                            user with --user=LOGIN or --filter-users=LOGIN.

Flow execution (require --run-tests):
  --run-tests=LIST          Attach over CDP, run scenarios, write reports, then
                            exit (0 = all passed). Values: all, config (each user
                            runs its own "tests" field from the config), or a
                            comma-separated list of ids / tag:NAME.
  --execution-overlay=LIST  In-page execution HUD; components tree,progress,
                            status,logs,highlight (or all). highlight flashes a
                            box on each clicked/typed/pressed element.
  --flows-dir=DIR           Where scenarios/blocks/selectors.yaml live (default:
                            flows/ next to the script). Lets the flows live in
                            their own repository. Taken literally: this becomes
                            the ONLY tree, so the blocks and selectors.yaml your
                            scenarios reference have to be in it too - nothing
                            that ships with the application is searched behind
                            it. Needs no --run-tests: it applies to --describe
                            and the --flow-* commands as well, which is how the
                            GUI edits a tree of your own.
  --reports-dir=DIR         Where run artifacts are written (default: reports/).
  --jobs=N|all|auto         Drive N windows at once (default 1 = one after
                            another); extras queue for a free slot. Scenarios
                            within a window always run in order. A number is
                            kept: nothing raises or lowers it. "auto" starts at
                            one window per core and lets the machine decide -
                            fewer while memory is under real pressure, back up
                            again once it is not. With --close-after this also
                            caps how many windows are OPEN at once, launched a
                            slot at a time and closed as they finish; "auto"
                            needs --close-after to have anything to give back.
  --close-after             Close the browser windows once the run finishes;
                            without it they stay open for inspection - which
                            also means every window is launched up front.
  --no-browser              Run with no browser at all, for scenarios made only
                            of service steps and assert_host_up (use: blocks
                            included). Any other scenario is refused before the
                            run starts. Sessions, --jobs and the load governor
                            work as usual; the launcher exits when the run ends.
                            Service steps need the GUI (--control=-).

Reports (require --run-tests):
  --report-level=LIST       What artifacts to generate from the browser:
                            console,dom,result,screen,url (default: result on
                            success, the full bundle on failure). Backend logs are
                            not in here - --server-log decides those.
  --report-always           Produce a full report on success too, not only
                            on failure.
  --report-screen=LIST      When to capture screenshots: start,each,finish
                            (only when 'screen' is in --report-level).

Example:
  %s --user=admin --password=admin --url=http://localhost:8069 \\
    --run-tests=my_scenario --report-level=result,screen --report-screen=start,finish\
""" % (prog, prog))
    sys.exit(0)


def main():
    # An installed build has no checkout to fall back on, so the user's directories
    # and a starter users.json have to exist before anything reads them. A no-op in
    # a source checkout - see runtime_paths.ensure_user_data_root.
    runtime_paths.ensure_user_data_root()
    detach = False
    session_prefix = ""
    url = ""
    sessions_dir = DEFAULT_SESSIONS_DIR
    config_path = DEFAULT_CONFIG
    cli_user = None
    cli_password = None
    init_config = False
    describe_json = False # --describe: print the JSON inventory and exit
    flow_command = None   # ("show"|"save"|"delete"|"import", id or path); JSON, then exit
    flow_source = None    # --from=FILE: the JSON document --flow-save writes
    cycle_command = None  # ("show"|"save"|"delete"|"import"|"list", id or path)
    cycle_run = None      # --cycle-run=ID: a run mode, not a JSON command
    cycle_only = []       # --cycle-only: run just these, reuse the rest
    cycle_from = ""       # --cycle-from: run this one and everything after
    cycle_resume = ""     # --cycle-resume: exact saved execution, not the latest
    cycle_reuse = ""      # --cycle-reuse: exact source for a partial execution
    plugin_action = None  # --cycle-plugin-action=PLUGIN:ACTION: JSON, then exit
    cycle_vars = {}       # --cycle-var=NAME=VALUE, repeatable
    cycle_jobs = None     # --cycle-jobs: steps at once (None = the engine default)
    cycles_dir = None     # --cycles-dir: where cycles live (None = the default)
    secret_command = None # --cycle-secret-list/set/delete: JSON, then exit
    memory_command = None # --cycle-memory-list/show/forget: JSON, then exit
    memory_file = None    # --cycle-memory-file: the store (None = the default)
    session_command = None  # --cycle-sessions / --cycle-session-delete: JSON, then exit
    watch_command = None  # --cycle-watch / --cycle-watch-seen: JSON, then exit
    secrets_file = None   # --cycle-secrets-file: the store (None = the default)
    users_filter = None   # None = launch every user in the config
    env_name = None       # --env=NAME; None = every environment
    recorder = None       # --recorder: record a scenario from a live window
    run_tests = None      # None = just launch; "all" or [ids] = run flows then exit
    jobs = 1              # --jobs: windows at once (1 = one after another)
    flows_dir = None      # --flows-dir: where scenarios live (None = the engine default)
    reports_dir = None    # --reports-dir: where run artifacts are written
    jobs_given = False
    overlay_components = None  # None = no HUD; else list of --execution-overlay components
    close_after = False   # with --run-tests: close windows after the run (default: keep open)
    no_browser = False    # --no-browser: service-only scenarios, no Chrome at all
    report_level = None   # raw --report-level value (None = flag absent -> default artifacts)
    report_always = False # --report-always: produce a full report on success too
    report_screen = None  # raw --report-screen value (None = flag absent -> default capture)
    extensions = None     # --extensions: None = the default set (every local one)
    extensions_given = False
    excluded_extensions = set()   # from the deprecated --no-odoo-debug
    legacy_odoo_flag = None   # --odoo-debug / --no-odoo-debug, reported once after parsing
    events_target = None  # --events: "-" (stdout) or a file path; None = off
    control_stdin = False # --control=-: accept commands (stop one session) on stdin
    server_log = None     # --server-log value: None = the logs marked "default",
                          # "all"/"none", or a list of names. Meaningless unless:
    server_log_given = False   # ...the flag was actually passed.
    server_log_show = None     # --server-log-show=NAME: the log as JSON, then exit
    server_log_lines = None    # --server-log-lines: how many from the end
    log_level = os.environ.get("OPEN_USERS_LOG_LEVEL", "INFO")
    bad_option = None     # unusable option we saw, reported once argv is fully parsed
    positional = []
    # Read ahead for this one, because --server-log=list answers and exits from
    # inside the loop below: parsed in order, "--server-log=list --log-sources=X"
    # would have listed the wrong file. Everything else that reads logsources.json
    # waits until argv is fully parsed and does not care.
    log_sources_path = None
    for arg in sys.argv[1:]:
        if arg.startswith("--log-sources="):
            log_sources_path = os.path.abspath(
                os.path.expanduser(arg.split("=", 1)[1].strip()))
    for arg in sys.argv[1:]:
        if arg in ("--help", "-h"):
            _print_help()   # prints all parameters and exits 0
        elif arg in ("--version", "-V"):
            print("qavector %s" % version())
            sys.exit(0)
        elif arg == "--detach":
            detach = True
        elif arg == "--close-after":
            # Flow-execution mode only: after the run finishes (report + screenshots
            # written), close every window automatically. Without it the windows stay
            # open for inspection. Ignored outside --run-tests (checked after the loop).
            close_after = True
        elif arg == "--no-browser":
            # Flow execution with no Chrome at all, for scenarios made only of
            # service steps and assert_host_up. Checked after the loop: it needs
            # --run-tests, and every scenario is vetted before anything runs.
            no_browser = True
        elif arg.startswith("--extensions="):
            # Comma-separated friendly names (see KNOWN_EXTENSIONS) or raw Web Store
            # ids. Nothing is installed by default.
            # Not parse_filter_list: that flag's "empty means all" wording is wrong
            # here - omitting --extensions installs nothing, and there is no "all".
            names = [n.strip() for n in arg.split("=", 1)[1].split(",") if n.strip()]
            if names == ["list"]:
                print(format_extensions())
                sys.exit(0)
            if names == ["none"]:
                extensions = []          # explicit: install nothing
                extensions_given = True
                continue
            if names == ["all"]:
                extensions = None        # explicit: the default set (every local one)
                extensions_given = False
                continue
            extensions_given = True
            if not names:
                sys.exit("--extensions= is empty. Omit the flag to install nothing, or "
                         "name what you want, e.g. --extensions=odoo_debug.")
            if extensions is None:
                extensions = []
            for name in names:
                ext = resolve_extension(name)
                if ext.name not in [e.name for e in extensions]:
                    extensions.append(ext)
        elif arg.startswith("--server-log-show="):
            # Machine-facing, like --describe and the --flow-* commands: the GUI's
            # Log sources page opens a log with it, which is also how somebody
            # finds out that a connection works before a run depends on it.
            server_log_show = arg.split("=", 1)[1].strip()
        elif arg.startswith("--server-log-lines="):
            value = arg.split("=", 1)[1].strip().lower()
            if value in ("all", "full", "0"):
                server_log_lines = 0          # 0 = everything the budget allows
            else:
                try:
                    server_log_lines = max(1, int(value))
                except ValueError:
                    sys.exit("--server-log-lines: %r is not a number or \"all\"."
                             % value)
        elif arg == "--server-log" or arg.startswith("--server-log="):
            # Stream the backend's own log alongside the windows. Values mirror
            # --extensions: bare flag = the logs marked "default" for this
            # environment, "all", "none", "list", or comma-separated names. Which
            # logs exist is a property of logsources.json, so resolving the names
            # waits until the environment is known (see resolve_server_logs).
            # "=" tells the bare flag from an explicitly empty one: --server-log
            # means "this environment's defaults", while --server-log= is a value
            # the user meant to type and did not, and guessing at that is how a run
            # ends up streaming something nobody asked for.
            given_value = "=" in arg
            value = arg.split("=", 1)[1].strip() if given_value else ""
            server_log_given = True
            if value == _SERVER_LOG_LIST:
                print(format_server_logs(log_sources_path))
                sys.exit(0)
            elif not given_value or value == _SERVER_LOG_DEFAULT:
                server_log = None
            elif value in (_SERVER_LOG_ALL, _SERVER_LOG_NONE):
                server_log = value
            else:
                # Covers both "--server-log=" and a value that is only separators.
                names = [n.strip() for n in value.split(",") if n.strip()]
                if not names:
                    sys.exit("--server-log=%s names no log. Omit the value for this "
                             "environment's default logs, or name them, e.g. "
                             "--server-log=app,nginx (--server-log=list shows them)."
                             % value)
                server_log = names
        elif arg == "--odoo-debug":
            # Deprecated: no extension is special-cased any more.
            legacy_odoo_flag = arg
            excluded_extensions.discard("odoo_debug")
        elif arg == "--no-odoo-debug":
            # Deprecated: now expressed as an exclusion from the default set.
            legacy_odoo_flag = arg
            excluded_extensions.add("odoo_debug")
        elif arg.startswith("--events="):
            # Structured JSONL event stream for a GUI or any other program
            # driving the launcher: "-" is stdout (logging goes to stderr, so
            # the two never mix), anything else is a file to append to.
            events_target = arg.split("=", 1)[1].strip()
            if not events_target:
                sys.exit("--events= is empty. Use --events=- for stdout, or "
                         "--events=PATH to append to a file.")
        elif arg.startswith("--control="):
            # The inbound half of --events. Only "-" (stdin) is meaningful: a
            # program that drives this launcher already has its pipes.
            value = arg.split("=", 1)[1].strip()
            if value != "-":
                sys.exit("--control: only '-' (stdin) is supported, got %r." % value)
            control_stdin = True
        elif arg.startswith("--log-level="):
            log_level = arg.split("=", 1)[1].strip()
        elif arg == "--init-users-json":
            init_config = True
        elif arg == "--describe":
            # Machine-readable inventory (environments, users, scenarios,
            # extensions) for a front-end. Handled after the parse loop so
            # --config/--flows-dir can appear in any order.
            describe_json = True
        elif arg.startswith("--flow-show="):
            # The scenario editing commands. All four answer with JSON and exit,
            # like --describe, and all are handled after the parse loop so
            # --flows-dir can appear on either side of them.
            flow_command = ("show", arg.split("=", 1)[1].strip())
        elif arg.startswith("--flow-save="):
            flow_command = ("save", arg.split("=", 1)[1].strip())
        elif arg.startswith("--flow-delete="):
            flow_command = ("delete", arg.split("=", 1)[1].strip())
        elif arg.startswith("--flow-import="):
            flow_command = ("import", os.path.abspath(os.path.expanduser(
                arg.split("=", 1)[1].strip())))
        # -- cycles. The same five shapes as the flow commands above, answering
        # the same way, because a front-end editing one edits the other.
        elif arg.startswith("--cycle-show="):
            cycle_command = ("show", arg.split("=", 1)[1].strip())
        elif arg.startswith("--cycle-save="):
            cycle_command = ("save", arg.split("=", 1)[1].strip())
        elif arg.startswith("--cycle-delete="):
            cycle_command = ("delete", arg.split("=", 1)[1].strip())
        elif arg.startswith("--cycle-import="):
            cycle_command = ("import", os.path.abspath(os.path.expanduser(
                arg.split("=", 1)[1].strip())))
        elif arg == "--cycle-list":
            cycle_command = ("list", "")
        # -- secret variables. The value of one never appears here: --set takes
        # it from the --from file, because argv is world-readable through ps.
        elif arg.startswith("--cycle-secret-list="):
            secret_command = ("list", arg.split("=", 1)[1].strip())
        elif arg.startswith("--cycle-secret-set="):
            secret_command = ("set", arg.split("=", 1)[1].strip())
        elif arg.startswith("--cycle-secret-copy="):
            secret_command = ("copy", arg.split("=", 1)[1].strip())
        elif arg.startswith("--cycle-secret-delete="):
            secret_command = ("delete", arg.split("=", 1)[1].strip())
        # -- what the project remembers. A plain file, unlike the secrets one:
        # these read and write it from a script, not protect it.
        elif arg.startswith("--cycle-memory-list"):
            secret_command = None
            memory_command = ("list", arg.split("=", 1)[1].strip()
                              if "=" in arg else "")
        elif arg.startswith("--cycle-memory-show="):
            memory_command = ("show", arg.split("=", 1)[1].strip())
        elif arg.startswith("--cycle-memory-forget="):
            memory_command = ("forget", arg.split("=", 1)[1].strip())
        elif arg == "--cycle-watch-planned":
            watch_command = ("planned", "")
        elif arg.startswith("--cycle-plan-remove="):
            watch_command = ("unplan", arg.split("=", 1)[1].strip())
        elif arg.startswith("--cycle-watch-plan="):
            watch_command = ("plan", arg.split("=", 1)[1].strip())
        elif arg.startswith("--cycle-watch-seen="):
            watch_command = ("seen", arg.split("=", 1)[1].strip())
        elif arg.startswith("--cycle-watch="):
            watch_command = ("check", arg.split("=", 1)[1].strip())
        elif arg == "--cycle-sessions" or arg.startswith("--cycle-sessions="):
            session_command = ("list", arg.split("=", 1)[1].strip()
                               if "=" in arg else "")
        elif arg.startswith("--cycle-session-delete="):
            session_command = ("delete", arg.split("=", 1)[1].strip())
        elif arg.startswith("--cycle-runs-dir="):
            runtime_paths.set_cycle_runs_dir(arg.split("=", 1)[1].strip())
        elif arg.startswith("--cycle-memory-file="):
            memory_file = os.path.abspath(os.path.expanduser(
                arg.split("=", 1)[1].strip()))
        elif arg.startswith("--cycle-secrets-file="):
            secrets_file = os.path.abspath(os.path.expanduser(
                arg.split("=", 1)[1].strip()))
        elif arg.startswith("--cycle-plugin-action="):
            plugin_action = arg.split("=", 1)[1].strip()
        elif arg.startswith("--cycle-run="):
            cycle_run = arg.split("=", 1)[1].strip()
        elif arg.startswith("--cycle-var="):
            # Repeatable, and split on the FIRST equals, so a value may contain
            # one: --cycle-var=url=https://host/?a=b is one variable, not a
            # parse error.
            pair = arg.split("=", 1)[1]
            if "=" not in pair:
                bad_option = "--cycle-var"
            else:
                name, value = pair.split("=", 1)
                cycle_vars[name.strip()] = value
        elif arg.startswith("--cycle-jobs="):
            raw = arg.split("=", 1)[1].strip()
            try:
                cycle_jobs = max(1, int(raw))
            except ValueError:
                bad_option = "--cycle-jobs"
        elif arg.startswith("--cycle-only="):
            cycle_only = [one.strip() for one
                          in arg.split("=", 1)[1].split(",") if one.strip()]
        elif arg.startswith("--cycle-from="):
            cycle_from = arg.split("=", 1)[1].strip()
        elif arg.startswith("--cycle-resume="):
            cycle_resume = arg.split("=", 1)[1].strip()
        elif arg.startswith("--cycle-reuse="):
            cycle_reuse = arg.split("=", 1)[1].strip()
        elif arg.startswith("--cycles-dir="):
            cycles_dir = os.path.abspath(os.path.expanduser(
                arg.split("=", 1)[1].strip()))
        elif arg == "--recorder" or arg.startswith("--recorder="):
            # Recording mode: open the windows as usual, but with a debug port
            # and the recorder shown in each of them. Nothing is recorded until
            # Capture Step is pressed - showing it is not capturing.
            recorder = arg.split("=", 1)[1].strip() if "=" in arg else True
        elif arg == "--selectors-show":
            flow_command = ("selectors-show", "")
        elif arg == "--selectors-save":
            flow_command = ("selectors-save", "")
        elif arg.startswith("--from="):
            # The document --flow-save writes, as JSON in a file. A file rather
            # than stdin so the command can be run and debugged from a shell.
            flow_source = os.path.abspath(os.path.expanduser(arg.split("=", 1)[1].strip()))
        elif arg.startswith("--user-session="):
            session_prefix = arg.split("=", 1)[1].strip()
        elif arg.startswith("--url="):
            url = arg.split("=", 1)[1].strip()
        elif arg.startswith("--sessions-dir="):
            sessions_dir = os.path.abspath(os.path.expanduser(arg.split("=", 1)[1].strip()))
        elif arg.startswith("--config="):
            config_path = os.path.abspath(os.path.expanduser(arg.split("=", 1)[1].strip()))
        elif arg.startswith("--log-sources="):
            pass          # already read above, before anything could answer with it
        elif arg.startswith("--flows-dir="):
            # Lets the scenarios live in their own repo, separate from the engine.
            flows_dir = os.path.abspath(os.path.expanduser(arg.split("=", 1)[1].strip()))
        elif arg.startswith("--reports-dir="):
            reports_dir = os.path.abspath(os.path.expanduser(arg.split("=", 1)[1].strip()))
        elif arg.startswith("--filter-users="):
            # --filter-users=all keeps everyone; otherwise a comma-separated list of
            # logins to launch from the config. An empty value is an error.
            users_filter = parse_filter_list("--filter-users", arg.split("=", 1)[1])
        elif arg.startswith("--env="):
            env_name = arg.split("=", 1)[1].strip()
        elif arg == "--env" or arg.startswith("--filter-prefix"):
            # --filter-prefix was replaced by --env. Deferred until after the loop
            # so --config= can appear in any order (the error lists the envs).
            bad_option = arg
        elif arg.startswith("--run-tests="):
            val = arg.split("=", 1)[1].strip()
            # --run-tests=all runs every (non-template) scenario in flows/scenarios;
            # --run-tests=config gives each user its own "tests" from the config, so
            # one launch covers several roles without replaying every scenario against
            # every window; otherwise a comma-separated list of scenario ids. Presence
            # of this flag switches the launcher into flow-execution mode: after
            # launching, attach to each window over CDP, run, write reports, and exit.
            if not val or val.lower() == "all":
                run_tests = "all"
            elif val.lower() == "config":
                run_tests = "config"
            else:
                run_tests = [s.strip() for s in val.split(",") if s.strip()]
        elif arg.startswith("--jobs="):
            # How many WINDOWS are driven at once. Scenarios inside a window always
            # run one after another; this only removes the wait between windows.
            val = arg.split("=", 1)[1].strip().lower()
            jobs_given = True
            if val == "all":
                jobs = "all"
            elif val == "auto":
                # The ONLY value that lets the load governor move the number of
                # windows. A number given here is a number kept.
                jobs = "auto"
            elif val.isascii() and val.isdigit() and int(val) >= 1:
                jobs = int(val)
            else:
                sys.exit("--jobs: expected a positive number, 'all' or 'auto', got %r."
                         % val)
        elif arg == "--execution-overlay" or arg.startswith("--execution-overlay="):
            # Enable the in-page execution HUD. Value is a comma-separated list of
            # components (tree, progress, status, logs, ...) or "all"; bare flag = all.
            # Only valid together with --run-tests (checked after the parse loop).
            val = arg.split("=", 1)[1].strip() if "=" in arg else "all"
            if not val or val.lower() == "all":
                overlay_components = list(_OVERLAY_COMPONENTS)
            else:
                requested = [c.strip() for c in val.split(",") if c.strip()]
                bad = [c for c in requested if c not in _OVERLAY_COMPONENTS]
                if bad:
                    sys.exit("--execution-overlay: unknown component(s) %s. Known: %s"
                             % (", ".join(bad), ", ".join(_OVERLAY_COMPONENTS)))
                overlay_components = requested
        elif arg.startswith("--report-level="):
            # Which report artifacts to generate (console,dom,result,screen,url).
            # When given, only the listed artifacts are produced, on success and on
            # failure alike. Validated after the parse loop. Requires --run-tests.
            report_level = arg.split("=", 1)[1].strip()
        elif arg == "--report-always":
            # Produce a full report after a successful run too, not only on failure.
            report_always = True
        elif arg.startswith("--report-screen="):
            # When screenshots are captured: start,each,finish (any combination).
            # Only has effect when 'screen' is among the report artifacts.
            report_screen = arg.split("=", 1)[1].strip()
        elif arg.startswith("--user="):
            cli_user = arg.split("=", 1)[1].strip()
        elif arg.startswith("--password="):
            cli_password = arg.split("=", 1)[1]
        elif arg.startswith("-"):
            # An unknown or valueless option. NEVER let this reach `positional`:
            # that list supplies the URL, so a bare --run-tests used to launch
            # Chrome on the literal string "--run-tests" and run no flows at all.
            bad_option = arg
        else:
            positional.append(arg)
    level = getattr(logging, log_level.upper(), None)
    if not isinstance(level, int):
        sys.exit("Invalid --log-level %r (use DEBUG, INFO, WARNING, ERROR)." % log_level)
    logging.basicConfig(level=level, format="%(asctime)s  %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    if events_target:
        # basicConfig has already claimed stderr for log records, so events can
        # own stdout. Imported here: a launch without --events must not pay for
        # the engine package at all.
        global _events
        from engine import events as _events_module
        _events = _events_module
        _events.configure(events_target)
        atexit.register(_events.close)
    if init_config:
        init_users_json(config_path)  # writes the starter config (if absent) and exits
    if flow_command:
        run_flow_command(flow_command, flow_source, flows_dir)
    if server_log_show:
        from engine import serverlog as _serverlog
        wanted = (_serverlog.READ_TAIL_LINES if server_log_lines is None
                  else (server_log_lines or None))
        json.dump(show_server_log(server_log_show, lines=wanted,
                                  path=log_sources_path), sys.stdout,
                  indent=2, ensure_ascii=False, default=str)
        print()
        sys.exit(0)
    if describe_json:
        # Always valid JSON on stdout, even when it fails: the caller is a
        # program, and an exit code plus a plain-text message would leave it
        # parsing error strings.
        try:
            payload = describe(config_path, log_sources_path=log_sources_path,
                               flows_dir=flows_dir, cycles_dir=cycles_dir,
                               sessions_dir=sessions_dir, reports_dir=reports_dir)
        except Exception as exc:  # noqa: BLE001 - the report IS the error report
            json.dump({"error": "%s: %s" % (type(exc).__name__, exc)}, sys.stdout,
                      indent=2)
            print()
            sys.exit(2)
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False, default=str)
        print()
        sys.exit(0)
    if bad_option is not None:
        sys.exit(_bad_option_message(bad_option, config_path))
    # After the option check, deliberately. A cycle command exits with JSON the
    # way the flow commands do, but a cycle *run* is long and starts services,
    # and neither should happen when the command line has a typo in it that
    # might be the very flag that was meant to narrow what runs.
    if plugin_action:
        run_plugin_action(plugin_action, flow_source)
    if secret_command:
        run_secret_command(secret_command, flow_source, secrets_file)
    if memory_command:
        run_memory_command(memory_command, flow_source, memory_file)
    if session_command:
        run_session_command(session_command, memory_file, cycles_dir)
    if watch_command:
        run_watch_command(watch_command, cycles_dir, secrets_file, memory_file,
                          flow_source)
    if cycle_command:
        # --from= is shared with --flow-save: one flag, one meaning - the JSON
        # document this write is being given.
        run_cycle_command(cycle_command, flow_source, cycles_dir)
    if cycle_run:
        if run_tests or recorder:
            sys.exit("--cycle-run cannot be combined with --run-tests or "
                     "--recorder: a cycle runs scenarios through its own "
                     "scenario.run step, and decides itself when to open a "
                     "browser.")
        # Before the launch loop, because a cycle opens no windows of its own.
        sys.exit(run_cycle_mode(cycle_run, cycles_dir=cycles_dir,
                                flows_dir=flows_dir, variables=cycle_vars,
                                jobs=cycle_jobs, control=control_stdin,
                                reports_dir=reports_dir,
                                secrets_path=secrets_file,
                                memory_path=memory_file,
                                only=cycle_only, start_from=cycle_from,
                                resume=cycle_resume, reuse_from=cycle_reuse))
    if session_prefix and (os.sep in session_prefix or (os.altsep and os.altsep in session_prefix)):
        sys.exit("--user-session must be a single name, no path separators.")
    if overlay_components and run_tests is None:
        sys.exit("--execution-overlay requires --run-tests (there is no flow "
                 "execution to visualize without it).")
    if no_browser:
        if recorder:
            sys.exit("--no-browser cannot record: the recorder is used by clicking in "
                     "a window, and this run opens none.")
        if run_tests is None:
            sys.exit("--no-browser requires --run-tests (it runs scenarios; without "
                     "them there is nothing to do).")
        if overlay_components:
            log.warning("--execution-overlay does nothing with --no-browser: there is "
                        "no page to draw it in. Running without it.")
            overlay_components = None
        if detach:
            # Before the --server-log check below: a detach that is about to be
            # ignored must not cost the run its server logs.
            log.info("--detach does nothing with --no-browser: there are no windows "
                     "to leave running.")
            detach = False
        extensions = []           # and no profile to install them into
    if server_log_given and detach:
        # --detach means this process goes away the moment the windows are up, and
        # the readers are its threads: there would be nothing left to stream into.
        # Said once and dropped, not refused - the windows are what was asked for,
        # and a diagnostic is never a reason not to open them.
        log.warning("--server-log does nothing with --detach: the launcher exits as "
                    "soon as the windows open, and the log readers exit with it. "
                    "Launching without server logs.")
        server_log_given = False
    if legacy_odoo_flag == "--odoo-debug":
        log.info("%s is deprecated; extensions/ is installed by default, and "
                 "--extensions=odoo_debug names it explicitly.", legacy_odoo_flag)
    elif legacy_odoo_flag == "--no-odoo-debug":
        log.info("%s is deprecated; use --extensions to choose, or --extensions=none.",
                 legacy_odoo_flag)
    # No --extensions: install every usable extension vendored in the project. A
    # broken one is skipped with a warning rather than stopping the launch.
    if extensions is None:
        extensions = default_extensions()
    if excluded_extensions and not extensions_given:
        extensions = [e for e in extensions if e.name not in excluded_extensions]
    if flows_dir is not None and not os.path.isdir(flows_dir):
        sys.exit("--flows-dir: %s is not a directory." % flows_dir)
    # No --run-tests guard, unlike the flags below it, and it used to have one.
    # This says where a tree *is*, not what to do with it, and the GUI now carries
    # it on every call so that the tree its Scenarios page edits is the tree a run
    # reads - the bargain --config and --log-sources already make. On a plain
    # launch it simply goes unused, which is not worth refusing.
    if reports_dir is not None and run_tests is None:
        sys.exit("--reports-dir requires --run-tests (nothing is written without a run).")
    if jobs_given and run_tests is None:
        sys.exit("--jobs requires --run-tests (a plain launch just opens windows; there "
                 "are no scenario runs to spread across workers).")
    if close_after and run_tests is None:
        sys.exit("--close-after requires --run-tests (a plain launch already leaves "
                 "windows open until you close them).")
    # Build the report configuration from the --report-* flags (if any). Validate
    # here, before any window is launched, so a typo fails fast. Legacy default
    # (report_config stays None) when no report flag was given.
    report_config = None
    if report_level is not None and "server_log" in report_level:
        # It briefly was one. Backend logs are not the browser's, and they have
        # their own switch: --server-log decides both the streaming and the file.
        log.info("--report-level=server_log is no longer a thing; --server-log "
                 "already keeps the file. Ignoring it.")
        report_level = ",".join(part for part in report_level.split(",")
                                if part.strip() != "server_log")
        if not report_level.strip():
            report_level = None
    if report_level is not None or report_always or report_screen is not None:
        if run_tests is None:
            sys.exit("--report-level/--report-always/--report-screen require --run-tests "
                     "(reports are only produced when flows run).")
        from engine.artifacts import ReportConfig
        try:
            report_config = ReportConfig.from_cli(level=report_level, always=report_always,
                                                   screen=report_screen)
        except ValueError as exc:
            sys.exit("Error: %s" % exc)
        if report_screen is not None and not report_config.screen_enabled:
            log.warning("--report-screen has no effect: 'screen' is not in --report-level, "
                        "so no screenshots will be captured.")
        if no_browser and (report_screen is not None
                           or (report_config.level or frozenset()) - {"result"}):
            log.info("--no-browser: only result.json and server logs are written; "
                     "screenshots, dom, console and url need a page.")
    # Resolve --env before the URL check: it names the environment to launch AND
    # supplies that environment's URL, so --url only has to be typed to override it.
    envs = environments_from_config(config_path, strict=(env_name is not None
                                                         or cli_password is None))
    selected = resolve_environment(env_name, envs, config_path) if env_name is not None else None

    # --url= takes precedence, then a positional URL, then the environment's own.
    if not url and positional:
        url = positional[0]
    if not url and selected is not None:
        if not selected.origin:
            sys.exit("--env=%s selects %r, which is not a URL or host:port, so it cannot "
                     "supply the URL. Pass --url too." % (selected.alias, selected.value))
        url = selected.origin
    if not url:
        sys.exit("Usage: %s [--detach] [--env=NAME] [--user-session=PREFIX] [--sessions-dir=DIR] "
                 "[--config=FILE] [--init-users-json] [--filter-users=all|L1,L2,...] "
                 "[--extensions=LIST] [--log-level=LEVEL] "
                 "[--run-tests=all|ID,...] [--execution-overlay=all|tree,progress,status,logs] "
                 "[--report-level=console,dom,result,screen,url] [--report-always] "
                 "[--report-screen=start,each,finish] "
                 "[--close-after] [--no-browser] [--user=LOGIN --password=PASS] "
                 "(--env=NAME | --url=URL | <URL>)   "
                 "e.g. --env=dev, or --url=http://localhost:8069/web/login" % sys.argv[0])
    url = normalize_url(url)
    split = urlsplit(url)
    origin = "%s://%s" % (split.scheme, split.netloc)
    if selected is not None and selected.origin and selected.origin.lower() != origin.lower():
        # Not fatal - an explicit --url wins, e.g. a deep link into the same app -
        # but a mismatched host is nearly always a copy-paste slip worth naming.
        log.warning("--url origin %s differs from --env=%s (%s); using the URL. Profiles and "
                    "user selection still follow --env.", origin, selected.alias, selected.origin)

    # --user launches a single user, taking whatever it is not given from the
    # environment (see resolve_user_row); otherwise the list comes from the config.
    if cli_user is not None:
        if users_filter is not None:
            sys.exit("--filter-users applies to the config list; not compatible with "
                     "--user (which already names a single user). --env is fine with both.")
        config_users = [] if cli_password is not None else load_users(config_path)
        users = [resolve_user_row(cli_user, cli_password, config_users, selected,
                                  origin, envs, config_path, url)]
    else:
        users = load_users(config_path)
        if selected is not None:
            users = [u for u in users if u.env == selected.value]
        if users_filter is not None:
            # keep the config's order; error on any requested login that isn't there
            known = {u.login for u in users}
            missing = [name for name in users_filter if name not in known]
            if missing:
                sys.exit("--filter-users: unknown login(s) %s. %s has: %s"
                         % (", ".join(missing),
                            "--env=%s" % selected.alias if selected else "The config",
                            ", ".join(sorted(known))))
            wanted = set(users_filter)
            users = [u for u in users if u.login in wanted]
        if not users:
            sys.exit("No users selected (--env/--filter-users left nothing to launch).")
        if cli_password is not None:
            # Same precedence as everywhere else: what you typed beats the config.
            log.info("--password overrides the stored password for %d selected user%s.",
                     len(users), "" if len(users) == 1 else "s")
            users = apply_password_override(users, cli_password)
        spanned = {u.env for u in users}
        if selected is None and len(spanned) > 1:
            # The bug this flag exists to prevent: several environments' users, all
            # pointed at one URL. Legitimate when you really do want everything.
            log.warning("No --env: launching %d users across %d environments (%s), all pointed "
                        "at %s.", len(users), len(spanned),
                        ", ".join(sorted(env_alias(v) for v in spanned)), url)

    # Guard: two selected users must not map to the same profile folder. This can
    # happen when --user-session forces one prefix onto entries that differ only by
    # env (e.g. the same login across several environments).
    seen_dirs = {}
    for user in users:
        login = user.login
        sd = session_dir_for(session_prefix, user.env, login)
        if sd in seen_dirs:
            sys.exit("Users %r and %r both map to profile folder %r. --user-session "
                     "forces one prefix on every entry; drop it, or use --env "
                     "to launch a single environment." % (seen_dirs[sd], login, sd))
        seen_dirs[sd] = login

    # Recording is a one-window activity. Several windows would each show their own
    # recorder, only the first would get the scenario id that was asked for (see
    # engine.recorder.record_sessions) and the rest would write files nobody asked
    # for - while the person doing the recording can only be clicking in one of
    # them anyway. Checked before any window opens, so the answer costs nothing.
    if recorder and len(users) > 1:
        sys.exit("--recorder records ONE window: %d users are selected (%s). Pick one "
                 "with --user=LOGIN or --filter-users=LOGIN."
                 % (len(users), ", ".join(u.login for u in users)))

    # --run-tests=config is only meaningful if somebody actually has tests. Check it
    # here, before any window opens, so a typo does not cost a full launch first.
    if run_tests == "config":
        without = [u.login for u in users if not u.tests]
        if len(without) == len(users):
            sys.exit("--run-tests=config: none of the %d selected user(s) has a 'run-tests' "
                     "field in %s. Add e.g. \"run-tests\": [\"access_agent\"] to an entry, or "
                     "name the scenarios with --run-tests=ID,..."
                     % (len(users), config_path))
        if without:
            log.warning("--run-tests=config: no 'tests' configured for %s; %s will launch "
                        "but run nothing.", ", ".join(without),
                        "they" if len(without) > 1 else "it")

    # --no-browser runs only what never touches a page. Vetted here, before anything
    # starts, so a scenario that clicks is named now rather than failing its first
    # page step minutes into the run. The runner checks again after compiling, for
    # a scenario edited in the meantime.
    if no_browser:
        refused = _needing_a_browser(run_tests, users, flows_dir)
        if refused:
            sys.exit("--no-browser runs only service steps and assert_host_up. These "
                     "scenarios need a browser:\n%s"
                     % "\n".join("  %s: %s" % (scenario_id, ", ".join(actions))
                                 for scenario_id, actions in refused))

    # Everything above is argument/config validation, so it fails without needing a
    # browser at all; only now does a missing Chrome become the problem - and not
    # at all for a run that opens none.
    chrome = None if no_browser else find_chrome()
    if not chrome and not no_browser:
        sys.exit(chrome_missing_message())

    # Everything is resolved and valid from here on, so this event describes the
    # run a consumer is about to watch (never the passwords, which stay in the
    # config and the profile).
    _emit("launcher.start", version=version(), url=url, origin=origin,
          config=config_path, chrome=chrome, run_tests=run_tests,
          env=selected.alias if selected is not None else None,
          users=[{"login": u.login, "class": u.cls, "env": u.env, "tests": list(u.tests)}
                 for u in users])

    # In the foreground (not --detach) on Linux, bind each window's lifetime to this
    # launcher with PR_SET_PDEATHSIG, so even a hard kill of the launcher - e.g. VS
    # Code's "Stop" button, which we can't trap in Python - still closes the windows.
    # With --detach (or off Linux) just start a new session so windows outlive us.
    if detach or not sys.platform.startswith("linux"):
        spawn_kwargs = {"start_new_session": True}
    else:
        spawn_kwargs = {"preexec_fn": _preexec_die_with_parent}  # calls setsid itself
    # Chrome is a foreign executable: it must not inherit the library search path a
    # frozen build sets up for itself, or it loads our bundled libssl and dies.
    spawn_kwargs["env"] = runtime_paths.clean_subprocess_env()

    # Fetch each requested extension once (cached under the sessions dir) so every
    # profile can install it below. On any failure - offline, service down - we warn
    # and carry on without that one rather than block the logins.
    # Local (in-tree) extensions need no fetch at all; only Web Store ones are
    # downloaded, once each, cached under the sessions dir.
    ready = []              # [(name, kind, path)] installable into every profile
    store_extensions = [e for e in extensions if e.kind == "store"]
    for ext in extensions:
        if ext.kind == "local":
            ready.append((ext.name, "local", ext.value))
    if store_extensions:
        prodversion = _chrome_prodversion(chrome)
        for ext in store_extensions:
            path = os.path.join(sessions_dir, "_crx", "%s.crx" % ext.name)
            try:
                download_crx(path, ext.value, prodversion)
                ready.append((ext.name, "store", path))
            except Exception as exc:
                log.warning("Extension %s (%s) unavailable (%s); continuing without it.",
                            ext.name, ext.value, exc)

    # Backend logs, if asked for. Started before the first window so a line written
    # while Chrome is still coming up is already being read - the correlation window
    # opens when the window does, and a reader that starts late has a blind spot at
    # exactly the moment the session begins.
    log_hub = None
    if server_log_given:
        chosen = resolve_server_logs(sorted({u.env for u in users}), server_log,
                                     path=log_sources_path)
        if chosen:
            from engine import serverlog
            log_hub = serverlog.ServerLogHub(chosen, on_lines=_emit_server_lines)
            log_hub.start()
            atexit.register(log_hub.close)
            log.info("Streaming %d server log(s): %s", len(chosen),
                     ", ".join(sorted({s.name for s in chosen})))
        elif not isinstance(server_log, list):
            # A named log that did not resolve has already been reported, with the
            # detail this could not add. Only the vaguer requests need a word here.
            log.warning("--server-log matched no logs for %s; nothing to stream.",
                        ", ".join(sorted({u.env for u in users})) or "this run")

    procs = []
    sessions = []  # (cls, proc, profile, login, origin, tests) per window, for --run-tests
    windows_by_session = {}  # session name -> Popen, for "stop just this one"
    # profile -> planted extension directories, for the Windows CDP install
    # (see load_extensions_over_cdp).
    extension_dirs = {}
    # profile -> the config's env string. logsources.json is keyed by that exact
    # string, and it is the one thing the session tuple does not carry.
    session_envs = {}
    # Staged launching: open each window inside the runner's slot instead of all of
    # them here, so --jobs caps resident browsers and the load governor has a lever
    # that returns memory. Only when the windows are going to be closed anyway -
    # without --close-after the user asked to keep them for inspection, and a
    # window closed the moment its scenarios end is not that.
    # Never with --no-browser: there is no window to open inside a slot, and the
    # sessions are rationed by the same slots through the runner's other path.
    staged = bool(run_tests) and not recorder and close_after and not no_browser
    for entry_prefix, cls, login, password, user_tests in users:
        # The profile folder is "<prefix>-<login>" (a single safe path segment),
        # keyed by prefix+login so each user/environment gets its own reused profile.
        # --user-session overrides the config's per-entry prefix.
        session_dir = session_dir_for(session_prefix, entry_prefix, login)
        profile = os.path.join(sessions_dir, session_dir)
        session_envs[profile] = entry_prefix
        if no_browser:
            # Named exactly as a window's session is - the runner names a session
            # by its profile folder - but nothing is created there and nothing
            # opens. From here the session is the runner's, as any other.
            sessions.append((cls, None, profile, login, origin, user_tests))
            if log_hub is not None:
                log_hub.add_session(os.path.basename(profile), entry_prefix)
            continue
        os.makedirs(profile, exist_ok=True)
        set_profile_name(profile, "%s - %s" % (cls, login))
        clear_previous_tabs(profile)  # always start with one tab, keep the login
        # Install the auto-login extension into the profile (must run after
        # set_profile_name, which writes the Preferences file this merges into).
        planted = []      # the directories Chrome is asked to load on Windows
        try:
            planted.append(install_autologin_extension(profile, origin, login, password))
        except Exception as exc:  # a broken extension must not block the launch
            log.warning("Skipping auto-login extension for %s: %s", login, exc)
        for ext_name, ext_kind, ext_path in ready:
            try:
                if ext_kind == "local":
                    planted.append(install_local_extension(
                        profile, ext_path, origin, ".%s_key" % ext_name))
                else:
                    planted.append(install_crx_extension(
                        profile, ext_path, origin, ".%s_key" % ext_name))
            except Exception as exc:  # optional extra; never block the launch
                log.warning("Skipping %s extension for %s: %s", ext_name, login, exc)
        extension_dirs[profile] = [d for d in planted if d]
        try:
            # store this user's single credential in the profile's password manager
            seed_password(chrome, profile, origin, login, password)
        except Exception as exc:  # never let a save failure block the launch
            log.warning("Skipping password save for %s: %s", login, exc)
        if staged:
            # Prepared, not opened: the runner starts this Chrome when it has a
            # slot for it. proc is None until then.
            sessions.append((cls, None, profile, login, origin, user_tests))
            continue
        wanted = extension_dirs.get(profile) or []
        proc = open_window(chrome, profile, cls, login, url,
                           bool(run_tests or recorder)
                           or bool(EXTENSIONS_NEED_CDP and wanted),
                           spawn_kwargs, bind_lifetime=not detach)
        procs.append((cls, proc))
        if EXTENSIONS_NEED_CDP and wanted:
            _install_extensions_for(profile, wanted)
        # Keyed the way the engine and the event stream name a session, so a
        # "stop this one" command can be addressed to what the user is looking at.
        windows_by_session[os.path.basename(profile)] = proc
        if log_hub is not None:
            # From here on, whatever the backend writes is this window's.
            log_hub.add_session(os.path.basename(profile), entry_prefix)
        if run_tests or recorder:
            # user_tests is this user's own "tests" field; the runner uses it
            # when --run-tests=config, and ignores it otherwise.
            sessions.append((cls, proc, profile, login, origin, user_tests))
        time.sleep(0.4)

    if control_stdin:
        # Daemon: a run must never wait on stdin to finish. Started once the
        # windows exist so an early command finds something to address.
        threading.Thread(target=read_commands, args=(windows_by_session,),
                         name="control", daemon=True).start()
        # A scenario may now ask for a service. Both halves have to be there:
        # this thread to be answered on, and an events sink to ask through.
        try:
            from engine import services as engine_services

            engine_services.configure(_events is not None and _events.enabled())
        except ImportError:
            pass                    # no engine installed: a plain launch, nothing to ask

    if no_browser:
        log.info("%d session(s), no browser: nothing is opened.", len(sessions))
    elif staged:
        log.info("%d session(s) prepared. Windows open as slots free, %s at a time; "
                 "each closes when its scenarios are done.", len(sessions),
                 {"all": "all", "auto": "as many as the machine allows"}.get(jobs, jobs))
        _emit("windows.deferred", count=len(sessions))
    else:
        log.info("All %d windows launched. The auto-login extension signs each one in on the "
                 "login page; profiles persist the session, so later launches open already "
                 "logged in.", len(procs))
        _emit("windows.ready", count=len(procs))

    if recorder:
        # Recording mode: attach to every window and show the recorder in it.
        # Nothing is captured until it is asked for, so this blocks exactly like a
        # plain launch does, and CTRL+C (or the GUI's Stop) ends it the same way,
        # closing the windows through close_all.
        from engine.recorder import record_sessions
        stop = threading.Event()

        def _stop_recording(_signum, _frame):
            stop.set()

        signal.signal(signal.SIGINT, _stop_recording)
        signal.signal(signal.SIGTERM, _stop_recording)
        rc = 1
        try:
            rc = record_sessions(sessions, env={"origin": origin, "url": url},
                                 flows_dir=flows_dir,
                                 scenario_id=recorder if recorder is not True else None,
                                 stop_event=stop)
        finally:
            _emit("recorder.exit", exit_code=rc)
            close_all(procs)
        sys.exit(rc)

    if run_tests:
        # Flow-execution mode: attach to each launched window over CDP, run the
        # requested scenarios, and write reports. Import here so a normal launch
        # never loads playwright/yaml.
        from engine.runner import run_scenarios
        rc = None   # stays None if the run raises: the event still says so
        stopped = False
        try:
            rc = run_scenarios(sessions, run_tests, env={"origin": origin, "url": url},
                               flows_dir=flows_dir, reports_dir=reports_dir,
                               overlay_components=overlay_components, report=report_config,
                               jobs=len(sessions) if jobs == "all" else jobs,   # "auto" passes through
                               server_logs=log_hub,
                               windows=WindowSource(chrome, url, spawn_kwargs, procs,
                                                    by_session=windows_by_session,
                                                    bind_lifetime=not detach,
                                                    extension_dirs=extension_dirs,
                                                    log_hub=log_hub,
                                                    session_envs=session_envs)
                                       if staged else None,
                               browser=not no_browser)
        except KeyboardInterrupt:
            stopped = True
            raise
        finally:
            # Before anything else: a session thread still blocked on a service
            # reply would otherwise hold the run open for the rest of its
            # timeout, and no answer is coming now.
            if control_stdin:
                from engine import services as engine_services

                engine_services.abandon_all()
            _emit("run.finished", exit_code=rc)
            # A STOPPED run always closes its windows, whatever --close-after
            # says. That flag answers "what should happen when a run finishes";
            # someone who pressed Stop is not asking to be left with seven
            # browsers open on a half-finished flow.
            if close_after or stopped:
                close_all(procs)  # graceful teardown flushes each login session to disk
        # With no browser there is nothing left to inspect, so the run ending is
        # the launcher ending, whatever --close-after says.
        if close_after or no_browser:
            _emit("launcher.exit", exit_code=rc)
            sys.exit(rc)
        # Default: reports/screenshots are done, but leave the windows open so the
        # final app state (and the Execution Overlay, if enabled) can be inspected.
        # Block here just like a plain launch; exit with the aggregate result once
        # the windows are closed manually or a shutdown signal arrives.
        log.info("Execution finished (exit code %d). --close-after not set; leaving "
                 "windows open for inspection.", rc)
        keep_open_until_closed(procs)
        sys.exit(rc)

    if detach:
        log.info("--detach: leaving windows running; script exits now.")
        return

    keep_open_until_closed(procs)


if __name__ == "__main__":
    main()
