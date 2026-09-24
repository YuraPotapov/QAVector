"""How the GUI names the core, in a checkout and in an installed build.

Two layouts have to work: ``<python> session_launcher.py ...`` next to the GUI's
source, and ``<core-exe> ...`` shipped beside a frozen GUI. Everything else in
the front-end goes through Core.argv, so getting that shape right is what makes
the packaged app behave like the checkout.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import core as core_mod


@pytest.fixture
def packaged(tmp_path, monkeypatch):
    """A frozen GUI with the core executable beside it, in the .deb's layout."""
    prefix = tmp_path / "opt" / "qavector"
    gui_exe = prefix / "gui" / "qavector-gui"
    core_exe = prefix / "core" / core_mod.CORE_EXE
    for path in (gui_exe, core_exe):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(gui_exe))
    monkeypatch.delenv(core_mod.CORE_EXE_ENV, raising=False)
    monkeypatch.setenv("CMS_HOME", str(tmp_path / "data"))
    return str(core_exe)


# ------------------------------------------------------------ what runs what

def test_a_py_file_needs_an_interpreter_and_an_executable_does_not():
    assert core_mod.needs_interpreter("/x/session_launcher.py")
    assert not core_mod.needs_interpreter("/x/qavector-core")
    assert not core_mod.needs_interpreter("")


def test_a_checkout_runs_the_script_through_a_python():
    script, interpreter = core_mod.autodetect()
    if not script:
        pytest.skip("no core checkout next to the GUI")
    assert script.endswith("session_launcher.py")
    assert core_mod.Core(script, interpreter).argv("--describe")[:2] == [
        interpreter, script]


def test_a_packaged_core_is_the_whole_command(packaged):
    core = core_mod.Core()
    assert core.script == packaged
    assert core.interpreter == ""
    assert core.argv("--describe") == [packaged, "--describe"]
    assert core.is_configured()


def test_a_stale_interpreter_setting_cannot_derail_a_packaged_core(packaged):
    # Someone who ran the GUI from source has core/interpreter in QSettings; it
    # must not become "python qavector-core" after they install.
    core = core_mod.Core(interpreter="/usr/bin/python3")
    assert core.argv() == [packaged]


def test_the_core_exe_env_var_wins(packaged, tmp_path):
    elsewhere = tmp_path / "other-core"
    elsewhere.write_text("", encoding="utf-8")
    os.environ[core_mod.CORE_EXE_ENV] = str(elsewhere)
    try:
        assert core_mod.Core().script == str(elsewhere)
    finally:
        del os.environ[core_mod.CORE_EXE_ENV]


# ------------------------------------------------------------- where it runs

def test_a_packaged_core_runs_in_the_users_data_directory(packaged, tmp_path):
    # Its own directory is read-only (/opt), and the config it reads by default
    # lives with the user's profiles and reports, not with the binaries.
    core = core_mod.Core()
    assert core.root == str(tmp_path / "data")
    assert core.config_path == str(tmp_path / "data" / "users.json")


def test_the_data_directory_is_created_before_a_core_is_spawned(packaged, tmp_path):
    # A working directory that does not exist stops the process from starting at
    # all - WinError 267 on Windows - and the core cannot create the directory
    # it is being started in. First launch of an installed build is exactly that
    # case: nothing has made ~/QAVector yet.
    core = core_mod.Core()
    assert not (tmp_path / "data").exists()
    assert core.spawn_dir() == str(tmp_path / "data")
    assert (tmp_path / "data").is_dir()


def test_reading_root_creates_nothing(packaged, tmp_path):
    # It is rendered in the Command page and the settings dialog; showing a path
    # must not bring it into being.
    assert core_mod.Core().root == str(tmp_path / "data")
    assert not (tmp_path / "data").exists()


def test_a_checkout_spawns_in_the_checkout_and_makes_no_data_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("CMS_HOME", str(tmp_path / "data"))
    script = tmp_path / "checkout" / "session_launcher.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    core = core_mod.Core(str(script), sys.executable)
    assert core.spawn_dir() == str(tmp_path / "checkout")
    assert not (tmp_path / "data").exists()


def test_the_folder_chosen_at_install_time_is_where_the_core_runs(tmp_path, monkeypatch):
    """The GUI must reach the same answer as runtime_paths, or the two disagree
    about where users.json is and the front-end edits a file the core ignores."""
    monkeypatch.delenv("CMS_HOME", raising=False)
    install = tmp_path / "install"
    (install / "core").mkdir(parents=True)
    (install / "gui").mkdir()
    core_exe = install / "core" / core_mod.CORE_EXE
    core_exe.write_text("", encoding="utf-8")
    chosen = tmp_path / "D" / "CMS Projects"
    (install / "cms.ini").write_text(
        "[Paths]%sdata_dir=%s%s" % ("\n", chosen, "\n"), encoding="utf-8")

    core = core_mod.Core(str(core_exe))
    assert core.root == str(chosen)
    assert core.config_path == str(chosen / "users.json")


