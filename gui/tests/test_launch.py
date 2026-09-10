"""The translation from a user's answers to a launcher command line.

This is where the Launch Sessions page's promise is kept or broken: the user
never sees a flag, so every rule the launcher has about how flags combine has to
be applied here instead. These tests pin the ones a user could not possibly
guess - screenshots needing ``screen`` in the report level, the flow and report
flags being meaningless without scenarios - and the one rule that keeps the two
pages honest: the argv comes out of ``commands.build_argv``, not from here.
"""

import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import commands, core, launch


def _config(**sections):
    """A default configuration with some sections replaced."""
    config = copy.deepcopy(launch.DEFAULTS)
    config.update(sections)
    return config


def _flag(argv, name):
    """The value of ``--name=value`` in ``argv``, "" for a bare flag, None if absent."""
    for arg in argv:
        if arg == name:
            return ""
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


# ------------------------------------------------------------------ the defaults

def test_the_default_configuration_asks_for_nothing_but_the_event_stream():
    # Every default is the core's own, so a fresh page runs the plainest possible
    # launch: open the windows, sign in, touch nothing else.
    assert launch.argv(launch.DEFAULTS) == ["--events=-", "--control=-"]


def test_every_command_line_ends_with_the_event_stream():
    argv = launch.argv(_config(scenarios={"mode": launch.SCENARIOS_ALL,
                                          "selected": []}))
    assert argv[-2:] == ["--events=-", "--control=-"]


def test_a_partial_configuration_is_filled_in_from_the_defaults():
    # A configuration saved by an older version has no "overlay" section at all.
    config = {"environment": "localhost:8069"}
    assert launch.merged(config)["overlay"] == launch.DEFAULTS["overlay"]
    assert _flag(launch.argv(config), "--env") == "localhost:8069"


def test_merging_does_not_mutate_the_defaults():
    launch.merged({"users": {"mode": launch.USERS_PICK, "logins": ["admin"]}})
    assert launch.DEFAULTS["users"]["logins"] == []


# ------------------------------------------------------------------ selection

def test_choosing_accounts_filters_them_and_choosing_all_does_not():
    picked = _config(users={"mode": launch.USERS_PICK,
                            "logins": ["admin", "manager1"]})
    assert _flag(launch.argv(picked), "--filter-users") == "admin,manager1"
    assert _flag(launch.argv(launch.DEFAULTS), "--filter-users") is None


def test_extensions_all_passes_no_flag_but_none_has_to_say_so():
    # "all" is the core's default set, so silence is the right way to ask for it;
    # "none" is an instruction and has to be spoken.
    assert _flag(launch.argv(launch.DEFAULTS), "--extensions") is None
    none = _config(extensions={"mode": launch.EXT_NONE, "names": []})
    assert _flag(launch.argv(none), "--extensions") == "none"
    picked = _config(extensions={"mode": launch.EXT_PICK,
                                 "names": ["odoo_debug", "helper"]})
    assert _flag(launch.argv(picked), "--extensions") == "odoo_debug,helper"


def test_each_scenario_mode_maps_to_what_the_core_expects():
    def run_tests(mode, selected=()):
        return _flag(launch.argv(_config(
            scenarios={"mode": mode, "selected": list(selected)})), "--run-tests")

    assert run_tests(launch.SCENARIOS_NONE) is None
    assert run_tests(launch.SCENARIOS_ALL) == "all"
    assert run_tests(launch.SCENARIOS_PER_USER) == "config"
    assert run_tests(launch.SCENARIOS_PICK, ["auth.login", "tag:smoke"]) \
        == "auth.login,tag:smoke"


def test_sessions_at_once_and_closing_afterwards():
    config = _config(scenarios={"mode": launch.SCENARIOS_ALL, "selected": []},
                     sessions={"jobs": 4, "all_at_once": False,
                               "keep_open": False, "detach": False})
    argv = launch.argv(config)
    assert _flag(argv, "--jobs") == "4"
    assert "--close-after" in argv

    config["sessions"] = {"jobs": 4, "all_at_once": True,
                          "keep_open": True, "detach": True}
    argv = launch.argv(config)
    assert _flag(argv, "--jobs") == "all"
    assert "--close-after" not in argv
    assert "--detach" in argv


def test_one_session_at_a_time_is_the_default_and_is_not_passed():
    config = _config(scenarios={"mode": launch.SCENARIOS_ALL, "selected": []})
    assert _flag(launch.argv(config), "--jobs") is None


# ------------------------------------------------------------------ the rules a
# user could not guess

