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

import collections
import copy
from urllib.parse import urlsplit

from . import commands, launchvars

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
    SCENARIOS_NONE: "Open windows",
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
    # The users.json ``env`` string behind that alias. The alias is worked out
    # from the address and changes when another environment with the same host
    # turns up (localhost becomes localhost:8069 the day localhost:8070 is
    # added); the value is what the accounts and the profile folders are filed
    # under, so it is what a saved configuration holds on to.
    "environment_value": "",
    "users": {"mode": USERS_ALL, "logins": []},
    "sessions": {"jobs": 1, "all_at_once": False, "auto_jobs": False,
                 "keep_open": True, "detach": False, "maximized": False,
                 "no_password_prompts": False,
                 "lock_windows": False},
    # autologin: the launcher's own sign-in extension, in every profile and
    # outside the list - on unless a configuration turns it off.
    "extensions": {"mode": EXT_ALL, "names": [], "autologin": True},
    "server_logs": {"mode": LOGS_OFF, "names": []},
    # ``run_mode`` is which scenarios "Run scenarios" means while the windows
    # are only opened (mode none), so it comes back when Run is chosen again.
    "scenarios": {"mode": SCENARIOS_NONE, "selected": [], "run_mode": SCENARIOS_PICK},
    # Open the windows with the Scenario Recorder rather than run anything, and
    # the scenario the recording continues ("" for a new one).
    "record": False,
    "record_into": "",
    "reports": {"level": REPORTS_RESULTS, "artifacts": [], "always": False,
                "doc": False},
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


# -- where the windows open ---------------------------------------------------
#: Where an address came from, said the way the page labels it.
SOURCE_CONFIG = "this configuration"
SOURCE_ENV_ADDRESS = "environment address"
SOURCE_ENV = "environment"

#: Hosts served over plain http when an address carries no scheme - the rule the
#: core applies to an env value (session_launcher.env_origin), so an address
#: typed here means what the same text in users.json would.
_LOCAL_HOSTS = frozenset(("localhost", "127.0.0.1", "::1"))

#: (url, source, problem, inherited, parts, values): the address a run opens,
#: which of the three places it came from, what is wrong with it ("" if
#: nothing), what would apply if this configuration said nothing of its own,
#: the address again as ``[(text, variable name or "")]`` - which part a
#: variable put there - and the variables' ``launchvars.Value`` by name.
Address = collections.namedtuple("Address",
                                 "url source problem inherited parts values")


def _address(url="", source="", problem="", inherited="", parts=None, values=None):
    if parts is None:
        parts = [(url, "")] if url else []
    return Address(url, source, problem, inherited, parts, dict(values or {}))


def _filled(template, source, scope):
    """The :data:`Address` ``template`` makes once its variables are filled in."""
    scope = scope or launchvars.EMPTY
    result = launchvars.resolve(template, scope.definitions, scope.layers)
    if result.problems:
        return _address("", source, result.problems[0].message, "",
                        result.parts, result.values)
    url, problem = normalize_address(result.text)
    if problem:
        return _address("", source, "The start address %s." % problem
                        if source == SOURCE_CONFIG else
                        "The environment address %s." % problem, "",
                        result.parts, result.values)
    parts = list(result.parts)
    lead = len(url) - len(result.text.strip())
    if lead > 0 and url.endswith(result.text.strip()):
        parts.insert(0, (url[:lead], ""))      # the scheme the core needs, added
    return _address(url, source, "", "", parts, result.values)


def normalize_address(text):
    """``(url, problem)``: ``text`` as the absolute address the core needs.

    The core passes ``--url`` through untouched, and ``localhost:8069`` read
    as a URL has the scheme "localhost" - every window would then be keyed to
    an origin that does not exist, and the sign-in extension with it. So a
    missing scheme is added here, local hosts getting http and the rest https,
    and anything the core could not open is said rather than passed on.
    """
    text = (text or "").strip()
    if not text:
        return "", ""
    if "{{" in text or "}}" in text:
        return "", "has a {{...}} placeholder, which cannot be filled in yet"
    if any(char.isspace() for char in text):
        return "", "has a space in it"
    if "://" not in text:
        host = (urlsplit("//" + text).hostname or "").lower()
        local = host in _LOCAL_HOSTS or host.endswith(".localhost")
        text = "%s://%s" % ("http" if local else "https", text)
    split = urlsplit(text)
    if split.scheme.lower() not in ("http", "https"):
        return "", "has to start with http:// or https://"
    if not split.hostname:
        return "", "has no host"
    try:
        port = split.port
    except ValueError:            # not a number, or past 65535
        port = 0
    if port == 0:
        return "", "has a port outside 1-65535"
    return text, ""


