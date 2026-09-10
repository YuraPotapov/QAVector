"""What a *user* asks for, and how that becomes a launcher command line.

The Command page speaks the launcher's language: one control per flag, and the
user is expected to know that screenshots need ``screen`` in ``--report-level``
or the core ignores ``--report-screen`` entirely. This module is the other half
of that translation - a configuration written in the words of the job
(environment, accounts, scenarios, reports) and the rules that turn it into
flags.

It deliberately produces the *same* ``{flag name: value}`` state dict the Command
page produces, and hands it to :func:`commands.build_argv`. There is exactly one
place in the GUI that knows how to spell a command line, and this is not it: so
the two pages cannot drift into disagreeing about what a selection means, and
every rule the catalogue already enforces (drop the flow and report flags without
``--run-tests``, drop a value that equals its default, always append
``--events=-``) applies here for free.

No Qt in this module - it is data and rules, tested directly.
"""

import copy

from . import commands

# -- vocabularies the GUI offers ---------------------------------------------
USERS_ALL, USERS_PICK = "all", "pick"
EXT_ALL, EXT_NONE, EXT_PICK = "all", "none", "pick"
# Which backend logs (--server-log) a launch streams. "default" is the absence of
# the flag on the command line, i.e. the logs logsources.json marks as defaults for
# the environment; "off" is the absence of the feature.
LOGS_OFF, LOGS_DEFAULT, LOGS_ALL, LOGS_PICK = "off", "default", "all", "pick"
SCENARIOS_NONE, SCENARIOS_ALL = "none", "all"
SCENARIOS_PER_USER, SCENARIOS_PICK = "per_user", "pick"
REPORTS_RESULTS, REPORTS_FULL, REPORTS_CUSTOM = "results", "full", "custom"
SHOTS_OFF, SHOTS_FINISH, SHOTS_START_FINISH, SHOTS_EACH = ("off", "finish",
                                                           "start_finish", "each")

# The core's own lists, mirrored so this module works before --describe answers.
# Inventory.choices() prefers whatever the running core advertises.
ALL_ARTIFACTS = ["console", "dom", "result", "screen", "url"]
ALL_OVERLAY = ["tree", "progress", "status", "logs", "highlight", "notifications"]
LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]

SCENARIO_MODE_LABELS = {
    SCENARIOS_NONE: "Just open the windows",
    SCENARIOS_ALL: "Run every scenario",
    SCENARIOS_PER_USER: "Use each account's own list",
    SCENARIOS_PICK: "Chosen scenarios",
}
SHOT_LABELS = {
    SHOTS_OFF: "Off",
    SHOTS_FINISH: "At the end of each scenario",
    SHOTS_START_FINISH: "At the start and the end",
    SHOTS_EACH: "After every step",
}
REPORT_LABELS = {
    REPORTS_RESULTS: "Results only",
    REPORTS_FULL: "Full diagnostics",
    REPORTS_CUSTOM: "Chosen artifacts",
}

DEFAULTS = {
    "environment": "",                                  # "" = every environment
    "users": {"mode": USERS_ALL, "logins": []},
    "sessions": {"jobs": 1, "all_at_once": False, "auto_jobs": False,
                 "keep_open": True, "detach": False},
    "extensions": {"mode": EXT_ALL, "names": []},
    "server_logs": {"mode": LOGS_OFF, "names": []},
    "scenarios": {"mode": SCENARIOS_NONE, "selected": []},
    "reports": {"level": REPORTS_RESULTS, "artifacts": [], "always": False},
    "screenshots": {"mode": SHOTS_OFF},
    "overlay": {"enabled": False, "components": []},
    "advanced": {"url": "", "profile_prefix": "", "log_level": "INFO",
                 "reports_dir": "", "sessions_dir": ""},
}


def merged(config):
    """A complete config: ``config`` over :data:`DEFAULTS`.

    Saved configurations outlive the version that wrote them, so a missing
    section is normal rather than a bug - it means the config predates it.
    """
    result = copy.deepcopy(DEFAULTS)
    for key, value in (config or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key].update(value)
        else:
            result[key] = copy.deepcopy(value)
    return result


