"""Provider-independent chat history. Originals keep their provider IDs and usage."""

import json
import os
import shutil
from collections import Counter
from contextlib import contextmanager

from . import sessions


def chain(session):
    return (session.get("lineage") or {}).get("root") or session["id"]


def sidebar(items):
    """Hide sources of a Join only in the sidebar. History and usage still resolve them."""
    joined_into = {}
    roots = set()
    parents = set()
    for item in items:
        link = item.get("lineage") or {}
        if link:
            roots.add(link["root"])
            if link.get("joined"):
                for entry in link["sources"]:
                    joined_into[entry["id"]] = item["id"]
        source = item.get("source") or {}
        if source.get("parent") and source.get("label") in ("Delegated", "Message"):
            parents.add(source["parent"])
    visible = [item for item in items if item["id"] not in joined_into]
    # For a re-join the visible chat contains every original, including the
    # earlier Join. Its visible representative wins regardless of scan order.
    for item in visible:
        link = item.get("lineage") or {}
        if link.get("joined"):
            for entry in link["sources"]:
                joined_into[entry["id"]] = item["id"]
    sizes = Counter(chain(item) for item in visible)
    delegated_parents = {joined_into.get(sid, sid) for sid in parents}
    found = []
    for item in visible:
        parent = (item.get("source") or {}).get("parent")
        found.append(dict(item, continuation_group=chain(item) in roots, can_join=sizes[chain(item)] > 1,
                          delegation_group=item["id"] in delegated_parents,
                          sidebar_parent=joined_into.get(parent, parent)))
    return found


def history_dir(root, session_id):
    return os.path.join(sessions.directory(root, session_id), "attachments", "chat-history")


def _skip_links(folder, names):
    return [name for name in names if os.path.islink(os.path.join(folder, name))]


def _own_snapshot(root, item, target):
    """Keep the source's recorded events and the private files those events reference."""
    where = sessions.directory(root, item["id"])
    os.mkdir(target, sessions.PRIVATE_DIR)
    count = 0
    fd = os.open(os.path.join(target, "events.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, sessions.PRIVATE_FILE)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        path = os.path.join(where, "events.jsonl")
        if os.path.islink(path):
            raise sessions.SessionError("The transcript is a symbolic link.")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as source:
                for line in source:
                    if not line.endswith("\n"):
                        break  # A live turn has not finished writing this event yet.
                    if line.strip():
                        json.loads(line)
                        output.write(line)
                        count += 1
    attachments = os.path.join(where, "attachments")
    if os.path.isdir(attachments) and not os.path.islink(attachments):
        def exclude(folder, names):
            ignored = _skip_links(folder, names)
            if folder == attachments:
                ignored.append("chat-history")
            return ignored
        shutil.copytree(attachments, os.path.join(target, "attachments"), ignore=exclude)
    return {"id": item["id"], "title": item.get("title") or "Untitled",
            "connection": item.get("connection_name") or item["connection"],
            "model": item.get("model") or "", "created": item.get("created") or 0, "events": count}


def snapshot(root, session, original, members, joined):
    target = history_dir(root, session["id"])
    os.makedirs(target, mode=sessions.PRIVATE_DIR)
    # A continuation copies the selected branch as the user saw it. A Join takes
    # every branch's latest record once; saved segments also cover deleted originals.
    live = {item["id"] for item in members}
    sources = {}
    for item in members:
        for entry in (item.get("lineage") or {}).get("sources", []):
            sid = entry["id"]
            if sid not in sources and sid not in live:
                source_dir = os.path.join(history_dir(root, item["id"]), sid)
                if not sessions.ID_PATTERN.fullmatch(sid) or os.path.islink(source_dir):
                    raise sessions.SessionError("Invalid conversation history reference.")
                shutil.copytree(source_dir, os.path.join(target, sid), ignore=_skip_links)
                sources[sid] = entry
        sources[item["id"]] = _own_snapshot(root, item, os.path.join(target, item["id"]))
    entries = sorted(sources.values(), key=lambda e: (e["created"], e["id"]))
    lineage = {"root": chain(original), "previous": original["id"], "joined": joined, "sources": entries,
               "prefix": sum(1 + e["events"] for e in entries) if joined else 0}
    sessions._write(os.path.join(target, "index.json"), lineage)
    return lineage


def inherited_events(root, session, after):
    """Yield the joined transcript with stable sequence numbers for paging and live updates."""
    lineage = session.get("lineage") or {}
    offset = 0
    if lineage.get("joined") and after < lineage["prefix"]:
        for entry in lineage["sources"]:
            start = offset + 1
            offset = start + entry["events"]
            if offset <= after:
                continue
            if start > after:
                yield dict(entry, type="history.session", seq=start, at=entry["created"])
            path = os.path.join(history_dir(root, session["id"]), entry["id"], "events.jsonl")
            with open(path, encoding="utf-8") as handle:
                for seq, line in enumerate(handle, start + 1):
                    if seq > after:
                        yield dict(json.loads(line), seq=seq, history_session=entry["id"])


@contextmanager
def sources_for(root, previous, join):
    # Serialize membership changes independently of running provider turns.
    lockfile = os.path.join(sessions.resolve_root(root), ".continuations.lock")
    with sessions.file_lock(lockfile, "Another chat continuation is being created. Try again."):
        original = sessions.load(root, previous)
        members = [original]
        if join:
            members = [s for s in sessions.listing(root)["sessions"] if chain(s) == chain(original)]
            visible = sidebar(members)
            if len(visible) < 2:
                raise sessions.SessionError("The chain has fewer than two visible chats.", "invalid")
        yield original, members