def environment_value(config, inventory=None):
    """The users.json ``env`` string this configuration launches; "" for all.

    The stored value first: it is the environment's identity, while the alias
    beside it is only what it happened to be called when it was saved. An
    alias is looked up only for a configuration saved before the value was
    kept, and is passed on as it is when nothing knows it - the core then
    names what it does have.
    """
    config = merged(config)
    alias = config["environment"]
    if not alias:
        return ""
    value = config.get("environment_value") or ""
    if value:
        return value
    if inventory:
        known = inventory.env_value(alias)
        if known:
            return known
    return alias


def _env_row(value, inventory):
    for env in (inventory.envs if inventory else []):
        if env.get("value") == value:
            return env
    return None


def effective_url(config, inventory=None, env_urls=None, scope=None):
    """The :data:`Address` this configuration's windows open.

    One answer for every page that shows or runs an address: this
    configuration's own start address, else the environment's address, else the
    origin the core derives from the env value. ``env_urls`` maps an env value
    to its environment's address; ``scope`` (a ``launchvars.Scope``) fills in
    the ``{{variables}}`` either address may carry.
    """
    config = merged(config)
    inherited = None
    value = environment_value(config, inventory)
    if value:
        address = str((env_urls or {}).get(value) or "").strip()
        row = _env_row(value, inventory)
        origin = (row or {}).get("origin") or ""
        if address:
            inherited = _filled(address, SOURCE_ENV_ADDRESS, scope)
        elif origin:
            inherited = _address(origin, SOURCE_ENV)
    own = str(config["advanced"]["url"] or "").strip()
    if own:
        found = _filled(own, SOURCE_CONFIG, scope)
        return found._replace(inherited=inherited.url if inherited else "")
    if inherited:
        return inherited
    return _address()


# -- translation --------------------------------------------------------------
def to_command_state(config, inventory=None, env_urls=None, dirs=None, scope=None):
    """The Command page's own state dict, built from a user configuration.

    ``env_urls`` are the Environments page's addresses (see
    :func:`effective_url`) and ``dirs`` its default folders (``reports``,
    ``sessions``), which apply wherever this configuration names none of its
    own - defaults, not values copied into it.
    """
    config = merged(config)
    users = config["users"]
    sessions = config["sessions"]
    extensions = config["extensions"]
    advanced = config["advanced"]
    dirs = dirs or {}
    address = effective_url(config, inventory, env_urls, scope)

    state = {
        "--env": environment_value(config, inventory),
        "--filter-users": (",".join(users["logins"])
                           if users["mode"] == USERS_PICK else ""),
        "--user": "",
        "--password": "",
        # Only when it is not what --env supplies anyway: the core derives the
        # environment's origin itself, and saying it again is noise.
        "--url": address.url if address.source in (SOURCE_CONFIG,
                                                   SOURCE_ENV_ADDRESS) else "",
        "--user-session": advanced["profile_prefix"],
        "--sessions-dir": advanced["sessions_dir"] or dirs.get("sessions", ""),
        "--extensions": _extensions(extensions),
        "--server-log": _server_logs(config["server_logs"]),
        "--log-level": advanced["log_level"] or "INFO",
        "--detach": bool(sessions["detach"]),
        "--maximized": bool(sessions["maximized"]),
        "--no-password-prompts": bool(sessions["no_password_prompts"]),
        "--no-autologin": not extensions.get("autologin", True),

        "--run-tests": _run_tests(config["scenarios"]),
        "--jobs": ("auto" if sessions["auto_jobs"] else
                   "all" if sessions["all_at_once"] else
                   str(int(sessions["jobs"] or 1))),
        "--execution-overlay": _overlay(config["overlay"], inventory),
        "--reports-dir": advanced["reports_dir"] or dirs.get("reports", ""),
        "--close-after": not bool(sessions["keep_open"]),
        "--lock-windows": bool(sessions["lock_windows"]),

        "--report-level": ",".join(report_level(config, inventory)),
        "--report-screen": _report_screen(config["screenshots"]),
        "--report-always": bool(config["reports"]["always"]),
        "--report-doc": bool(config["reports"]["doc"]),
    }
    return state