# -- translation --------------------------------------------------------------
def to_command_state(config, inventory=None):
    """The Command page's own state dict, built from a user configuration."""
    config = merged(config)
    users = config["users"]
    sessions = config["sessions"]
    extensions = config["extensions"]
    advanced = config["advanced"]

    state = {
        "--env": config["environment"],
        "--filter-users": (",".join(users["logins"])
                           if users["mode"] == USERS_PICK else ""),
        "--user": "",
        "--password": "",
        "--url": advanced["url"],
        "--user-session": advanced["profile_prefix"],
        "--sessions-dir": advanced["sessions_dir"],
        "--extensions": _extensions(extensions),
        "--server-log": _server_logs(config["server_logs"]),
        "--log-level": advanced["log_level"] or "INFO",
        "--detach": bool(sessions["detach"]),

        "--run-tests": _run_tests(config["scenarios"]),
        "--jobs": ("auto" if sessions["auto_jobs"] else
                   "all" if sessions["all_at_once"] else
                   str(int(sessions["jobs"] or 1))),
        "--execution-overlay": _overlay(config["overlay"], inventory),
        "--reports-dir": advanced["reports_dir"],
        "--close-after": not bool(sessions["keep_open"]),

        "--report-level": ",".join(report_level(config, inventory)),
        "--report-screen": _report_screen(config["screenshots"]),
        "--report-always": bool(config["reports"]["always"]),
    }
    return state


def argv(config, inventory=None):
    """Launcher arguments for this configuration."""
    return commands.build_argv(to_command_state(config, inventory))


def preview(config, inventory=None, core=None):
    """The command line as a developer would read it."""
    return commands.preview(to_command_state(config, inventory), core)


def report_level(config, inventory=None):
    """``--report-level`` as a list, with ``screen`` added when it is needed.

    The core only honours ``--report-screen`` when ``screen`` is among the
    requested artifacts, and merely warns otherwise (session_launcher's report
    validation). Asking for screenshots and silently getting none is exactly the
    kind of thing this page exists to prevent, so the artifact is added here.
    """
    config = merged(config)
    reports = config["reports"]
    known = list(inventory.choices("report_artifacts", ALL_ARTIFACTS)) if inventory \
        else list(ALL_ARTIFACTS)

    if reports["level"] == REPORTS_FULL:
        level = list(known)
    elif reports["level"] == REPORTS_CUSTOM:
        chosen = set(reports["artifacts"])
        level = [a for a in known if a in chosen]
    else:
        level = []                       # the core's own default: no flag at all

    if config["screenshots"]["mode"] != SHOTS_OFF:
        if not level:
            # "Results only, plus the screenshots I asked for."
            level = [a for a in known if a in ("result", "screen")]
        elif "screen" not in level:
            level = [a for a in known if a in set(level) | {"screen"}]
    return level


def _extensions(extensions):
    if extensions["mode"] == EXT_NONE:
        return "none"
    if extensions["mode"] == EXT_PICK:
        return ",".join(extensions["names"])
    return ""                            # no flag: the core installs its default set


def _server_logs(server_logs):
    """The --server-log value, or "" when the flag should not appear at all.

    LOGS_DEFAULT spells itself out rather than leaving the value off: build_argv
    omits any flag whose value is empty, so "the bare flag" is not something a
    {flag: value} form can say. The core accepts "default" as that word.
    """
    mode = server_logs["mode"]
    if mode == LOGS_ALL:
        return "all"
    if mode == LOGS_DEFAULT:
        return "default"
    if mode == LOGS_PICK:
        return ",".join(server_logs["names"])
    return ""                            # LOGS_OFF: no flag at all


def _run_tests(scenarios):
    mode = scenarios["mode"]
    if mode == SCENARIOS_ALL:
        return "all"
    if mode == SCENARIOS_PER_USER:
        return "config"
    if mode == SCENARIOS_PICK:
        return ",".join(scenarios["selected"])
    return ""                            # empty drops every flow and report flag


def _overlay(overlay, inventory=None):
    if not overlay["enabled"]:
        return ""
    known = list(inventory.choices("overlay_components", ALL_OVERLAY)) if inventory \
        else list(ALL_OVERLAY)
    chosen = [c for c in known if c in set(overlay["components"])]
    # Enabled with nothing ticked means "the whole HUD", which is what the bare
    # flag means to the core.
    return ",".join(chosen or known)


def _report_screen(screenshots):
    return {SHOTS_FINISH: "finish", SHOTS_START_FINISH: "start,finish",
            SHOTS_EACH: "each"}.get(screenshots["mode"], "")


