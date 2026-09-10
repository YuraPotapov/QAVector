"""The flag catalogue and the argv it builds.

The last test here is the important one: it runs the *real* launcher's --help
and fails if the core grew a flag this GUI does not know about. That is the only
thing keeping a hand-written catalogue honest.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import commands, core as core_mod


def test_every_flag_has_a_group_and_a_help_line():
    for flag in commands.FLAGS:
        assert flag.group in commands.GROUPS, flag.name
        assert flag.help, flag.name
        assert flag.kind in ("flag", "text", "choice", "list", "path", "int"), flag.name


def test_empty_state_still_asks_for_the_event_stream():
    # The GUI's entire live view is --events=-, and --control=- is the way back:
    # stopping one window without touching the others. Neither is optional.
    assert commands.build_argv({}) == ["--events=-", "--control=-"]


def test_a_flag_left_at_its_default_is_not_passed():
    assert commands.build_argv({"--log-level": "INFO"}) == ["--events=-", "--control=-"]
    assert "--log-level=DEBUG" in commands.build_argv({"--log-level": "DEBUG"})


def test_switches_appear_without_a_value():
    args = commands.build_argv({"--detach": True})
    assert "--detach" in args and not any(a.startswith("--detach=") for a in args)


def test_flow_flags_are_dropped_without_run_tests():
    # The launcher exits on these; the GUI must not be able to build the line.
    state = {"--jobs": "4", "--close-after": True, "--execution-overlay": "all",
             "--report-level": "result", "--report-always": True}
    assert commands.build_argv(state) == ["--events=-", "--control=-"]


def test_flow_flags_survive_with_run_tests():
    state = {"--run-tests": "smoke", "--jobs": "4", "--close-after": True,
             "--execution-overlay": ["tree", "logs"]}
    args = commands.build_argv(state)
    assert args == ["--run-tests=smoke", "--jobs=4",
                    "--execution-overlay=tree,logs", "--close-after",
                    "--events=-", "--control=-"]


def test_lists_are_joined_with_commas():
    args = commands.build_argv({"--run-tests": "x", "--report-level": ["result", "screen"]})
    assert "--report-level=result,screen" in args


def test_preview_reads_like_the_command_you_would_type():
    text = commands.preview({"--env": "localhost", "--run-tests": "smoke"})
    assert text.startswith("python3 session_launcher.py")
    assert "--env=localhost" in text and "--events=-" in text


def test_parse_help_flags_finds_option_names():
    help_text = """
Options:
  --env=NAME                Launch one environment.
  --detach                  Fire-and-forget.
  --version, -V             Print the version.
