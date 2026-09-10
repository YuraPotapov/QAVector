"""cms_gui.logsourcesfile: the editor's model of the core's logsources.json.

The contract these tests exist to hold: whatever this module writes, the launcher
must be able to read. So the round-trip cases end by handing the file to
``engine.serverlog.parse_config`` itself rather than to a second opinion.
"""

import json
import os
import sys

import pytest

from cms_gui import logsourcesfile as lsf

# The core lives one directory up and is not installed; the GUI never imports it
# at runtime, but a test may - to check the two agree about the file.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

LOCAL = "localhost:8069"
DEV = "https://dev.example.com/"


def _rows():
    return (
        [lsf.ConnectionRow(name="here", type="local"),
         lsf.ConnectionRow(name="dev", type="ssh", host="dev.example.com",
                           user="deploy", identity="~/.ssh/id_ed25519")],
        [lsf.LogRow(name="app", connection="here", envs=[LOCAL], type="file",
                    target="/var/log/app.log", format="odoo", default=True),
         lsf.LogRow(name="nginx", connection="dev", envs=[DEV], type="file",
                    target="/var/log/nginx/error.log", format="nginx")],
    )


def _engine_reads(path):
    """Hand the written file to the launcher's own parser."""
    from engine import serverlog
    return serverlog.load_config(str(path))


# ------------------------------------------------------------------ round trip
def test_what_the_editor_writes_the_launcher_can_read(tmp_path):
    path = tmp_path / "logsources.json"
    connections, logs = _rows()
    lsf.save(str(path), connections, logs)
    config = _engine_reads(path)
    assert [c.name for c in config.connections] == ["here", "dev"]
    assert [s.name for s in config.for_env(LOCAL)] == ["app"]
    assert [s.name for s in config.for_env(DEV)] == ["nginx"]


def test_the_date_a_row_was_added_survives_and_the_launcher_ignores_it(tmp_path):
    path = tmp_path / "logsources.json"
    connections, logs = _rows()
    connections[0].added = "2026-08-27T10:00:00"
    logs[0].added = "2026-08-27T10:00:01"
    lsf.save(str(path), connections, logs)
    back_connections, back_logs = lsf.load(str(path))
    assert back_connections[0].added == "2026-08-27T10:00:00"
    assert back_logs[0].added == "2026-08-27T10:00:01"
    # The key is the editor's own. The launcher must read the file regardless,
    # and must not mistake it for anything of its own.
    config = _engine_reads(path)
    assert [s.name for s in config.for_env(LOCAL)] == ["app"]


def test_a_row_written_before_the_stamp_does_not_grow_one(tmp_path):
    path = tmp_path / "logsources.json"
    path.write_text(json.dumps({
        "connections": [{"name": "here", "type": "local"}],
        "logs": [{"name": "app", "connection": "here", "env": LOCAL,
                  "type": "file", "path": "/var/log/app.log"}]}))
    connections, logs = lsf.load(str(path))
    assert connections[0].added == "" and logs[0].added == ""
    lsf.save(str(path), connections, logs)
    written = json.loads(path.read_text())
    assert "added" not in written["connections"][0]
    assert "added" not in written["logs"][0]


def test_a_file_survives_a_load_save_cycle_unchanged(tmp_path):
    path = tmp_path / "logsources.json"
    lsf.save(str(path), *_rows())
    first = path.read_text()
    connections, logs = lsf.load(str(path))
    lsf.save(str(path), connections, logs)
    assert path.read_text() == first


def test_each_log_type_keeps_its_own_target_field(tmp_path):
    path = tmp_path / "logsources.json"
    connections = [lsf.ConnectionRow(name="here", type="local")]
    logs = [lsf.LogRow(name="a", connection="here", envs=[LOCAL], type="file",
                       target="/x.log"),
            lsf.LogRow(name="b", connection="here", envs=[LOCAL], type="docker",
                       target="odoo"),
            lsf.LogRow(name="c", connection="here", envs=[LOCAL], type="journal",
                       target="app.service"),
            lsf.LogRow(name="d", connection="here", envs=[LOCAL], type="http",
                       target="https://logs.example.com/stream")]
    lsf.save(str(path), connections, logs)
    written = json.loads(path.read_text())["logs"]
    assert [set(e) & {"path", "container", "unit", "url"} for e in written] == \
        [{"path"}, {"container"}, {"unit"}, {"url"}]
    # And the launcher agrees they are usable.
    assert len(_engine_reads(path).logs) == 4