def test_asking_for_screenshots_adds_the_artifact_that_makes_them_happen():
    """The core ignores --report-screen unless 'screen' is a requested artifact.

    A user ticking "screenshots" and silently getting none is the exact failure
    this page exists to prevent, so the artifact is added on their behalf.
    """
    config = _config(scenarios={"mode": launch.SCENARIOS_ALL, "selected": []},
                     screenshots={"mode": launch.SHOTS_FINISH})
    argv = launch.argv(config)
    assert _flag(argv, "--report-screen") == "finish"
    assert "screen" in (_flag(argv, "--report-level") or "").split(",")


def test_screenshots_added_to_a_chosen_artifact_set_keep_the_canonical_order():
    config = _config(scenarios={"mode": launch.SCENARIOS_ALL, "selected": []},
                     reports={"level": launch.REPORTS_CUSTOM,
                              "artifacts": ["url", "console"], "always": False},
                     screenshots={"mode": launch.SHOTS_EACH})
    level = (_flag(launch.argv(config), "--report-level") or "").split(",")
    assert level == ["console", "screen", "url"]
    assert _flag(launch.argv(config), "--report-screen") == "each"


def test_screenshots_off_never_asks_for_the_screen_artifact_on_its_own():
    config = _config(scenarios={"mode": launch.SCENARIOS_ALL, "selected": []},
                     reports={"level": launch.REPORTS_CUSTOM,
                              "artifacts": ["result"], "always": False})
    argv = launch.argv(config)
    assert _flag(argv, "--report-level") == "result"
    assert _flag(argv, "--report-screen") is None


def test_full_diagnostics_asks_for_every_artifact():
    config = _config(scenarios={"mode": launch.SCENARIOS_ALL, "selected": []},
                     reports={"level": launch.REPORTS_FULL, "artifacts": [],
                              "always": True})
    argv = launch.argv(config)
    assert set((_flag(argv, "--report-level") or "").split(",")) \
        == set(launch.ALL_ARTIFACTS)
    assert "--report-always" in argv


def test_nothing_scripted_drops_every_flag_that_would_be_rejected():
    """No scenarios means no flow run, and the core refuses the flow flags then.

    The page lets a user tick screenshots and 8-at-once before deciding not to
    script anything; the resulting command must still be one the launcher accepts.
    """
    config = _config(sessions={"jobs": 8, "all_at_once": False,
                               "keep_open": False, "detach": False},
                     reports={"level": launch.REPORTS_FULL, "artifacts": [],
                              "always": True},
                     screenshots={"mode": launch.SHOTS_EACH},
                     overlay={"enabled": True, "components": []})
    argv = launch.argv(config)
    for flag in ("--jobs", "--report-level", "--report-screen",
                 "--execution-overlay", "--flows-dir", "--reports-dir"):
        assert _flag(argv, flag) is None, flag
    assert "--close-after" not in argv
    assert "--report-always" not in argv
    # And it says so, rather than dropping them behind the user's back.
    assert any("not apply" in note for note in launch.notes(config))


def test_the_overlay_enabled_with_nothing_ticked_means_the_whole_hud():
    config = _config(scenarios={"mode": launch.SCENARIOS_ALL, "selected": []},
                     overlay={"enabled": True, "components": []})
    assert _flag(launch.argv(config), "--execution-overlay") == ",".join(
        launch.ALL_OVERLAY)


def test_advanced_overrides_reach_the_command_line():
    config = _config(scenarios={"mode": launch.SCENARIOS_ALL, "selected": []},
                     advanced={"url": "http://localhost:8069/web/login",
                               "profile_prefix": "matrix", "log_level": "DEBUG",
                               "reports_dir": "/tmp/out",
                               "sessions_dir": "/tmp/profiles"})
    argv = launch.argv(config)
    assert _flag(argv, "--url") == "http://localhost:8069/web/login"
    assert _flag(argv, "--user-session") == "matrix"
    assert _flag(argv, "--log-level") == "DEBUG"


def test_the_default_log_level_is_not_passed():
    assert _flag(launch.argv(launch.DEFAULTS), "--log-level") is None


# ------------------------------------------------------------------ one argv builder

def test_the_command_line_comes_from_the_shared_builder():
    """Launch Sessions must not grow an argv builder of its own.

    Both pages produce the same kind of state dict and hand it to the same
    function; if that ever stopped being true, a selection would mean two
    different things depending on which page made it.
    """
    config = _config(environment="localhost:8069",
                     scenarios={"mode": launch.SCENARIOS_ALL, "selected": []})
    state = launch.to_command_state(config)
    assert launch.argv(config) == commands.build_argv(state)
    # And every key it produces is a flag the catalogue knows about.
    assert set(state) <= set(commands.BY_NAME)


