"""Private notes on Jira issues: kept in QAVector, never sent until published.

* A note is kept in the project's own file, owner-readable, and Jira is asked
  nothing while it is written, changed, read or deleted.
* Publishing posts it as a comment through the page's own write - logged in
  Activity as any comment is - and then it is no longer a note. A publish Jira
  refuses keeps the note.
"""

import json
import os
import stat
import sys

import pytest

from integrations import activity, cli, notes, store

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_jira import FakeJira  # noqa: E402

TOKEN = "tok-SECRET-123"


def _issue():
    return {"key": "QA-142", "fields": {
        "summary": "Session expires", "updated": "2026-11-05T10:00:00.000+0000",
        "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
        "issuetype": {"name": "Bug"}, "priority": {"name": "High"},
        "project": {"key": "QA", "name": "Quality"}, "description": None,
        "reporter": {"displayName": "Reporter R"}, "created": "2026-11-01T09:00:00.000+0000"},
        "transitions": []}


@pytest.fixture
def project(tmp_path):
    made = []

    def build(answers=None):
        server = FakeJira(answers or {})
        made.append(server)
        paths = (str(tmp_path / "integrations.json"), str(tmp_path / "cyclesecrets.json"))
        entry, problems = store.upsert({"name": "Product team", "site": server.site, "email": "me@example.invalid"},
                                       paths[0], paths[1], new_token=TOKEN)
        assert not problems
        return server, paths, entry["id"]
    yield build
    for server in made:
        server.close()


def _cli(capsys, tmp_path, paths, command, document):
    source = tmp_path / ("%s.json" % command)
    source.write_text(json.dumps(document))
    code = cli.main([command, "--from=%s" % source, "--integrations-file=%s" % paths[0],
                     "--cycle-secrets-file=%s" % paths[1]])
    answer = json.loads(capsys.readouterr().out)
    assert (code == 0) == answer["ok"], (code, answer)
    return answer


DOC = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [
    {"type": "text", "text": "Ask the backend team first"}]}]}


def test_a_private_note_is_kept_here_and_never_sent(capsys, tmp_path, project):
    server, paths, cid = project()
    saved = _cli(capsys, tmp_path, paths, "note-save", {"connection": cid, "key": "qa-142", "body": DOC})
    note = saved["note"]
    assert note["private"] is True and note["author"] == "You"
    assert "Ask the backend team first" in note["html"] and note["doc"]["type"] == "doc"
    where = notes.path_for(paths[0])
    assert stat.S_IMODE(os.stat(where).st_mode) == 0o600
    changed = _cli(capsys, tmp_path, paths, "note-save", {"connection": cid, "key": "QA-142", "id": note["id"],
                                                          "body": "Asked them"})
    assert changed["note"]["id"] == note["id"] and changed["note"]["text"] == "Asked them"
    listed = _cli(capsys, tmp_path, paths, "notes", {"connection": cid, "key": "QA-142"})
    assert [one["id"] for one in listed["notes"]] == [note["id"]]
    assert _cli(capsys, tmp_path, paths, "notes", {"connection": cid, "key": "QA-1"})["notes"] == []
    _cli(capsys, tmp_path, paths, "note-delete", {"id": note["id"]})
    assert _cli(capsys, tmp_path, paths, "notes", {"connection": cid, "key": "QA-142"})["notes"] == []
    assert server.seen == []                                # Jira was asked nothing
    assert activity.read(paths[0]) == []


def test_publishing_posts_it_as_a_comment_and_it_is_no_longer_a_note(capsys, tmp_path, project):
    server, paths, cid = project({"POST /rest/api/3/issue/QA-142/comment": (201, {"id": "1001"}),
                                  "GET /rest/api/3/issue/QA-142?": _issue(),
                                  "GET /rest/api/3/issue/QA-142/comment": {"total": 0, "comments": []}})
    note = _cli(capsys, tmp_path, paths, "note-save", {"connection": cid, "key": "QA-142", "body": DOC})["note"]
    published = _cli(capsys, tmp_path, paths, "note-publish", {"id": note["id"]})
    assert published["note"] == note["id"] and published["result"]["issue"]["key"] == "QA-142"
    [post] = [one for one in server.seen if one.method == "POST"]
    assert "Ask the backend team first" in json.dumps(post.body)
    assert notes.get(paths[0], note["id"]) is None
    [line] = activity.read(paths[0])
    assert line["op"] == "comment" and line["ok"] is True and "backend" not in json.dumps(line)


def test_a_publish_jira_refuses_keeps_the_note(capsys, tmp_path, project):
    server, paths, cid = project({"POST /rest/api/3/issue/QA-142/comment": (403, {"errorMessages": ["no"]})})
    note = _cli(capsys, tmp_path, paths, "note-save", {"connection": cid, "key": "QA-142", "body": "Draft"})["note"]
    refused = _cli(capsys, tmp_path, paths, "note-publish", {"id": note["id"]})
    assert refused["ok"] is False and refused["kind"] == "forbidden"
    assert notes.get(paths[0], note["id"]) is not None


def test_an_empty_note_and_a_note_gone_are_refused(capsys, tmp_path, project):
    _server, paths, cid = project()
    assert _cli(capsys, tmp_path, paths, "note-save", {"connection": cid, "key": "QA-1", "body": ""})["ok"] is False
    assert _cli(capsys, tmp_path, paths, "note-delete", {"id": "n-gone"})["kind"] == "not_found"
    assert _cli(capsys, tmp_path, paths, "note-publish", {"id": "n-gone"})["kind"] == "not_found"