def test_unknown_keys_round_trip_so_a_comment_is_not_eaten(tmp_path):
    path = tmp_path / "logsources.json"
    path.write_text(json.dumps({
        "connections": [{"name": "here", "type": "local", "_comment": "keep me"}],
        "logs": [{"name": "app", "connection": "here", "env": LOCAL,
                  "path": "/x", "_note": "and me"}]}))
    connections, logs = lsf.load(str(path))
    lsf.save(str(path), connections, logs)
    data = json.loads(path.read_text())
    assert data["connections"][0]["_comment"] == "keep me"
    assert data["logs"][0]["_note"] == "and me"


def test_the_singular_env_spelling_is_read_and_normalised(tmp_path):
    path = tmp_path / "logsources.json"
    path.write_text(json.dumps({
        "connections": [{"name": "here", "type": "local"}],
        "logs": [{"name": "app", "connection": "here", "env": LOCAL, "path": "/x"}]}))
    _connections, logs = lsf.load(str(path))
    assert logs[0].envs == [LOCAL]


def test_a_local_connection_does_not_keep_ssh_fields(tmp_path):
    # A host on a local connection reads as though it might connect somewhere.
    path = tmp_path / "logsources.json"
    row = lsf.ConnectionRow(name="here", type="local", host="left-over",
                            user="stale")
    lsf.save(str(path), [row], [])
    written = json.loads(path.read_text())["connections"][0]
    assert "host" not in written and "user" not in written


def test_a_byte_order_mark_does_not_break_the_editor(tmp_path):
    # The trap that lost saved configurations in 0.8.3.
    path = tmp_path / "logsources.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(
        {"connections": [{"name": "here", "type": "local"}],
         "logs": []}).encode("utf-8"))
    connections, _logs = lsf.load(str(path))
    assert [c.name for c in connections] == ["here"]


def test_a_missing_file_is_empty_not_an_error(tmp_path):
    assert lsf.load(str(tmp_path / "nope.json")) == ([], [])


def test_broken_json_names_the_file(tmp_path):
    path = tmp_path / "logsources.json"
    path.write_text("{not json")
    with pytest.raises(lsf.LogSourcesFileError) as exc:
        lsf.load(str(path))
    assert "logsources.json" in str(exc.value)


# ------------------------------------------------------------------ validation
def _problems(connections, logs):
    return "\n".join(lsf.validate(connections, logs))


def test_a_log_pointing_at_no_connection_is_reported():
    connections = [lsf.ConnectionRow(name="here", type="local")]
    logs = [lsf.LogRow(name="app", connection="typo", envs=[LOCAL], target="/x")]
    assert "no connection named 'typo'" in _problems(connections, logs)


def test_an_ssh_connection_without_a_host_is_reported():
    assert "needs a host" in _problems([lsf.ConnectionRow(name="dev", type="ssh")], [])


def test_a_duplicate_name_within_one_environment_is_reported():
    connections = [lsf.ConnectionRow(name="here", type="local")]
    logs = [lsf.LogRow(name="app", connection="here", envs=[LOCAL], target="/a"),
            lsf.LogRow(name="app", connection="here", envs=[LOCAL], target="/b")]
    assert "unique per environment" in _problems(connections, logs)


def test_the_same_name_on_two_environments_is_not_a_problem():
    connections = [lsf.ConnectionRow(name="here", type="local")]
    logs = [lsf.LogRow(name="app", connection="here", envs=[LOCAL], target="/a"),
            lsf.LogRow(name="app", connection="here", envs=[DEV], target="/b")]
    assert lsf.validate(connections, logs) == []


def test_a_log_with_no_environment_is_reported():
    connections = [lsf.ConnectionRow(name="here", type="local")]
    logs = [lsf.LogRow(name="app", connection="here", envs=[], target="/x")]
    assert "at least one environment" in _problems(connections, logs)


def test_a_missing_target_is_named_by_its_own_field():
    connections = [lsf.ConnectionRow(name="here", type="local")]
    assert "path is required" in _problems(
        connections, [lsf.LogRow(name="a", connection="here", envs=[LOCAL],
                                 type="file")])
    assert "container is required" in _problems(
        connections, [lsf.LogRow(name="a", connection="here", envs=[LOCAL],
                                 type="docker")])


def test_a_journal_with_no_unit_is_allowed():
    # No unit means the whole journal, which is a reasonable thing to ask for.
    connections = [lsf.ConnectionRow(name="here", type="local")]
    logs = [lsf.LogRow(name="sys", connection="here", envs=[LOCAL], type="journal")]
    assert lsf.validate(connections, logs) == []