def test_cms_home_still_wins_in_the_gui_too(tmp_path, monkeypatch):
    install = tmp_path / "install"
    (install / "core").mkdir(parents=True)
    core_exe = install / "core" / core_mod.CORE_EXE
    core_exe.write_text("", encoding="utf-8")
    (install / "cms.ini").write_text(
        "[Paths]%sdata_dir=%s%s" % ("\n", tmp_path / "chosen", "\n"),
        encoding="utf-8")
    monkeypatch.setenv("CMS_HOME", str(tmp_path / "scratch"))
    assert core_mod.Core(str(core_exe)).root == str(tmp_path / "scratch")


def test_display_argv_shortens_both_shapes(packaged):
    assert core_mod.Core().display_argv("--describe") == (
        "%s --describe" % core_mod.CORE_EXE)
    script = core_mod.Core("/x/session_launcher.py", "/y/bin/python3")
    assert script.display_argv("--describe") == "python3 session_launcher.py --describe"


# ---------------------------------------------------------------- the browser

def test_inventory_reports_a_missing_chrome():
    inventory = core_mod.Inventory({"chrome": {"path": "", "message": "Install Chrome."}})
    assert inventory.chrome_problem() == "Install Chrome."


def test_inventory_says_nothing_when_chrome_is_there():
    assert not core_mod.Inventory(
        {"chrome": {"path": "/usr/bin/google-chrome", "version": "Chrome 151",
                    "message": ""}}).chrome_problem()


def test_inventory_reports_a_chrome_that_is_present_but_cannot_run():
    # Ubuntu's snap shim: on PATH, executable, and not a browser. The core makes
    # that call and puts the answer in "message"; the GUI just relays it.
    inventory = core_mod.Inventory(
        {"chrome": {"path": "/usr/bin/chromium-browser", "version": "",
                    "message": "…exists but does not run…"}})
    assert inventory.chrome_problem() == "…exists but does not run…"


def test_inventory_never_warns_about_a_core_that_was_not_asked():
    # An older core has no "chrome" key; that is "cannot tell", not "missing".
    assert not core_mod.Inventory({"users": []}).chrome_problem()


def test_the_toolbar_summary_is_plain_words_not_a_flag_name():
    from cms_gui import core as core_mod

    inv = core_mod.Inventory({
        "envs": [{"alias": "localhost", "value": "localhost:8069"}],
        "users": [{"env": "localhost:8069", "login": "admin", "class": "Admin"}],
        "scenarios": [{"id": "smoke"}, {"id": "login"}],
        "extensions": [], "tags": [],
    })
    line = inv.summary()
    # The toolbar is read by someone deciding what to launch; "--describe"
    # answers a question they did not ask.
    assert "--describe" not in line
    assert "1 environments" in line and "1 accounts" in line and "2 scenarios" in line


def test_an_unread_inventory_says_so_without_jargon():
    from cms_gui import core as core_mod

    assert core_mod.Inventory().summary() == "nothing read yet"


# ------------------------------------------------------------- --log-sources
def test_the_log_sources_path_travels_with_every_call(tmp_path):
    script = tmp_path / "session_launcher.py"
    script.write_text("")
    core = core_mod.Core(str(script), "python3", "", "/data/logsources.json")
    assert "--log-sources=/data/logsources.json" in core.argv("--describe")


def test_no_flag_when_nothing_is_configured(tmp_path):
    script = tmp_path / "session_launcher.py"
    script.write_text("")
    core = core_mod.Core(str(script), "python3")
    assert not [a for a in core.argv("--describe") if a.startswith("--log-sources")]


# --------------------------------------------------------------- --flows-dir
def test_the_flows_dir_travels_with_every_call(tmp_path):
    """Every call, not just the runs.

    --describe is what the Scenarios page lists from and --flow-save is what it
    writes with, so a tree named only on the run line would leave the page
    editing one place and the run reading another.
    """
    script = tmp_path / "session_launcher.py"
    script.write_text("")
    core = core_mod.Core(str(script), "python3", "", "", "/data/flows")
    for command in ("--describe", "--flow-show=alpha", "--flow-save=alpha"):
        assert "--flows-dir=/data/flows" in core.argv(command), command


