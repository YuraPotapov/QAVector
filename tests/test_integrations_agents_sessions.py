"""Conversations kept in the project: the record, the list, and who holds one.

What is worth defending: an id can only ever name a folder under the sessions
root, a turn nobody is taking is never reported as running, and what was said
is read back exactly as it was written - whole, in order, across turns.
"""

import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.agents import sessions                           # noqa: E402

CONNECTION = {"id": "claude", "name": "Claude", "kind": "claude"}


@pytest.fixture
def root(tmp_path):
    return str(tmp_path / "agent-sessions")


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    return str(path)


def _new(root, folder, **more):
    return sessions.create(root, CONNECTION, folder, "read", **more)


# ------------------------------------------------------------------ creating
def test_a_session_says_whom_it_is_with_where_and_how(root, folder):
    session = _new(root, folder, model="sonnet", title="Flaky login")
    assert sessions.ID_PATTERN.match(session["id"])
    assert (session["connection"], session["connection_name"], session["kind"]) == (
        "claude", "Claude", "claude")
    assert (session["folder"], session["mode"], session["model"]) == (folder, "read", "sonnet")
    assert session["source"] == {"label": "Chat", "detail": "", "ref": "", "record": False}
    assert session["turns"] == [] and session["provider_started"] is False
    assert sessions.load(root, session["id"]) == session


def test_whoever_started_it_says_so_in_its_own_words(root, folder):
    source = {"label": "Cycle nightly · step review", "detail": "Run 20261111-1"}
    assert _new(root, folder, source=source)["source"] == dict(source, ref="", record=False)


def test_a_creator_finds_its_conversations_by_its_own_ref_matched_exactly(root, folder):
    """A ref is the creator's key - a ticket, a task - and is never taken
    apart: one ref is not a prefix of another, and case counts."""
    task = _new(root, folder, title="Task", source={"label": "Jira QA-1", "ref": "jira:a:QA-1"})
    _new(root, folder, title="Other", source={"label": "Jira QA-12", "ref": "jira:a:QA-12"})
    _new(root, folder, title="Chat")
    found = sessions.listing(root, ref="jira:a:QA-1")
    assert [one["id"] for one in found["sessions"]] == [task["id"]] and found["total"] == 1
    assert sessions.listing(root, ref="jira:a:qa-1")["sessions"] == []
    assert sessions.listing(root)["total"] == 3
    sessions.append_turn(root, task["id"], {"started": 1.0, "ended": 2.0, "state": "completed",
                                            "cost_usd": 0.5}, [{"type": "user", "text": "hi"}])
    assert [one["session"] for one in sessions.activity(root, ref="jira:a:QA-1")] == [task["id"]]
    assert sessions.activity(root, ref="jira:a:QA-12") == []


def test_a_record_is_said_by_its_source_and_old_ones_by_where_they_came_from(root, folder):
    """A conversation opened elsewhere to talk in - a task handed over - goes on
    in the chat; a record of turns taken elsewhere is read. One saved before a
    source could say so is a record when it did not come from the chat."""
    talk = _new(root, folder, source={"label": "Jira QA-1", "ref": "jira:a:QA-1"})
    kept = _new(root, folder, source={"label": "Cycle n · step s", "record": True})
    assert (sessions.described(root, talk)["record"], sessions.described(root, kept)["record"]) == (
        False, True)
    old = dict(talk, source={"label": "Cycle n · step s", "detail": "Run 1"})
    assert sessions.is_record(old) is True
    assert sessions.is_record(dict(talk, source={"label": "Chat", "detail": ""})) is False


def test_a_turn_counts_its_own_cost_and_old_cumulative_records_are_settled(root, folder):
    """The CLI reports what a continued conversation cost so far. A turn saved
    before turns kept that apart holds the total as its cost: settled, the
    turns add up to the last total, not to the sum of totals."""
    session = _new(root, folder)
    session["turns"] = [{"n": 1, "cost_usd": 0.10}, {"n": 2, "cost_usd": None},
                        {"n": 3, "cost_usd": 0.25}]
    assert [one["cost_usd"] for one in sessions.own_costs(session)] == [0.10, None, 0.15]
    assert sessions.cost_so_far(session) == 0.25
    assert sessions.settle(dict(session))["cost_usd"] == 0.25
    session["turns"].append({"n": 4, "cost_usd": 0.05, "cost_total_usd": 0.30,
                             "tokens": {"input": 5, "output": 30},
                             "context": {"used": 1205, "window": 200000}})
    settled = sessions.settle(dict(session))
    assert settled["cost_usd"] == 0.30 and sessions.cost_so_far(session) == 0.30
    assert settled["tokens"] == {"input": 5, "output": 30}
    assert settled["context"] == {"used": 1205, "window": 200000}
    # A record's turns were calls of their own: what each reported is its own.
    record = dict(session, source={"label": "Cycle", "record": True},
                  turns=[{"n": 1, "cost_usd": 0.10}, {"n": 2, "cost_usd": 0.25}])
    assert sessions.settle(record)["cost_usd"] == 0.35


