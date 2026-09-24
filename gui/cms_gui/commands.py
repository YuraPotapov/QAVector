"""The launcher's command vocabulary, declared once.

Every flag ``session_launcher.py`` accepts is described here - its group, the
kind of control that edits it, whether it needs ``--run-tests``, and the help
line it shows. The Command page renders itself from this list and
:func:`build_argv` turns a form state back into a command line, so adding a flag
to the core means adding one row here, not touching six widgets.

``tests/test_commands.py`` cross-checks this catalogue against the real
``--help`` output, so the two cannot drift apart silently.
"""

import collections

# kind:
#   flag   - a switch, present or absent
#   text   - free text (--url, --filter-users)
#   choice - one of `choices`
#   list   - comma-separated subset of `choices`
#   path   - a directory or file
#   int    - a number (or one of `choices`, e.g. "all")
Flag = collections.namedtuple(
    "Flag", "name kind group help placeholder choices needs_run_tests default "
            "needs_foreground")
Flag.__new__.__defaults__ = ("", None, False, None, False)

GENERAL = "General"
FLOW = "Flow execution"
REPORTS = "Reports"

# Order is the order the Command page renders, mirroring --help.
FLAGS = [
    Flag("--env", "choice", GENERAL,
         "Launch one environment, matched by short name. Also supplies its URL.",
         placeholder="(all environments)"),
    Flag("--filter-users", "text", GENERAL,
         "Only these logins, comma-separated.", placeholder="all"),
    Flag("--user", "text", GENERAL,
         "Launch a single user instead of the config list."),
    Flag("--password", "text", GENERAL,
         "Password to use, overriding the config's."),
    Flag("--url", "text", GENERAL,
         "Login URL; overrides the URL --env would supply.",
         placeholder="supplied by --env"),
    Flag("--user-session", "text", GENERAL,
         "Folder-name prefix for this run's profile dirs."),
    Flag("--sessions-dir", "path", GENERAL,
         "Where profiles and generated extensions are stored."),
    Flag("--extensions", "text", GENERAL,
         "Which extensions to install: names, ids, none, all.", placeholder="all"),
    Flag("--log-level", "choice", GENERAL, "Console log level.",
         choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO"),
    Flag("--detach", "flag", GENERAL,
         "Fire-and-forget: leave windows running after the launcher exits."),
    # No needs_run_tests: a plain launch streams these into the Run page's
    # session panels just as a --run-tests one does. Choices come from
    # --describe (log_sources), filtered to the environment being launched.
    # needs_foreground: with --detach the launcher exits the moment the windows
    # are up, taking its reader threads with it, so there is nothing left to
    # stream into. The launcher says so and carries on; the GUI does not build
    # the line at all.
    Flag("--server-log", "list", GENERAL,
         "Backend logs to stream into each panel, and keep in the report.",
         placeholder="the environment's defaults", needs_foreground=True),

    Flag("--run-tests", "text", FLOW,
         "Attach over CDP and run scenarios: all, config, or ids / tag:NAME.",
         placeholder="all"),
    Flag("--jobs", "int", FLOW,
         "Windows driven at once (1 = one after another).",
         choices=["all"], default="1", needs_run_tests=True),
    Flag("--execution-overlay", "list", FLOW,
         "In-page execution HUD components.",
         choices=["tree", "progress", "status", "logs", "highlight", "notifications"],
         needs_run_tests=True),
    Flag("--reports-dir", "path", FLOW,
         "Where run artifacts are written.", needs_run_tests=True),
    Flag("--close-after", "flag", FLOW,
         "Close the windows once the run finishes.", needs_run_tests=True),

    Flag("--report-level", "list", REPORTS,
         "Which artifacts to generate.",
         choices=["console", "dom", "result", "screen", "url"],
         needs_run_tests=True),
    Flag("--report-screen", "list", REPORTS,
         "When screenshots are captured (needs 'screen' in --report-level).",
         choices=["start", "each", "finish"], needs_run_tests=True),
    Flag("--report-always", "flag", REPORTS,
         "Produce a full report on success too.", needs_run_tests=True),
]

BY_NAME = {f.name: f for f in FLAGS}
GROUPS = [GENERAL, FLOW, REPORTS]

# Flags the GUI drives itself rather than exposing as form controls. The
# --flow-* ones belong to the Scenarios page: they are how it reads and writes
# scenario files, since the GUI has no YAML of its own.
GUI_OWNED = ("--config", "--events", "--control", "--describe", "--init-users-json",
             "--flow-show", "--flow-save", "--flow-delete", "--flow-import",
             "--selectors-show", "--selectors-save", "--from",
             # The Services & Logs page's "Open" buttons, not launch options.
             "--server-log-show", "--server-log-lines",
             # Where logsources.json is. A setting the GUI passes on every call
             # so the file it edits and the file a run reads are the same one -
             # not something anyone builds a command line out of by hand.
             "--log-sources",
             # And where the flows tree is, for the same reason: the Scenarios
             # page edits it and a run reads it. It was a form field until it
             # became Settings -> Scenarios; two places to say it meant the page
             # could be editing one tree while a run read another.
             "--flows-dir",
             # The Cycles page's editing surface, exactly as --flow-* is the
             # Scenarios page's: read, write, delete, import.
             "--cycle-show", "--cycle-save", "--cycle-delete", "--cycle-import",
             "--cycle-list",
             # And running one. Not a form field: a cycle run is a cycle, picked
             # on the page that draws it, plus the variables that cycle declares
             # - which a generic list of flags has no way to know the names of.
             # The page builds the command; the Command page builds launches.
             "--cycle-run", "--cycle-jobs", "--cycle-var",
             # And running part of one: the two chevrons beside Run, which act
             # on the step selected on the canvas. Naming a step by hand on a
             # command line would mean knowing the graph without seeing it.
             "--cycle-only", "--cycle-from", "--cycle-resume", "--cycle-reuse",
             # Where the cycles tree is, for the reason --flows-dir is here: the
             # page edits it and a run reads it, so two places to say it would
             # mean editing one tree while a run read another.
             "--cycles-dir",
             # The two stores a cycle run reads, and the commands that edit
             # them. Here for the same reason --cycles-dir is: Settings says
             # where each file lives and the page edits it through these, so a
             # second place to name the file would mean the page editing one
             # store while a run read another. A secret is doubly not a form
             # field - a value typed into a command line is a value in `ps`.
             "--cycle-secrets-file", "--cycle-secret-set",
             "--cycle-secret-list", "--cycle-secret-delete",
             "--cycle-secret-copy",
             "--cycle-memory-file", "--cycle-memory-list",
             "--cycle-memory-show", "--cycle-memory-forget",
             "--cycle-runs-dir",
             # The Inspector's Setup section: a plugin declares what it can be
             # asked outside a run, and the page renders and calls it. Nobody
             # composes one of these by hand.
             "--cycle-plugin-action",
             # The Subjects sidebar lists a cycle's sessions and deletes one;
             # both act on a row the page shows, not on anything typed.
             "--cycle-sessions", "--cycle-session-delete",
             # RUN ▾ -> "With Recorder" adds this; it is a mode, not a form field.
             "--recorder",
             # And RUN ▾ -> "In Background" adds this one, for the same reason.
             "--no-browser",
             "--help", "-h", "--version", "-V")

# One-shot commands offered in the Tools menu: (what it is called, what a
# developer calls it, what it runs). Two names because the menu is read in both
# modes and a flag is the answer in only one of them - see widgets.Phrasing.
ONE_SHOTS = [
    ("Launcher help", "Show --help", ["--help"]),
    ("Launcher version", "Show --version", ["--version"]),
    ("List extensions", "List extensions", ["--extensions=list"]),
    ("What the launcher reports", "Describe (JSON)", ["--describe"]),
]


def for_recording(args):
    """``args`` with everything that belongs to a run taken out.

    Recording is not running, so ``--run-tests`` goes - and with it every flag
    the launcher only accepts alongside it, or the launch is rejected before a
    window opens ("--execution-overlay requires --run-tests"). Reading the group
    off the catalogue rather than naming flags here means a new one is covered
    the day it is added.

    ``--flows-dir`` used to need an exception here, being the one flow flag a
    recording still wants - it says where the recording gets written. It is a
    setting now rather than a form field, so it is not in the catalogue at all
    and there is nothing to except: it rides along like --config does.
    """
    drop = {flag.name for flag in FLAGS if flag.needs_run_tests}
    drop.add("--run-tests")
    return [a for a in args if a.split("=", 1)[0] not in drop]


def for_background(args):
    """``args`` for RUN's menu -> In Background: the same run, with no browser at all.

    ``--no-browser`` goes first, ahead of --events (which build_argv always puts
    last). What only means something to a window comes out: ``--detach`` (there
    is none to leave running), ``--execution-overlay`` (no page to draw it in) and
    ``--report-screen`` (nothing to photograph). Everything else - --jobs above
    all - is the run exactly as it was configured.
    """
    drop = {"--detach", "--execution-overlay", "--report-screen"}
    return ["--no-browser"] + [a for a in args if a.split("=", 1)[0] not in drop]


def flags_for(group):
    return [f for f in FLAGS if f.group == group]


def build_argv(state, events=True):
    """Turn a form ``state`` ({flag name: value}) into launcher arguments.

    Rules mirror the launcher's own validation so the GUI cannot build a line it
    would reject: the flow-execution and report flags are dropped unless
    ``--run-tests`` is set, anything needing this process to stay alive is dropped
    under ``--detach``, and a flag left at its default is simply not passed.
    """
    args = []
    run_tests = str(state.get("--run-tests", "") or "").strip()
    detached = bool(state.get("--detach"))
    for flag in FLAGS:
        if flag.needs_run_tests and not run_tests:
            continue
        if flag.needs_foreground and detached:
            continue
        value = state.get(flag.name)
        if flag.kind == "flag":
            if value:
                args.append(flag.name)
            continue
        if isinstance(value, (list, tuple)):
            value = ",".join(str(v) for v in value)
        value = str(value or "").strip()
        if not value or value == (flag.default or ""):
            continue
        args.append("%s=%s" % (flag.name, value))
    if events:
        # Always last, and always on: the GUI's whole live view is this stream.
        args.append("--events=-")
        # And the way back: stopping one window without touching the others.
        args.append("--control=-")
    return args


def preview(state, core=None, events=True):
    """The command line as the user should read it (and can copy)."""
    args = build_argv(state, events=events)
    if core is not None and core.script:
        return core.display_argv(*args)
    return " ".join(["python3", "session_launcher.py"] + args)


def parse_help_flags(help_text):
    """Every ``--flag`` mentioned in the launcher's --help output.

    Used by the test that keeps this catalogue honest.
    """
    found = set()
    for line in help_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        for token in stripped.split():
            if token.startswith("--"):
                # "--flag[=VALUE]" is how --help writes an optional value, so
                # the bracket goes with the value rather than with the name.
                # Without this the test looked for a flag called
                # "--cycle-memory-list[" and reported the real one as missing.
                name = token.split("=")[0].split("[")[0].rstrip(",")
                if len(name) > 2:
                    found.add(name)
                break
    return found