def test_the_state_it_produces_covers_every_flag_the_command_page_offers():
    state = launch.to_command_state(launch.DEFAULTS)
    developer_only = {"--user", "--password"}
    missing = [f.name for f in commands.FLAGS
               if f.name not in state and f.name not in developer_only]
    assert not missing, "no way to reach: %s" % ", ".join(missing)


# ------------------------------------------------------------------ validation

def test_choosing_scenarios_without_choosing_any_is_a_problem():
    config = _config(scenarios={"mode": launch.SCENARIOS_PICK, "selected": []})
    assert any("scenario" in p for p in launch.validate(config))
    config["scenarios"]["selected"] = ["auth.login"]
    assert launch.validate(config) == []


def test_choosing_accounts_without_choosing_any_is_a_problem():
    config = _config(users={"mode": launch.USERS_PICK, "logins": []})
    assert any("account" in p for p in launch.validate(config))


def test_choosing_artifacts_without_choosing_any_is_a_problem():
    config = _config(scenarios={"mode": launch.SCENARIOS_ALL, "selected": []},
                     reports={"level": launch.REPORTS_CUSTOM, "artifacts": [],
                              "always": False})
    assert any("artifact" in p for p in launch.validate(config))


def test_an_account_missing_from_the_chosen_environment_is_reported():
    inventory = core.Inventory({
        "envs": [{"alias": "localhost:8069", "value": "localhost:8069",
                  "origin": "http://localhost:8069", "count": 1}],
        "users": [{"env": "localhost:8069", "login": "admin", "class": "admin"}],
    })
    config = _config(environment="localhost:8069",
                     users={"mode": launch.USERS_PICK,
                            "logins": ["admin", "ghost"]})
    problems = launch.validate(config, inventory)
    assert any("ghost" in p for p in problems)
    # The account that does exist is not complained about.
    assert not any("admin," in p or p.endswith("admin.") for p in problems)


def test_an_unknown_environment_is_reported():
    inventory = core.Inventory({"envs": [{"alias": "localhost:8069"}]})
    problems = launch.validate(_config(environment="staging"), inventory)
    assert any("staging" in p for p in problems)


def test_the_default_configuration_has_nothing_to_fix():
    assert launch.validate(launch.DEFAULTS) == []


# ------------------------------------------------------------------ how it reads

def test_the_summary_speaks_in_the_user_s_words():
    config = _config(environment="localhost:8069",
                     users={"mode": launch.USERS_PICK, "logins": ["admin"]},
                     scenarios={"mode": launch.SCENARIOS_ALL, "selected": []})
    text = " ".join("%s %s" % row for row in launch.summarise(config))
    for flag in ("--env", "--filter-users", "--run-tests", "--report-level"):
        assert flag not in text
    assert "admin" in text and "Every scenario" in text.replace("Run every", "Every")


def test_the_summary_hides_report_rows_when_nothing_is_scripted():
    labels = [label for label, _value in launch.summarise(launch.DEFAULTS)]
    assert "Screenshots" not in labels
    assert "Reports" not in labels


def test_a_history_line_names_the_four_things_that_identify_a_run():
    config = _config(environment="localhost:8069",
                     users={"mode": launch.USERS_PICK, "logins": ["admin"]},
                     scenarios={"mode": launch.SCENARIOS_PICK,
                                "selected": ["auth.login"]})
    line = launch.describe_line(config)
    assert "localhost:8069" in line and "admin" in line and "auth.login" in line


def test_auto_hands_the_number_to_the_machine():
    config = _config(scenarios={"mode": launch.SCENARIOS_ALL, "selected": []},
                     sessions={"jobs": 4, "all_at_once": False, "auto_jobs": True,
                               "keep_open": False, "detach": False})
    # The typed 4 is not passed: under Auto the core starts from the core count
    # and moves from there, and sending a number would look like a cap it must keep.
    assert _flag(launch.argv(config), "--jobs") == "auto"


def test_a_typed_number_is_passed_exactly_as_typed():
    config = _config(scenarios={"mode": launch.SCENARIOS_ALL, "selected": []},
                     sessions={"jobs": 6, "all_at_once": False, "auto_jobs": False,
                               "keep_open": False, "detach": False})
    assert _flag(launch.argv(config), "--jobs") == "6"


def test_auto_says_so_in_the_summary():
    config = _config(sessions={"jobs": 4, "all_at_once": False, "auto_jobs": True,
                               "keep_open": False, "detach": False})
    assert "as many as the machine allows" in launch.sessions_label(config)