def test_everything_kept_is_readable_by_its_owner_only(root, folder):
    session = _new(root, folder)
    record = sessions.Record(root, session["id"], 1)
    record.add({"type": "user", "text": "hello"})
    record.close()
    if os.name == "nt":
        return
    where = sessions.directory(root, session["id"])
    assert stat.S_IMODE(os.stat(where).st_mode) == 0o700
    for name in ("session.json", "events.jsonl"):
        assert stat.S_IMODE(os.stat(os.path.join(where, name)).st_mode) == 0o600


def test_a_folder_that_is_not_there_is_refused(root, tmp_path):
    with pytest.raises(sessions.SessionError) as caught:
        _new(root, str(tmp_path / "nowhere"))
    assert caught.value.kind == "invalid"
    assert not os.path.exists(root) or os.listdir(root) == []


@pytest.mark.parametrize("bad", ["../outside", "..", "", "20261111-172345-3fa91c/..",
                                 "/etc", "20261111-172345-XYZXYZ", None])
def test_an_id_can_only_name_a_folder_of_its_own(root, bad):
    """An id arrives on a command line and becomes a path."""
    for call in (sessions.directory, sessions.load, sessions.delete):
        with pytest.raises(sessions.SessionError) as caught:
            call(root, bad)
        assert caught.value.kind == "invalid"


def test_a_session_that_is_not_there_says_so(root):
    with pytest.raises(sessions.SessionError) as caught:
        sessions.load(root, "20261111-172345-3fa91c")
    assert caught.value.kind == "not_found"


@pytest.mark.parametrize("message, title", [
    ("Why does test_login fail?", "Why does test_login fail?"),
    ("\n\n   spaced    out   \nsecond line", "spaced out"),
    ("x" * 200, "x" * 57 + "..."),
    ("   \n ", ""),
])
def test_a_conversation_nobody_named_is_called_by_how_it_began(message, title):
    assert sessions.title_from(message) == title


# ------------------------------------------------------------------ the list
def test_the_list_is_the_most_recently_used_first_and_pages(root, folder):
    made = [_new(root, folder, title="Session %d" % number) for number in range(5)]
    for number, session in enumerate(made):
        session["updated"] = 1000 + number
        sessions.save(root, session)
    page = sessions.listing(root, limit=2)
    assert [one["title"] for one in page["sessions"]] == ["Session 4", "Session 3"]
    assert (page["total"], page["more"]) == (5, True)
    last = sessions.listing(root, limit=2, offset=4)
    assert [one["title"] for one in last["sessions"]] == ["Session 0"]
    assert last["more"] is False


def test_the_list_narrows_by_what_was_typed(root, folder):
    _new(root, folder, title="Flaky login test")
    _new(root, folder, title="Refund flow", source={"label": "Cycle nightly", "detail": ""})
    assert [one["title"] for one in sessions.listing(root, query="LOGIN")["sessions"]] == [
        "Flaky login test"]
    assert [one["title"] for one in sessions.listing(root, query="nightly")["sessions"]] == [
        "Refund flow"]
    assert sessions.listing(root, query="nothing like it")["total"] == 0


def test_no_folder_yet_is_no_sessions_and_strays_are_not_sessions(root, folder):
    assert sessions.listing(root) == {"sessions": [], "total": 0, "more": False}
    _new(root, folder)
    os.makedirs(os.path.join(root, "not-a-session"))
    os.makedirs(os.path.join(root, "20200101-000000-abcdef"))       # no record in it
    assert sessions.listing(root)["total"] == 1


def test_deleting_removes_everything_kept_of_it(root, folder):
    session = _new(root, folder)
    sessions.delete(root, session["id"])
    assert not os.path.exists(sessions.directory(root, session["id"]))
    with pytest.raises(sessions.SessionError):
        sessions.delete(root, session["id"])


# ----------------------------------------------------------- who is taking a turn
def test_one_turn_at_a_time_and_nothing_is_deleted_from_under_it(root, folder):
    session = _new(root, folder)
    assert sessions.held(root, session["id"]) is False
    with sessions.lock(root, session["id"]):
        assert sessions.held(root, session["id"]) is True
        with pytest.raises(sessions.SessionError) as caught:
            with sessions.lock(root, session["id"]):
                pass
        assert caught.value.kind == "busy"
        with pytest.raises(sessions.SessionError) as caught:
            sessions.delete(root, session["id"])
        assert caught.value.kind == "busy"
    assert sessions.held(root, session["id"]) is False
    assert os.path.isdir(sessions.directory(root, session["id"]))


