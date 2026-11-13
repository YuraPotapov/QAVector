"""What the launcher said, kept for QCR (ADR-004 L.1): the cursor, the ring, the run's digest."""

from cms_gui.launcherfeed import LauncherFeed, label

TREE = {"id": "0", "label": "Login", "kind": "group", "children": [
    {"id": "0/0", "label": "goto /web/login", "kind": "step", "step_index": 0},
    {"id": "0/1", "label": "access.open_app", "kind": "group", "children": [
        {"id": "0/1/0", "label": "fill login", "kind": "step", "step_index": 1},
        {"id": "0/1/1", "label": "click Sign in", "kind": "step", "step_index": 2}]}]}


def test_a_run_s_digest_names_the_first_failed_step_its_blocks_and_artifacts():
    feed = LauncherFeed(clock=lambda: 10.0)
    feed.begin("--run-tests=login --env=Shop", "flows", by="agent:claude/s1", execution_id="e1")
    for event in ({"kind": "run.dir", "dir": "/runs/4"},
                  {"kind": "flow.start", "session": "u1", "id": "login", "scenario": "Login", "tree": TREE, "steps": 3},
                  {"kind": "step.end", "session": "u1", "index": 0, "status": "pass"},
                  {"kind": "step.end", "session": "u1", "index": 2, "status": "fail", "attempts": 3,
                   "message": "Timeout 5000ms"},
                  {"kind": "step.end", "session": "u1", "index": 2, "status": "error", "message": "later noise"},
                  {"kind": "flow.end", "session": "u1", "status": "fail", "passed": 2, "total": 3},
                  {"kind": "artifacts.written", "session": "u1", "dir": "/runs/4/u1", "files": ["a.png"]}):
        feed.event(event)
    feed.finish(1)
    run = feed.summary()
    assert (run["by"], run["execution_id"], run["exit_code"], run["run_dir"]) == ("agent:claude/s1", "e1", 1, "/runs/4")
    assert run["scenarios"] == [{
        "id": "login", "name": "Login", "session": "u1", "status": "fail", "passed": 2, "total": 3,
        "failed_step": {"index": 2, "label": "click Sign in", "blocks": ["access.open_app"], "status": "fail",
                        "message": "Timeout 5000ms", "attempts": 3},
        "artifacts": {"dir": "/runs/4/u1", "files": ["a.png"]}}]
    assert feed.status() == {"running": False, "run": run}


def test_parallel_sessions_keep_their_own_scenarios():
    feed = LauncherFeed()
    feed.begin("x", "flows")
    feed.event({"kind": "flow.start", "session": "a", "id": "one"})
    feed.event({"kind": "flow.start", "session": "b", "id": "two"})
    feed.event({"kind": "step.end", "session": "b", "index": 4, "status": "fail", "message": "boom"})
    feed.event({"kind": "flow.end", "session": "a", "status": "pass"})
    first, second = feed.summary()["scenarios"]
    assert first["status"] == "pass" and "failed_step" not in first
    assert second["failed_step"]["index"] == 4 and second["failed_step"]["label"] == ""


def test_a_launcher_that_went_first_leaves_its_scenarios_ended_not_running():
    feed = LauncherFeed()
    feed.begin("x", "flows")
    feed.event({"kind": "flow.start", "session": "a", "id": "one"})
    feed.finish(-9)
    assert feed.summary()["scenarios"][0]["status"] == "ended"


def test_output_is_read_by_cursor():
    feed = LauncherFeed()
    feed.begin("x", "flows")
    feed.log({"ts": "", "level": "INFO", "session": "u1", "text": "attached"})
    feed.event({"kind": "step.end", "session": "u1", "index": 0, "status": "pass", "tree": {"huge": True},
                "view": "/tmp/page.mhtml"})
    first = feed.read(0, 2)
    assert [entry["type"] for entry in first["entries"]] == ["run", "log"] and first["next"] == 2
    second = feed.read(first["next"])
    assert second["entries"][0]["fields"] == {"index": 0, "status": "pass"}, "no tree, no page copy"
    assert feed.read(second["next"]) == {"entries": [], "next": 3, "dropped": False, "running": True}


def test_a_reader_left_behind_is_told_entries_were_dropped():
    feed = LauncherFeed(keep=3)
    for number in range(6):
        feed.log({"text": str(number)})
    behind = feed.read(1)
    assert behind["dropped"] and [entry["text"] for entry in behind["entries"]] == ["3", "4", "5"]
    assert not feed.read(5)["dropped"]


def test_a_cursor_from_before_a_restart_reads_from_the_start():
    feed = LauncherFeed()
    feed.log({"text": "a"})
    assert [entry["text"] for entry in feed.read(9999)["entries"]] == ["a"]


def test_listeners_hear_each_entry_and_a_bad_one_stops_nothing():
    feed, heard = LauncherFeed(), []
    feed.listeners.extend([lambda: 1 / 0, lambda: heard.append(feed.last_seq)])
    feed.log({"text": "a"})
    feed.event({"kind": "x"})
    assert heard == [1, 2]


def test_long_text_is_cut():
    feed = LauncherFeed()
    feed.log({"text": "x" * 10000})
    assert len(feed.read()["entries"][0]["text"]) == 2000


def test_the_label_is_what_the_launcher_was_asked_to_do():
    assert label(["/usr/bin/python3", "/opt/q/session_launcher.py", "--run-tests=login", "--env=Shop"]) == \
        "--run-tests=login --env=Shop"
    assert label([]) == ""