def argv(config, inventory=None, env_urls=None, dirs=None, scope=None):
    """Launcher arguments for this configuration."""
    return commands.build_argv(to_command_state(config, inventory, env_urls, dirs,
                                                scope))


def preview(config, inventory=None, core=None, env_urls=None, dirs=None, scope=None):
    """The command line as a developer would read it."""
    return commands.preview(to_command_state(config, inventory, env_urls, dirs, scope),
                            core)


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
def validate(config, inventory=None, env_urls=None, scope=None):
    """Problems that must be fixed before this configuration can run."""
    config = merged(config)
    problems = []

    users = config["users"]
    if users["mode"] == USERS_PICK and not users["logins"]:
        problems.append("Select at least one account, or switch back to all of them.")

    scenarios = config["scenarios"]
    if scenarios["mode"] == SCENARIOS_PICK and not scenarios["selected"]:
        problems.append("Pick at least one scenario, or choose "
                        "\"Open windows\".")

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

    address = effective_url(config, inventory, env_urls, scope)
    if address.problem:
        problems.append(address.problem)
    into = config.get("record_into") or ""
    if config.get("record") and into and inventory is not None and inventory.scenarios:
        row = inventory.scenario(into)
        if not row:
            problems.append("The scenario to record into, %s, is not in the scenario "
                            "list any more. Choose another, or a new one." % into)
        elif not row.get("writable", True):
            problems.append("%s ships with the application and cannot be recorded "
                            "into. Choose one of your own, or a new one." % into)
    if not config["environment"] and not address.url:
        # The core refuses this outright - no --env and no --url leaves it no
        # address to open - so it is said here, before RUN, not after.
        problems.append("Choose an environment. \"All environments\" needs a start "
                        "address, and every account then opens that one address.")

    if inventory:
        missing_env = _missing_environment(config, inventory)
        if missing_env:
            problems.append(missing_env)
        if users["mode"] == USERS_PICK:
            available = set(_available_logins(config, inventory))
            missing = [l for l in users["logins"] if l not in available]
            if missing and available:
                problems.append("These accounts are not in the chosen "
                                "environment: %s." % ", ".join(missing[:6]))
    return problems


def _missing_environment(config, inventory):
    """Why the chosen environment cannot be launched; "" when it can.

    The value decides, when the configuration kept one: the alias may have
    moved on (another environment with the same host renames it) while the
    accounts are still filed under the value. Without a value, the alias has
    to match - and when it does not, the environments it could have meant are
    named, because "localhost" became "localhost:8069" rather than vanished.
    """
    alias = config["environment"]
    if not alias or not inventory.envs:
        return ""
    values = {env.get("value") for env in inventory.envs}
    stored = config.get("environment_value") or ""
    if stored:
        if stored in values:
            return ""
        return ("Environment \"%s\" (%s) is not in users.json any more. Choose "
                "another one." % (alias, stored))
    if alias in set(inventory.env_aliases()):
        return ""
    wanted = alias.lower()
    near = [env.get("alias", "") for env in inventory.envs
            if wanted in (env.get("alias") or "").lower()
            or wanted in (env.get("value") or "").lower()]
    if near:
        return ("Environment \"%s\" is not in users.json under that name any more. "
                "Choose one of: %s." % (alias, ", ".join(near[:6])))
    return ("Environment \"%s\" is not in users.json any more. Choose another "
            "one." % alias)