"""
    assert commands.parse_help_flags(help_text) == {"--env", "--detach", "--version"}


# --------------------------------------------------------------- the sync check

def _core():
    script, interpreter = core_mod.autodetect()
    if not script:
        pytest.skip("no core checkout next to the GUI")
    return core_mod.Core(script, interpreter)


def test_catalogue_covers_every_flag_the_core_advertises():
    """No flag may exist in --help that the Command page cannot set.

    Anything deliberately driven by the GUI itself (--config, --events, the
    exit-immediately ones) is listed in GUI_OWNED; everything else has to be in
    the catalogue.
    """
    core = _core()
    documented = commands.parse_help_flags(core.help_text())
    if not documented:
        pytest.skip("could not read --help from the core")
    missing = documented - set(commands.BY_NAME) - set(commands.GUI_OWNED)
    # Deprecated aliases the core still accepts but no longer documents as the
    # way to do things.
    missing -= {"--odoo-debug", "--no-odoo-debug", "--filter-prefix"}
    assert not missing, ("the core has flags this GUI does not offer: %s"
                         % ", ".join(sorted(missing)))


def test_no_catalogue_flag_is_unknown_to_the_core():
    core = _core()
    documented = commands.parse_help_flags(core.help_text())
    if not documented:
        pytest.skip("could not read --help from the core")
    unknown = set(commands.BY_NAME) - documented
    assert not unknown, ("this GUI offers flags the core does not accept: %s"
                         % ", ".join(sorted(unknown)))


# ------------------------------------------------------- the dependency toggle

def _page(qapp):
    from cms_gui.pages.command import CommandPage
    from cms_gui.settings import Settings
    return CommandPage(Settings())


def test_run_tests_stays_editable_while_its_dependents_are_disabled(qapp):
    """The field that unlocks the group must never be disabled with it.

    Regression: --close-after and --report-always are bare checkboxes, so their
    parentWidget() is the whole panel. Disabling that greyed out --run-tests
    too, leaving no way to enable flow execution from the UI at all.
    """
    page = _page(qapp)
    page.set_state({"--run-tests": ""})
    assert page._controls["--run-tests"].isEnabled()
    assert page._flag_rows["--run-tests"].isEnabled()
    for name in ("--jobs", "--execution-overlay", "--close-after", "--report-level"):
        assert not page._flag_rows[name].isEnabled(), name


def test_filling_run_tests_enables_the_flow_and_report_flags(qapp):
    page = _page(qapp)
    page.set_state({"--run-tests": "tag:access"})
    # --flows-dir is deliberately absent: it is Settings -> Scenarios now, and
    # rides on every call from Core.argv the way --config does.
    for name in ("--jobs", "--execution-overlay", "--close-after",
                 "--report-level", "--report-screen", "--report-always"):
        assert page._flag_rows[name].isEnabled(), name


def test_clearing_run_tests_disables_them_again(qapp):
    page = _page(qapp)
    page.set_state({"--run-tests": "smoke"})
    page.set_state({"--run-tests": ""})
    assert page._controls["--run-tests"].isEnabled()
    assert not page._flag_rows["--jobs"].isEnabled()


def test_general_flags_never_depend_on_run_tests(qapp):
    page = _page(qapp)
    page.set_state({"--run-tests": ""})
    for name in ("--env", "--filter-users", "--user", "--password", "--url",
                 "--extensions", "--log-level", "--detach"):
        assert page._flag_rows[name].isEnabled(), name


# ------------------------------------------------------ recording is not running

def test_recording_drops_every_flag_that_needs_run_tests():
    """The launcher rejects them outright, before a window opens.

    A recording that inherited the Launch Sessions form died on
    "--execution-overlay requires --run-tests" - the GUI had removed --run-tests
    and left behind everything that only exists alongside it.
    """
    args = ["--env=localhost", "--filter-users=admin",
            "--run-tests=all", "--jobs=2", "--close-after",
            "--execution-overlay=tree,progress", "--reports-dir=/tmp/r",
            "--report-level=result,screen", "--report-screen=each",
            "--flows-dir=/tmp/flows", "--events=-"]
    kept = commands.for_recording(args)
    assert "--run-tests=all" not in kept
    for dropped in ("--jobs", "--close-after", "--execution-overlay",
                    "--reports-dir", "--report-level", "--report-screen"):
        assert not any(a.startswith(dropped) for a in kept), dropped


def test_recording_keeps_what_it_still_needs():
    args = ["--env=localhost", "--filter-users=admin", "--run-tests=all",
            "--sessions-dir=/tmp/s", "--flows-dir=/tmp/flows",
            "--extensions=odoo_debug", "--log-level=DEBUG", "--events=-"]
    kept = commands.for_recording(args)
    # Where the recording is written, and everything about the launch itself.
    assert "--flows-dir=/tmp/flows" in kept
    assert "--env=localhost" in kept and "--filter-users=admin" in kept
    assert "--sessions-dir=/tmp/s" in kept and "--extensions=odoo_debug" in kept
    assert "--log-level=DEBUG" in kept and "--events=-" in kept


def test_the_list_of_dropped_flags_comes_from_the_catalogue():
    """So a flow-execution flag added later is covered without touching this."""
    for flag in commands.FLAGS:
        if flag.needs_run_tests:
            assert commands.for_recording(["%s=x" % flag.name]) == []


def test_recording_still_gets_the_flows_dir_now_that_it_is_a_setting():
    # It used to be the one exception to the rule above. It is not in the
    # catalogue at all any more - Core.argv carries it - so nothing drops it.
    assert commands.for_recording(["--flows-dir=/tmp/flows"]) == ["--flows-dir=/tmp/flows"]


# ------------------------------------------------ running in the background

def test_background_is_the_same_run_with_no_browser():
    args = ["--env=localhost", "--run-tests=restart", "--jobs=auto", "--detach",
            "--execution-overlay=tree", "--report-level=result,screen",
            "--report-screen=finish", "--events=-", "--control=-"]
    kept = commands.for_background(args)
    assert kept[0] == "--no-browser" and kept.count("--no-browser") == 1
    # What only a window could use goes; the run as configured stays, --jobs
    # above all, and the event stream is still last.
    assert kept[1:] == ["--env=localhost", "--run-tests=restart", "--jobs=auto",
                        "--report-level=result,screen", "--events=-", "--control=-"]


def test_no_browser_is_a_mode_the_gui_adds_not_a_form_field():
    assert "--no-browser" in commands.GUI_OWNED
    assert "--no-browser" not in commands.BY_NAME