def test_no_flows_flag_when_the_setting_is_blank(tmp_path):
    # Blank means the core's own default, which it must be left to work out.
    script = tmp_path / "session_launcher.py"
    script.write_text("")
    core = core_mod.Core(str(script), "python3")
    assert not [a for a in core.argv("--describe") if a.startswith("--flows-dir")]


def test_the_launchers_own_location_is_computable_without_asking_it(tmp_path):
    # It is the one path --log-sources cannot distort, which is what makes it
    # usable as the "read the old one" fallback.
    script = tmp_path / "session_launcher.py"
    script.write_text("")
    core = core_mod.Core(str(script), "python3")
    assert core.legacy_log_sources == str(tmp_path / "logsources.json")


# --------------------------------------------------------------------- cycles
# The cycle commands go through the same _flow_json as the scenario ones, so
# what is worth checking is the argv each builds and that the Inventory reads
# what --describe carries. The core's own behaviour is covered in its checkout.

class _Recorder(core_mod.Core):
    """A Core that records the argv instead of running anything."""

    def __init__(self, answer=None, secrets_path="", memory_path=""):
        core_mod.Core.__init__(self, "/x/session_launcher.py", "/usr/bin/python3",
                               secrets_path=secrets_path,
                               memory_path=memory_path)
        self.calls = []
        self.answer = answer if answer is not None else {"ok": True}

    def run(self, *args, timeout=60):
        self.calls.append(list(args))
        import json
        return 0, json.dumps(self.answer), ""


def test_listing_cycles_asks_for_the_list():
    core = _Recorder({"cycles": []})
    assert core.cycle_list() == {"cycles": [], "problems": []}
    assert core.calls == [["--cycle-list"]]


def test_showing_one_names_it_on_the_command_line():
    core = _Recorder({"id": "demo"})
    core.cycle_show("demo")
    assert core.calls == [["--cycle-show=demo"]]


def test_deleting_and_importing_name_what_they_act_on():
    core = _Recorder()
    core.cycle_delete("demo")
    core.cycle_import("/tmp/thing.yaml")
    assert core.calls == [["--cycle-delete=demo"],
                          ["--cycle-import=/tmp/thing.yaml"]]


def test_saving_hands_the_document_over_in_a_file(tmp_path):
    """A temp file rather than stdin, so the same call can be run from a shell."""
    core = _Recorder()
    core.cycle_save("demo", {"yaml": "id: demo\n"})

    argv = core.calls[0]
    assert argv[0] == "--cycle-save=demo"
    assert argv[1].startswith("--from=")


def test_the_document_file_is_cleaned_up_afterwards():
    import os

    written = {}

    class Peeking(_Recorder):
        def run(self, *args, timeout=60):
            for arg in args:
                if arg.startswith("--from="):
                    path = arg.split("=", 1)[1]
                    written["path"] = path
                    written["text"] = open(path, encoding="utf-8").read()
            return _Recorder.run(self, *args, timeout=timeout)

    core = Peeking()
    core.cycle_save("demo", {"yaml": "id: demo\n"})

    assert "id: demo" in written["text"]
    assert not os.path.exists(written["path"])


def test_a_cycle_that_does_not_hold_is_a_payload_not_an_exception():
    """Which is the most useful thing these commands say."""
    core = _Recorder({"ok": False, "id": "demo",
                      "problems": ["b: needs 'ghost'"]})
    payload = core.cycle_save("demo", {"yaml": "x"})
    assert payload["ok"] is False
    assert payload["problems"] == ["b: needs 'ghost'"]


# --------------------------------------------------------------------- secrets
def test_listing_secrets_asks_for_names_and_nothing_else():
    core = _Recorder({"ok": True, "secrets": ["token"]})
    assert core.cycle_secrets("nightly")["secrets"] == ["token"]
    assert core.calls == [["--cycle-secret-list=nightly"]]


def test_a_secret_s_value_never_appears_on_the_command_line():
    """ps shows argv to every user on the machine, and history keeps it."""
    written = {}

    class Peeking(_Recorder):
        def run(self, *args, timeout=60):
            for arg in args:
                if arg.startswith("--from="):
                    written["text"] = open(arg.split("=", 1)[1],
                                           encoding="utf-8").read()
            return _Recorder.run(self, *args, timeout=timeout)

    core = Peeking()
    core.cycle_secret_set("nightly", "token", "ghp_example")

    argv = core.calls[0]
    assert argv[0] == "--cycle-secret-set=nightly:token"
    assert not any("ghp_example" in arg for arg in argv)
    assert "ghp_example" in written["text"]