def test_auto_with_the_windows_kept_open_still_runs():
    config = _config(scenarios={"mode": launch.SCENARIOS_ALL, "selected": []},
                     sessions={"jobs": 4, "all_at_once": False, "auto_jobs": True,
                               "keep_open": True, "detach": False})
    # Auto adjusts by holding the next window back and closing each as it ends,
    # so with every window kept open it has nothing to hand back. That is worth
    # saying, but it is not a reason to refuse the run.
    assert not any("Auto" in p for p in launch.validate(config))
    assert any("Auto" in n for n in launch.notes(config))


def test_auto_that_can_adjust_says_nothing():
    config = _config(scenarios={"mode": launch.SCENARIOS_ALL, "selected": []},
                     sessions={"jobs": 4, "all_at_once": False, "auto_jobs": True,
                               "keep_open": False, "detach": False})
    assert not any("Auto" in n for n in launch.validate(config) + launch.notes(config))


# --------------------------------------------------- who a recording would open

class _Inv:
    def __init__(self, logins):
        self._logins = logins

    def logins(self, env=None):
        return list(self._logins)

    def env_value(self, alias):
        return alias


def test_chosen_accounts_are_the_ones_a_recording_must_choose_between():
    config = _config(users={"mode": launch.USERS_PICK, "logins": ["a", "b", "c"]})
    assert launch.selected_logins(config, _Inv(["a", "b", "c", "d"])) == ["a", "b", "c"]


def test_all_accounts_resolves_through_the_inventory():
    config = _config(users={"mode": launch.USERS_ALL, "logins": []})
    assert launch.selected_logins(config, _Inv(["a", "b"])) == ["a", "b"]


def test_without_an_inventory_all_accounts_is_unknowable_not_empty():
    # The caller must read empty as "cannot say" and leave the choice to the core,
    # not as "no accounts" and launch nothing.
    config = _config(users={"mode": launch.USERS_ALL, "logins": []})
    assert launch.selected_logins(config, None) == []


# ------------------------------------------------------------- server logs
def _argv(**server_logs):
    config = dict(launch.DEFAULTS)
    config["server_logs"] = dict(launch.DEFAULTS["server_logs"], **server_logs)
    return launch.argv(config)


def test_off_by_default_no_flag_at_all():
    assert not any(a.startswith("--server-log") for a in launch.argv({}))


def test_the_default_mode_spells_itself_out():
    # build_argv drops a flag whose value is empty, so "the bare flag" cannot be
    # expressed by a form. The core takes "default" as the word for it.
    assert "--server-log=default" in _argv(mode=launch.LOGS_DEFAULT)


def test_all_and_picked_names_reach_the_command_line():
    assert "--server-log=all" in _argv(mode=launch.LOGS_ALL)
    assert "--server-log=app,nginx" in _argv(mode=launch.LOGS_PICK,
                                             names=["app", "nginx"])


def test_picking_nothing_passes_no_flag():
    # An empty --server-log= is an error in the core; the GUI must not build it.
    assert not any(a.startswith("--server-log") for a in _argv(mode=launch.LOGS_PICK))


def test_server_logs_work_without_run_tests():
    # Unlike the flow and report flags: a plain launch streams them into the Run
    # page's panels just the same.
    config = dict(launch.DEFAULTS)
    config["server_logs"] = {"mode": launch.LOGS_ALL, "names": []}
    config["scenarios"] = {"mode": launch.SCENARIOS_NONE, "selected": []}
    args = launch.argv(config)
    assert "--server-log=all" in args
    assert not any(a.startswith("--run-tests") for a in args)


def test_a_configuration_saved_before_this_feature_still_loads():
    # merged() treats a missing section as "the config predates it", which is the
    # whole reason saved Launch configurations survive an upgrade.
    old = {"environment": "localhost", "users": {"mode": launch.USERS_ALL,
                                                 "logins": []}}
    assert launch.merged(old)["server_logs"] == launch.DEFAULTS["server_logs"]
    assert not any(a.startswith("--server-log") for a in launch.argv(old))


def test_server_log_is_not_a_report_artifact_the_gui_offers():
    # --server-log decides the file; a second control for the same decision could
    # only ever contradict it.
    assert "server_log" not in launch.ALL_ARTIFACTS


def test_a_configuration_that_still_lists_it_does_not_pass_it_on():
    # It was briefly an artifact, so a saved configuration can still name it.
    # report_level() intersects with what the core advertises, which is what keeps
    # a stale name off the command line.
    config = dict(launch.DEFAULTS)
    config["scenarios"] = {"mode": launch.SCENARIOS_ALL, "selected": []}
    config["reports"] = {"level": launch.REPORTS_CUSTOM,
                         "artifacts": ["result", "server_log"], "always": False}
    level = [a for a in launch.argv(config) if a.startswith("--report-level")]
    assert level == ["--report-level=result"]