def notes(config, inventory=None, env_urls=None, scope=None):
    """Advisories: things the run will simply ignore, said out loud.

    These do not block a run - the launcher would accept it - but a user who
    ticked "screenshots" and gets none because nothing was scripted deserves to
    be told rather than left guessing.
    """
    config = merged(config)
    result = []
    if config["scenarios"]["mode"] == SCENARIOS_NONE and not config.get("record"):
        ignored = []
        if config["screenshots"]["mode"] != SHOTS_OFF:
            ignored.append("screenshots")
        if config["reports"]["level"] != REPORTS_RESULTS or config["reports"]["always"]:
            ignored.append("reports")
        if config["reports"]["doc"]:
            ignored.append("the step-by-step guide")
        if config["overlay"]["enabled"]:
            ignored.append("the execution overlay")
        if not config["sessions"]["keep_open"]:
            ignored.append("closing the windows afterwards")
        if config["sessions"]["lock_windows"]:
            ignored.append("locking the windows")
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
    if not config["environment"] and inventory is not None:
        address = effective_url(config, inventory, env_urls, scope)
        chosen = set(selected_logins(config, inventory))
        spanned = {user.get("env") for user in inventory.users
                   if user.get("login") in chosen}
        if address.url and len(spanned) > 1:
            # What the core warns about after the fact, said before it: there
            # is one address per run, whatever environment an account is from.
            result.append("Accounts from %d environments will all open %s."
                          % (len(spanned), address.url))
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


def account_scenarios(config, inventory=None):
    """What "each account's own list" adds up to for the accounts this would launch.

    Each entry as the Credentials page stores it - a scenario id or ``tag:name`` -
    de-duplicated, in account order. Empty with no inventory yet, which is
    "cannot say" here just as it is for :func:`selected_logins`.
    """
    if inventory is None:
        return []
    config = merged(config)
    chosen = set(selected_logins(config, inventory))
    env_value = environment_value(config, inventory) or None
    items = [test for user in inventory.users
             if user.get("login") in chosen
             and env_value in (None, user.get("env"))
             for test in user.get("tests") or ()]
    return list(dict.fromkeys(items))


def _available_logins(config, inventory):
    if not config.get("environment"):
        return inventory.logins()
    return inventory.logins(environment_value(config, inventory))


# -- how it reads -------------------------------------------------------------
def env_label(config, inventory=None):
    config = merged(config)
    alias = config["environment"]
    if not alias:
        return "Every environment"
    if inventory:
        row = _env_row(environment_value(config, inventory), inventory)
        if row is not None:
            # The environment's own name when the configuration carries one
            # (Launch Sessions puts today's in); otherwise today's alias, since
            # the one it was saved under may have moved on.
            name = alias if config.get("environment_value") else row.get("alias")
            return "%s (%s)" % (name or alias, row.get("origin") or "no URL")
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
    if config.get("record"):
        return "Record a scenario"
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


def windows_label(config):
    """How the windows open and whether they are locked; "" when neither is asked.

    The lock is left out when nothing is scripted: it is held while scenarios run,
    and notes() already says it will not apply.
    """
    config = merged(config)
    sessions = config["sessions"]
    parts = []
    if sessions["maximized"]:
        parts.append("maximized")
    if sessions["no_password_prompts"]:
        parts.append("no password offers")
    if not config["extensions"].get("autologin", True):
        parts.append("not signed in")
    if sessions["lock_windows"] and config["scenarios"]["mode"] != SCENARIOS_NONE:
        parts.append("locked while scenarios run")
    return " · ".join(parts)


def extensions_label(config):
    config = merged(config)
    extensions = config["extensions"]
    if extensions["mode"] == EXT_ALL:
        return "All available"
    if extensions["mode"] == EXT_NONE:
        return "None"
    return ", ".join(extensions["names"]) or "none selected"


def summarise(config, inventory=None, env_urls=None, scope=None):
    """``[(label, value)]`` rows describing the configuration in plain words."""
    config = merged(config)
    rows = [("Environment", env_label(config, inventory)),
            ("Users", users_label(config, inventory)),
            ("Sessions", sessions_label(config)),
            ("Extensions", extensions_label(config)),
            ("Scenarios", scenarios_label(config))]
    windows = windows_label(config)
    if windows:
        rows.append(("Windows", windows))
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
    address = effective_url(config, inventory, env_urls, scope)
    if address.source in (SOURCE_CONFIG, SOURCE_ENV_ADDRESS) and address.url:
        rows.append(("Start URL", "%s (%s)" % (address.url, address.source)))
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