def test_every_problem_is_reported_at_once():
    # The editor shows them all rather than making the user fix one, save, and
    # discover the next.
    problems = lsf.validate([lsf.ConnectionRow(name="", type="ssh")],
                            [lsf.LogRow(name="", connection="", envs=[])])
    assert len(problems) >= 4


def test_saving_an_invalid_file_writes_nothing(tmp_path):
    path = tmp_path / "logsources.json"
    with pytest.raises(lsf.LogSourcesFileError):
        lsf.save(str(path), [], [lsf.LogRow(name="a", connection="nope",
                                            envs=[LOCAL], target="/x")])
    assert not path.exists()


def test_nothing_the_editor_accepts_is_refused_by_the_launcher(tmp_path):
    """The failure the mirrored validation exists to prevent.

    A file this module accepts and the engine then refuses means the editor said
    "saved" and the next launch exits before opening a window. Covers each log
    type, a log bound to two environments, and the optional ssh fields.
    """
    from engine import serverlog

    connections = [lsf.ConnectionRow(name="here", type="local"),
                   lsf.ConnectionRow(name="dev", type="ssh", host="h", user="u",
                                     port="2222")]
    logs = [lsf.LogRow(name="app", connection="here", envs=[LOCAL, DEV],
                       type="file", target="/x", format="odoo", default=True),
            lsf.LogRow(name="sys", connection="dev", envs=[DEV], type="journal",
                       target="", format="syslog"),
            lsf.LogRow(name="gw", connection="dev", envs=[DEV], type="http",
                       target="https://x/stream", format="iso")]
    assert lsf.validate(connections, logs) == []
    path = tmp_path / "logsources.json"
    lsf.save(str(path), connections, logs)
    config = serverlog.load_config(str(path))      # must not raise
    assert {s.name for s in config.for_env(DEV)} == {"app", "sys", "gw"}


# ----------------------------------------------------------------- atomic save
def test_an_existing_file_is_kept_as_a_backup(tmp_path):
    path = tmp_path / "logsources.json"
    lsf.save(str(path), [lsf.ConnectionRow(name="one", type="local")], [])
    first = path.read_text()
    lsf.save(str(path), [lsf.ConnectionRow(name="two", type="local")], [])
    assert (tmp_path / "logsources.json.bak").read_text() == first
    assert "two" in path.read_text()


def test_no_temp_file_is_left_behind(tmp_path):
    path = tmp_path / "logsources.json"
    lsf.save(str(path), *_rows())
    assert [p.name for p in tmp_path.iterdir()] == ["logsources.json"]


def test_fingerprint_notices_a_change_underneath_the_editor(tmp_path):
    path = tmp_path / "logsources.json"
    assert lsf.fingerprint(str(path)) is None
    lsf.save(str(path), [lsf.ConnectionRow(name="one", type="local")], [])
    before = lsf.fingerprint(str(path))
    lsf.save(str(path), [lsf.ConnectionRow(name="one", type="local"),
                         lsf.ConnectionRow(name="two", type="local")], [])
    assert lsf.fingerprint(str(path)) != before


# ------------------------------------------------------------------ the path
# Unlike services.json this file is *also* the launcher's, so wherever it ends up
# the path has to travel with every call the GUI makes into the core.

def test_it_defaults_under_the_users_own_directory():
    assert lsf.default_path() == os.path.join(
        os.path.expanduser("~"), "QAVector", "logsources.json")


def test_a_configured_path_wins(tmp_path):
    chosen = tmp_path / "elsewhere.json"
    chosen.write_text("{}")
    assert lsf.resolve_path(str(chosen), "/old/logsources.json") == \
        (str(chosen), str(chosen))


def test_the_old_location_is_read_while_it_is_the_only_one(tmp_path):
    old = tmp_path / "checkout" / "logsources.json"
    old.parent.mkdir()
    old.write_text("{}")
    target = tmp_path / "new" / "logsources.json"
    read, write = lsf.resolve_path(str(target), str(old))
    # Read from where it is, written to where it belongs - and not moved until
    # Save says so, because the old one is still somebody's file.
    assert (read, write) == (str(old), str(target))
    assert old.exists()


def test_once_the_new_one_exists_the_old_is_left_behind(tmp_path):
    old = tmp_path / "old.json"
    old.write_text("{}")
    target = tmp_path / "new.json"
    target.write_text("{}")
    assert lsf.resolve_path(str(target), str(old)) == (str(target), str(target))


def test_with_neither_there_it_still_answers_where_it_would_go(tmp_path):
    target = tmp_path / "nothing-here.json"
    assert lsf.resolve_path(str(target), str(tmp_path / "also-not.json")) == \
        (str(target), str(target))