def test_fire_and_forget_drops_the_server_log_flag():
    """--detach and --server-log cannot both mean anything.

    The launcher exits the moment the windows are up, taking its reader threads
    with it, so there is nothing left to stream into. It says so and carries on;
    the GUI does not build the line at all.
    """
    config = dict(launch.DEFAULTS)
    config["server_logs"] = {"mode": launch.LOGS_ALL, "names": []}
    config["sessions"] = dict(launch.DEFAULTS["sessions"], detach=True)
    args = launch.argv(config)
    assert "--detach" in args
    assert not any(a.startswith("--server-log") for a in args)


def test_without_fire_and_forget_the_flag_is_built_as_usual():
    config = dict(launch.DEFAULTS)
    config["server_logs"] = {"mode": launch.LOGS_ALL, "names": []}
    config["sessions"] = dict(launch.DEFAULTS["sessions"], detach=False)
    assert "--server-log=all" in launch.argv(config)


# ------------------------------------------------ running in the background

def _background_inventory():
    return core.Inventory({
        "scenarios": [
            {"id": "restart", "tags": ["services"], "in_all": True,
             "browser_actions": []},
            {"id": "host", "tags": ["services", "smoke"], "in_all": True,
             "browser_actions": []},
            {"id": "clicks", "tags": ["smoke"], "in_all": True,
             "browser_actions": ["click", "fill"]},
            {"id": "manual_clicks", "tags": ["manual"], "in_all": False,
             "browser_actions": ["goto"]},
            # Described by a core that predates the field.
            {"id": "old_core", "tags": [], "in_all": True},
        ],
        "users": [{"login": "agent", "tests": ["restart"]},
                  {"login": "manager", "tests": ["clicks", "tag:services"]}],
    })


def test_nothing_scripted_cannot_run_in_the_background():
    assert launch.background_blockers("", _background_inventory()) == [
        ("", "Nothing is scripted.")]


def test_without_an_inventory_nothing_can_be_vouched_for():
    blockers = launch.background_blockers("restart", None)
    assert len(blockers) == 1 and blockers[0][0] == ""


def test_service_only_scenarios_can_run_in_the_background():
    inventory = _background_inventory()
    assert launch.background_blockers("restart,host", inventory) == []
    assert launch.background_blockers("tag:services", inventory) == []


def test_a_scenario_that_drives_a_page_says_what_it_does():
    inventory = _background_inventory()
    assert launch.background_blockers("restart,clicks", inventory) == [
        ("clicks", "click, fill")]
    # A tag pulls in everything carrying it, the page steps included.
    assert launch.background_blockers("tag:smoke", inventory) == [
        ("clicks", "click, fill")]


def test_all_means_what_the_core_would_run():
    # manual_clicks is not in --run-tests=all, so it does not block; old_core is,
    # and nobody can say it needs no browser.
    ids = [sid for sid, _r in launch.background_blockers("all", _background_inventory())]
    assert ids == ["clicks", "old_core"]


def test_each_accounts_own_list_is_judged_for_the_accounts_launched():
    inventory = _background_inventory()
    assert launch.background_blockers("config", inventory, ["agent"]) == []
    assert [sid for sid, _r in launch.background_blockers(
        "config", inventory, ["agent", "manager"])] == ["clicks"]
    # No list of accounts means every one of them.
    assert launch.background_blockers("config", inventory) != []
    assert launch.background_blockers("config", inventory, ["nobody"]) == [
        ("", "No scenario matches.")]


def test_what_the_inventory_cannot_vouch_for_blocks():
    inventory = _background_inventory()
    assert launch.background_blockers("ghost", inventory) == [
        ("ghost", "not in the scenario list")]
    assert launch.background_blockers("old_core", inventory) == [
        ("old_core", "cannot tell whether it needs a browser")]


def test_the_run_header_says_background_and_keeps_how_many_at_once():
    config = _config(sessions=dict(launch.DEFAULTS["sessions"], jobs=2))
    assert launch.sessions_label(config, background=True) == \
        "2 at a time, in the background"
    config = _config(sessions=dict(launch.DEFAULTS["sessions"], auto_jobs=True))
    assert launch.sessions_label(config, background=True) == \
        "as many as the machine allows, in the background"
    assert launch.sessions_label(config) == \
        "as many as the machine allows, windows stay open"