# -- validation ---------------------------------------------------------------
def validate(config, inventory=None):
    """Problems that must be fixed before this configuration can run."""
    config = merged(config)
    problems = []

    users = config["users"]
    if users["mode"] == USERS_PICK and not users["logins"]:
        problems.append("Select at least one account, or switch back to all of them.")

    scenarios = config["scenarios"]
    if scenarios["mode"] == SCENARIOS_PICK and not scenarios["selected"]:
        problems.append("Pick at least one scenario, or choose "
                        "\"Just open the windows\".")

    extensions = config["extensions"]
    if extensions["mode"] == EXT_PICK and not extensions["names"]:
        problems.append("Pick at least one extension, or choose \"None\".")

    reports = config["reports"]
    if reports["level"] == REPORTS_CUSTOM and not reports["artifacts"]:
        problems.append("Choose at least one report artifact, or pick "
                        "\"Results only\".")

    jobs = config["sessions"]
    if not (jobs["all_at_once"] or jobs["auto_jobs"]) and int(jobs["jobs"] or 1) < 1:
        problems.append("Run at least one session at a time.")

    if inventory:
        known = set(inventory.env_aliases())
        alias = config["environment"]
        if alias and known and alias not in known:
            problems.append("Environment \"%s\" is not in the current "
                            "configuration." % alias)
        if users["mode"] == USERS_PICK:
            available = set(_available_logins(config, inventory))
            missing = [l for l in users["logins"] if l not in available]
            if missing and available:
                problems.append("These accounts are not in the chosen "
                                "environment: %s." % ", ".join(missing[:6]))
    return problems


def notes(config, inventory=None):
    """Advisories: things the run will simply ignore, said out loud.

    These do not block a run - the launcher would accept it - but a user who
    ticked "screenshots" and gets none because nothing was scripted deserves to
    be told rather than left guessing.
    """
    config = merged(config)
    result = []
    if config["scenarios"]["mode"] == SCENARIOS_NONE:
        ignored = []
        if config["screenshots"]["mode"] != SHOTS_OFF:
            ignored.append("screenshots")
        if config["reports"]["level"] != REPORTS_RESULTS or config["reports"]["always"]:
            ignored.append("reports")
        if config["overlay"]["enabled"]:
            ignored.append("the execution overlay")
        if not config["sessions"]["keep_open"]:
            ignored.append("closing the windows afterwards")
        if ignored:
            result.append("Nothing is scripted, so %s will not apply."
                          % _join(ignored))
    if (config["sessions"]["auto_jobs"] and config["sessions"]["keep_open"]
            and config["scenarios"]["mode"] != SCENARIOS_NONE):
        # Auto still rations the driving here - sessions stand aside between
        # scenarios - which frees processor time. It cannot free memory, because
        # every window was opened up front and stays open to be inspected.
        result.append("Windows stay open, so \"Auto\" can free processor time but "
                      "not memory: it pauses sessions between scenarios rather "
                      "than closing anything. Untick \"Leave the windows open\" "
                      "to let it free both.")
    if config["sessions"]["detach"] and config["scenarios"]["mode"] != SCENARIOS_NONE:
        result.append("Detaching leaves the windows running after the launcher "
                      "exits; the run still finishes first.")
    return result


def background_blockers(run_tests, inventory, logins=None):
    """What stops a run going in the background: ``[(scenario id, reason)]``.

    Empty means nothing does: every scenario ``run_tests`` resolves to is made only
    of steps that need no browser, which the core reports per scenario as
    ``browser_actions`` in --describe. ``run_tests`` is the --run-tests value either
    page builds; ``logins`` are the accounts whose own lists ``config`` means (None:
    every account). Resolved the way the core resolves it, so the menu cannot offer
    a run the launcher would refuse. A scenario this inventory cannot vouch for -
    not in it, or described by a core too old to say - blocks as surely as one
    that clicks. A reason with no scenario id is about the run as a whole.
    """
    value = str(run_tests or "").strip()
    if not value:
        return [("", "Nothing is scripted.")]
    if not inventory:
        return [("", "The launcher has not described its scenarios yet.")]
    rows = {row.get("id"): row for row in inventory.scenarios}
    if value == "all":
        wanted = [sid for sid, row in rows.items() if row.get("in_all", True)]
    else:
        if value == "config":
            chosen = None if logins is None else set(logins)
            items = [test for user in inventory.users
                     if chosen is None or user.get("login") in chosen
                     for test in user.get("tests") or ()]
        else:
            items = [part.strip() for part in value.split(",") if part.strip()]
        wanted = []
        for item in items:
            if item.startswith("tag:"):
                tag = item[len("tag:"):]
                wanted.extend(sid for sid, row in rows.items()
                              if tag in (row.get("tags") or ()))
            else:
                wanted.append(item)
    if not wanted:
        return [("", "No scenario matches.")]
    blockers = []
    for scenario_id in dict.fromkeys(wanted):          # de-duplicated, in order
        row = rows.get(scenario_id)
        if row is None:
            blockers.append((scenario_id, "not in the scenario list"))
        elif not isinstance(row.get("browser_actions"), list):
            blockers.append((scenario_id, "cannot tell whether it needs a browser"))
        elif row["browser_actions"]:
            blockers.append((scenario_id, ", ".join(row["browser_actions"])))
    return blockers


