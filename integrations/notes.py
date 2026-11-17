"""Private notes on Jira issues: kept in QAVector, never sent until published.

A comment written in **Private** mode is a note of the person's own on the
issue. It is kept in the project's ``integrations-notes.json``, beside
``integrations.json``, readable by its owner only. It is shown among the
issue's comments, and is never sent to Jira - until somebody publishes it. Then
it is posted as a comment through the same write the page posts with, and the
note is gone: from then on it is Jira's comment.

Nothing here asks Jira anything.
"""

import json
import os
import secrets
import tempfile
import time

from . import store

FILE_NAME = "integrations-notes.json"
VERSION = 1
#: The most notes kept for one project; the oldest go first.
KEEP = 5000


class NoteError(ValueError):
    def __init__(self, message, kind="invalid"):
        super().__init__(message)
        self.kind = kind


def path_for(integrations_path):
    return os.path.join(os.path.dirname(os.path.abspath(store.resolve_path(integrations_path))), FILE_NAME)


def _load(path):
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise NoteError("The private notes could not be read: %s" % exc, "unreadable") from None
    notes = document.get("notes") if isinstance(document, dict) else None
    return [one for one in notes or [] if isinstance(one, dict) and one.get("id")]


def _write(path, notes):
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    descriptor, temp = tempfile.mkstemp(prefix=".notes-", dir=folder)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"version": VERSION, "notes": notes[-KEEP:]}, handle, ensure_ascii=False, indent=1)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def notes_for(integrations_path, connection, key):
    """The notes on one issue of one connection, the oldest first."""
    key = str(key or "").upper()
    return sorted((one for one in _load(path_for(integrations_path))
                   if one.get("connection") == connection and one.get("key") == key),
                  key=lambda one: one.get("created") or 0)


def get(integrations_path, note_id):
    return next((one for one in _load(path_for(integrations_path)) if one.get("id") == note_id), None)


def save(integrations_path, connection, key, body, files=(), note_id=""):
    """A new note, or a changed one. ``body`` is what the comment box holds:
    a document on Cloud, text on Server."""
    if not connection or not str(key or "").strip():
        raise NoteError("A note belongs to an issue of a connection.")
    if body in (None, "", {}) and not files:
        raise NoteError("A note needs some words.")
    path = path_for(integrations_path)
    notes = _load(path)
    now = time.time()
    if note_id:
        found = next((one for one in notes if one.get("id") == note_id), None)
        if found is None:
            raise NoteError("There is no such note any more.", "not_found")
        found.update(body=body, files=[str(one) for one in files or ()], updated=now)
        made = found
    else:
        made = {"id": "n-" + secrets.token_hex(6), "connection": connection, "key": str(key).upper(),
                "body": body, "files": [str(one) for one in files or ()], "created": now, "updated": now}
        notes.append(made)
    _write(path, notes)
    return made


def delete(integrations_path, note_id):
    path = path_for(integrations_path)
    notes = _load(path)
    kept = [one for one in notes if one.get("id") != note_id]
    if len(kept) == len(notes):
        return False
    _write(path, kept)
    return True


def shown(note, flavour):
    """A note as the page shows a comment: ``{id, author, created, updated,
    html, text, doc, lost, private, files}``."""
    from .jira import adf
    body = note.get("body")
    doc, lost = adf.editable(body) if flavour == "cloud" and isinstance(body, dict) else (None, [])
    created, updated = _iso(note.get("created")), _iso(note.get("updated"))
    return {"id": note["id"], "author": "You", "author_id": "", "created": created, "updated": updated,
            "edited": bool(updated and updated != created),
            "html": adf.to_html(body) if body else "", "text": adf.to_text(body, flavour) if body else "",
            "doc": doc, "lost": lost, "private": True, "files": list(note.get("files") or [])}


def _iso(seconds):
    if not isinstance(seconds, (int, float)):
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%S.000%z", time.localtime(seconds))