def test_a_turn_nobody_is_taking_is_never_called_running(root, folder):
    """The process was killed mid-turn: the file still says running."""
    session = _new(root, folder)
    session["turns"] = [{"n": 1, "state": "completed"}, {"n": 2, "state": "running"}]
    sessions.save(root, session)
    shown = sessions.described(root, session)
    assert (shown["running"], shown["state"]) == (False, "interrupted")
    assert [one["state"] for one in shown["turns"]] == ["completed", "interrupted"]
    with sessions.lock(root, session["id"]):
        shown = sessions.described(root, session)
        assert (shown["running"], shown["state"]) == (True, "running")
        assert shown["turns"][-1]["state"] == "running"
    # Shown, not rewritten: the record is the runner's to change.
    assert sessions.load(root, session["id"])["turns"][-1]["state"] == "running"


# ----------------------------------------------------------------- what was said
def test_events_are_numbered_through_the_conversation_and_read_back_whole(root, folder):
    session = _new(root, folder)
    first = sessions.Record(root, session["id"], 1)
    long = "x" * 50000
    assert first.add({"type": "user", "text": "hello"})["seq"] == 1
    assert first.add({"type": "text", "text": long})["seq"] == 2
    first.close()
    second = sessions.Record(root, session["id"], 2)
    entry = second.add({"type": "user", "text": "again"})
    second.close()
    assert (entry["seq"], entry["turn"]) == (3, 2) and entry["at"] > 0
    found = sessions.events(root, session["id"])
    assert [(one["seq"], one["turn"], one["type"]) for one in found] == [
        (1, 1, "user"), (2, 1, "text"), (3, 2, "user")]
    assert found[1]["text"] == long


def test_a_long_conversation_is_opened_at_its_end(root, folder):
    session = _new(root, folder)
    record = sessions.Record(root, session["id"], 1)
    for number in range(10):
        record.add({"type": "text", "text": str(number)})
    record.close()
    assert [one["seq"] for one in sessions.events(root, session["id"], limit=3)] == [8, 9, 10]
    assert [one["seq"] for one in sessions.events(root, session["id"], after=8)] == [9, 10]
    assert sessions.events(root, session["id"], after=10) == []


def test_a_line_cut_off_by_a_crash_is_stepped_over(root, folder):
    session = _new(root, folder)
    record = sessions.Record(root, session["id"], 1)
    record.add({"type": "user", "text": "hello"})
    record.close()
    path = os.path.join(sessions.directory(root, session["id"]), "events.jsonl")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"type": "text", "seq": 2, "te')
        handle.write("\n")
    again = sessions.Record(root, session["id"], 2)
    assert again.add({"type": "user", "text": "after"})["seq"] == 2
    again.close()
    assert [one["type"] for one in sessions.events(root, session["id"])] == ["user", "user"]


def test_what_was_never_said_is_nothing(root, folder):
    assert sessions.events(root, _new(root, folder)["id"]) == []


# ---------------------------------------------------------------------- the log
def test_the_log_is_every_turn_of_every_session_newest_first(root, folder):
    one = _new(root, folder, title="First")
    two = _new(root, folder, title="Second",
               source={"label": "Cycle nightly · step review", "detail": "Run 7"})
    one["turns"] = [{"n": 1, "started": 100, "state": "completed", "cost_usd": 0.01},
                    {"n": 2, "started": 300, "state": "failed", "error": "boom"}]
    two["turns"] = [{"n": 1, "started": 200, "state": "completed"}]
    sessions.save(root, one)
    sessions.save(root, two)
    log = sessions.activity(root)
    assert [(entry["title"], entry["n"], entry["state"]) for entry in log] == [
        ("First", 2, "failed"), ("Second", 1, "completed"), ("First", 1, "completed")]
    assert log[1]["source"]["label"] == "Cycle nightly · step review"
    assert log[0]["session"] == one["id"] and log[0]["folder"] == folder
    assert [entry["n"] for entry in sessions.activity(root, limit=1)] == [2]


def test_the_record_on_disk_is_plain_json_a_person_can_open(root, folder):
    session = _new(root, folder)
    path = os.path.join(sessions.directory(root, session["id"]), "session.json")
    assert json.load(open(path, encoding="utf-8"))["id"] == session["id"]


def test_a_source_keeps_whose_child_it_is():
    from integrations.agents import sessions
    kept = sessions.normal_source({"label": "Delegated", "parent": "s-1", "by": "Claude", "other": "x"})
    assert kept["parent"] == "s-1" and kept["by"] == "Claude" and "other" not in kept
    assert "parent" not in sessions.normal_source({"label": "Chat"})