def test_deleting_names_one_secret_or_the_whole_cycle():
    core = _Recorder()
    core.cycle_secret_delete("nightly", "token")
    core.cycle_secret_delete("nightly")
    assert core.calls == [["--cycle-secret-delete=nightly:token"],
                          ["--cycle-secret-delete=nightly"]]


def test_the_configured_store_travels_with_every_secret_command():
    core = _Recorder({"ok": True}, secrets_path="/data/secrets.json")
    core.cycle_secrets("nightly")
    assert core.calls[0] == ["--cycle-secret-list=nightly",
                             "--cycle-secrets-file=/data/secrets.json"]


def test_no_configured_store_sends_no_flag_and_lets_the_core_decide():
    core = _Recorder({"ok": True})
    core.cycle_secrets("nightly")
    assert core.calls[0] == ["--cycle-secret-list=nightly"]
    assert core.secrets_flag() == []


def test_the_store_is_not_named_on_commands_it_means_nothing_to():
    """--describe and a scenario run have no business knowing where it is."""
    core = _Recorder({"ok": True}, secrets_path="/data/secrets.json")
    assert "--cycle-secrets-file=/data/secrets.json" not in core.argv("--describe")


# ---------------------------------------------------------------------- memory
def test_reading_what_is_remembered_asks_for_the_keys():
    core = _Recorder({"ok": True, "keys": ["QA-1"]})
    assert core.cycle_memory()["keys"] == ["QA-1"]
    assert core.calls == [["--cycle-memory-list="]]


def test_a_prefix_narrows_the_listing():
    core = _Recorder({"ok": True, "keys": []})
    core.cycle_memory("web/")
    assert core.calls == [["--cycle-memory-list=web/"]]


def test_showing_and_forgetting_name_what_they_act_on():
    core = _Recorder()
    core.cycle_memory_show("QA-1")
    core.cycle_memory_forget("QA-1")
    assert core.calls == [["--cycle-memory-show=QA-1"],
                          ["--cycle-memory-forget=QA-1"]]


# -------------------------------------------------------------------- sessions
def test_listing_sessions_asks_for_every_cycle_s():
    core = _Recorder({"ok": True, "sessions": []})
    assert core.cycle_sessions()["sessions"] == []
    assert core.calls == [["--cycle-sessions="]]


def test_deleting_a_session_names_it_and_brings_the_memory_store():
    """Deleting one forgets its subject's record, so the store has to be the one in use."""
    core = _Recorder({"ok": True}, memory_path="/data/memory.json")
    core.cycle_session_delete("dev:QA-1")
    assert core.calls == [["--cycle-session-delete=dev:QA-1",
                           "--cycle-memory-file=/data/memory.json"]]


def test_the_configured_store_travels_with_every_memory_command():
    core = _Recorder({"ok": True}, memory_path="/data/memory.json")
    core.cycle_memory_show("QA-1")
    assert core.calls[0] == ["--cycle-memory-show=QA-1",
                             "--cycle-memory-file=/data/memory.json"]


def test_no_configured_store_sends_no_flag():
    core = _Recorder({"ok": True})
    assert core.memory_flag() == []


def test_the_two_stores_are_named_by_two_different_flags():
    """Memory is readable and secrets are not; conflating them would put what
    a cycle remembers behind encryption it does not need."""
    core = _Recorder({"ok": True}, secrets_path="/s.json",
                     memory_path="/m.json")
    assert core.secrets_flag() == ["--cycle-secrets-file=/s.json"]
    assert core.memory_flag() == ["--cycle-memory-file=/m.json"]


# ------------------------------------------------------------------- inventory
def test_the_inventory_carries_the_cycles_and_the_plugins():
    inventory = core_mod.Inventory({
        "cycles": [{"id": "nightly", "name": "Nightly"}],
        "cycle_plugins": [{"id": "command.shell", "name": "Shell Command"}]})

    assert [row["id"] for row in inventory.cycles] == ["nightly"]
    assert [one["id"] for one in inventory.cycle_plugins] == ["command.shell"]


def test_one_plugin_can_be_asked_for_by_id():
    inventory = core_mod.Inventory({
        "cycle_plugins": [{"id": "command.shell", "summary": "Run a command."}]})
    assert inventory.cycle_plugin("command.shell")["summary"] == "Run a command."


def test_asking_for_a_plugin_that_is_not_there_gives_an_empty_answer():
    assert core_mod.Inventory({}).cycle_plugin("nope") == {}


def test_a_core_that_predates_cycles_reads_as_having_none():
    """Which is what makes the page say so rather than break."""
    inventory = core_mod.Inventory({"users": [], "envs": []})
    assert inventory.cycles == []
    assert inventory.cycle_plugins == []
