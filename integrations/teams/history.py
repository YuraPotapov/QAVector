"""What was posted to Teams through QAVector (QCR ADR-015 D4).

A webhook cannot read a channel back, so this is the only record of what was
sent: ``integrations-teams-posts.jsonl`` beside the connections file - apart
from the activity log, because it keeps the content. Each post records its
title, text, mentions, outcome, who sent it (a person, an agent with its
conversation, a cycle step with its run) and who approved it. **Never the
webhook URL.** The newest ``KEEP`` posts are kept.

Outcomes: ``accepted`` (the workflow took it - HTTP 2xx; 202 is not
"delivered"), ``failed`` (the host and the HTTP status), ``unknown`` (no answer
in time). A post is never sent again by itself.
"""

import contextlib
import json
import os
import time
import uuid

from integrations import store

FILE_NAME = "integrations-teams-posts.jsonl"
KEEP, TRIM_AT = 2000, 2500
OUTCOMES = ("accepted", "failed", "unknown")


def path_for(integrations_path):
    return os.path.join(os.path.dirname(os.path.abspath(store.resolve_path(integrations_path))), FILE_NAME)


def record(integrations_path, connection, title, text, mentions, outcome, status=0, host="", error="",
           source=None, approved_by="", again_of=""):
    """Write one post down; returns it. Never raises: a post that was made must
    not be reported as failed because its record could not be written."""
    post = {"id": uuid.uuid4().hex[:12], "at": time.time(), "connection": str(connection or ""),
            "title": str(title or "")[:300], "text": str(text or ""),
            "mentions": [str(one) for one in mentions or ()][:50],
            "outcome": outcome if outcome in OUTCOMES else "unknown", "status": int(status or 0),
            "host": str(host or ""), "error": str(error or "")[:500],
            "source": dict(source or {"label": "This page"})}
    if approved_by:
        post["approved_by"] = str(approved_by)[:200]
    if again_of:
        post["again_of"] = str(again_of)
    path = path_for(integrations_path)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(post, ensure_ascii=False) + "\n")
        _trim(path)
    except OSError:
        pass
    return post


def _trim(path):
    with open(path, encoding="utf-8") as handle:
        lines = handle.readlines()
    if len(lines) <= TRIM_AT:
        return
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.writelines(lines[-KEEP:])
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def read(integrations_path, connection="", size=50, outcome=""):
    """The newest posts first, of one connection or all, of one outcome or all."""
    try:
        with open(path_for(integrations_path), encoding="utf-8") as handle:
            posts = [json.loads(line) for line in handle if line.strip()]
    except (OSError, ValueError):
        return []
    posts = [one for one in posts if (not connection or one.get("connection") == connection)
             and (not outcome or one.get("outcome") == outcome)]
    return list(reversed(posts))[:max(1, int(size or 50))]


@contextlib.contextmanager
def held(integrations_path, connection):
    """One post at a time through a connection, whichever process sends it -
    the page, QCR, a cycle: the check of the last post and the sending are one."""
    folder = os.path.dirname(path_for(integrations_path))
    os.makedirs(folder, exist_ok=True)
    lock = os.path.join(folder, ".teams-%s.lock" % connection)
    with open(lock, "a+b") as handle:
        if os.name != "nt":
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name != "nt":
                fcntl.flock(handle, fcntl.LOCK_UN)


def last_sent(integrations_path, connection):
    """When the newest post that may have reached the channel was made, or None:
    a failed one did not; one with no answer may have."""
    for one in read(integrations_path, connection, size=KEEP):
        if one.get("outcome") in ("accepted", "unknown"):
            return one.get("at")
    return None


def get(integrations_path, post_id):
    return next((one for one in read(integrations_path, size=KEEP) if one.get("id") == post_id), None)