def selected_logins(config, inventory=None):
    """The logins this configuration would actually launch, in order.

    Empty when it cannot be known - no inventory yet, with "All accounts"
    chosen - so a caller must treat empty as "cannot say", not as "none".
    Recording asks this because it has to end up with exactly one.
    """
    config = merged(config)
    users = config["users"]
    if users["mode"] == USERS_PICK:
        return list(users["logins"])
    if inventory is None:
        return []
    return list(_available_logins(config, inventory))


def _available_logins(config, inventory):
    alias = config.get("environment") or ""
    if not alias:
        return inventory.logins()
    return inventory.logins(inventory.env_value(alias))


# -- how it reads -------------------------------------------------------------
def env_label(config, inventory=None):
    config = merged(config)
    alias = config["environment"]
    if not alias:
        return "Every environment"
    if inventory:
        for env in inventory.envs:
            if env.get("alias") == alias:
                origin = env.get("origin") or "no URL"
                return "%s (%s)" % (alias, origin)
    return alias


def users_label(config, inventory=None):
    config = merged(config)
    users = config["users"]
    if users["mode"] == USERS_ALL:
        if inventory:
            return "All %d accounts" % len(_available_logins(config, inventory))
        return "All accounts"
    count = len(users["logins"])
    if count <= 3:
        return ", ".join(users["logins"]) or "none selected"
    return "%d accounts (%s, …)" % (count, ", ".join(users["logins"][:2]))


def scenarios_label(config):
    config = merged(config)
    scenarios = config["scenarios"]
    if scenarios["mode"] != SCENARIOS_PICK:
        return SCENARIO_MODE_LABELS.get(scenarios["mode"], scenarios["mode"])
    chosen = scenarios["selected"]
    if len(chosen) == 1:
        return chosen[0]
    return "%d scenarios" % len(chosen)


def sessions_label(config, background=False):
    """How many run at once, and what becomes of the windows.

    ``background`` is RUN's menu -> In Background: the sessions are the same, but there
    are no windows to stay open or close.
    """
    config = merged(config)
    sessions = config["sessions"]
    at_once = ("as many as the machine allows" if sessions["auto_jobs"] else
               "all at once" if sessions["all_at_once"] else
               "one at a time" if int(sessions["jobs"] or 1) == 1
               else "%d at a time" % int(sessions["jobs"]))
    after = ("in the background" if background else
             "windows stay open" if sessions["keep_open"] else "windows close after")
    return "%s, %s" % (at_once, after)


def extensions_label(config):
    config = merged(config)
    extensions = config["extensions"]
    if extensions["mode"] == EXT_ALL:
        return "All available"
    if extensions["mode"] == EXT_NONE:
        return "None"
    return ", ".join(extensions["names"]) or "none selected"


def summarise(config, inventory=None):
    """``[(label, value)]`` rows describing the configuration in plain words."""
    config = merged(config)
    rows = [("Environment", env_label(config, inventory)),
            ("Users", users_label(config, inventory)),
            ("Sessions", sessions_label(config)),
            ("Extensions", extensions_label(config)),
            ("Scenarios", scenarios_label(config))]
    if config["scenarios"]["mode"] != SCENARIOS_NONE:
        level = report_level(config, inventory)
        rows.append(("Reports", "%s%s" % (
            ", ".join(level) if level else REPORT_LABELS[REPORTS_RESULTS].lower(),
            " · also on success" if config["reports"]["always"] else "")))
        rows.append(("Screenshots", SHOT_LABELS.get(config["screenshots"]["mode"],
                                                    "Off")))
        if config["overlay"]["enabled"]:
            rows.append(("Overlay", _overlay(config["overlay"], inventory)))
    advanced = config["advanced"]
    if advanced["url"]:
        rows.append(("Start URL", advanced["url"]))
    if advanced["profile_prefix"]:
        rows.append(("Profile prefix", advanced["profile_prefix"]))
    if (advanced["log_level"] or "INFO") != "INFO":
        rows.append(("Log level", advanced["log_level"]))
    for label, key in (("Reports to", "reports_dir"),
                       ("Profiles in", "sessions_dir")):
        if advanced[key]:
            rows.append((label, advanced[key]))
    return rows


def describe_line(config, inventory=None, background=False):
    """One line for a history row."""
    config = merged(config)
    parts = [env_label(config, inventory), users_label(config, inventory),
             scenarios_label(config), sessions_label(config, background)]
    return " · ".join(p for p in parts if p)


def _join(items):
    if len(items) == 1:
        return items[0]
    return "%s and %s" % (", ".join(items[:-1]), items[-1])
