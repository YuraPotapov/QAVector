"""What was changed in a service through QAVector, and what failed: the Activity tab.

One line of JSON per change, beside the project's connections file::

    <project>/integrations-activity.jsonl

A change made on the Integrations page and one made by anything else through a
connection land here alike. Each says where it came from with a ``source`` the
writer gives - ``{"label": ..., "detail": ...}`` - which this module stores and
shows and never interprets. Reads are not written down - only what changed
something, or tried to. Nothing in an entry is a value: no field contents, no
comment text, never a token; an issue key, what was done, and how it went.
"""

import json
import os
import time

FILE_NAME = "integrations-activity.jsonl"

#: Kept: the newest this many entries. Trimmed only once the file has grown a
#: good way past it, so an ordinary write is one append.
KEEP = 2000
TRIM_AT = 2500


def path_for(integrations_path):
    """The activity file beside a connections file."""
    from integrations import store
    return os.path.join(os.path.dirname(os.path.abspath(store.resolve_path(integrations_path))),
                        FILE_NAME)


def record(integrations_path, connection, operation, target, ok, summary, source=None):
    """Append one entry. Never raises: a change that was made must not be
    reported as failed because its log line could not be written."""
    entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "connection": str(connection or ""),
             "op": str(operation or ""), "target": str(target or ""), "ok": bool(ok),
             "summary": str(summary or "")[:500],
             "source": dict(source or {"label": "This page"})}
    path = path_for(integrations_path)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if os.path.getsize(path) > TRIM_AT * 400:
            _trim(path)
    except OSError:
        pass
    return entry


def read(integrations_path, limit=500):
    """The newest entries first, at most ``limit``."""
    path = path_for(integrations_path)
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    found = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            found.append(entry)
        if len(found) >= limit:
            break
    return found


def _trim(path):
    with open(path, encoding="utf-8") as handle:
        lines = handle.readlines()
    if len(lines) <= TRIM_AT:
        return
    temp = path + ".tmp"
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.writelines(lines[-KEEP:])
    os.replace(temp, path)
