"""Where things live, in a source checkout and in an installed build.

A source checkout is self-contained: profiles, reports and users.json all sit
next to session_launcher.py, and that is exactly what a developer wants. An
installed build cannot work that way - its own files are read-only (under /opt,
or Program Files), and its resources are inside a PyInstaller bundle that is
replaced wholesale on every upgrade. So the two roles that were one directory
have to become two:

  app_root()        read-only, ships with the app, replaced on upgrade:
                    flows/, extensions/, engine/hud.js, VERSION
  user_data_root()  writable, belongs to the user, never touched by an upgrade:
                    users.json, user_sessions/, reports/

In a checkout both answer the same directory, so nothing about the development
workflow changes. Frozen, user_data_root() moves to ~/QAVector - a
plain visible folder, because it holds Chrome profiles and run reports the user
is expected to open, not opaque application state.

Stdlib only, and no import of anything else in this project: session_launcher
imports it at module scope, and a plain launch must stay cheap.
"""

import os
import sys

# PyInstaller sets both; sys._MEIPASS is the bundle directory (onedir: the
# _internal/ folder next to the executable, where our datas land).
FROZEN = bool(getattr(sys, "frozen", False))

#: Name of the per-user directory created for an installed build. Deliberately
#: capitalised and in $HOME rather than hidden in ~/.local: the user browses it.
USER_DIR_NAME = "QAVector"

#: What it was called before 0.15.0, when QAVector was chrome-multi-session.
#: Moved to USER_DIR_NAME on first start, with a link left behind; read in its
#: place for as long as it has not been (see migrate_legacy_dir).
LEGACY_USER_DIR_NAME = "ChromeMultiSession"

#: Overrides everything, on every platform. The escape hatch for a second
#: installation, a shared data directory, or a test run.
HOME_ENV = "CMS_HOME"

#: Written by the installer beside the program, naming the directory the user
#: chose for their own files. It is the installer's only way to tell the app
#: something, and the app's only way to honour a choice made before it first ran.
INSTALL_CONFIG_NAME = "cms.ini"
INSTALL_CONFIG_KEY = "data_dir"


def app_root():
    """The directory holding the resources that ship with the app."""
    if FROZEN:
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.abspath(__file__))


def install_config_path():
    """The installer's cms.ini, or "" when there is none to read.

    Looked for beside the installation rather than beside the executable,
    because the two bundles sit in <install>/core/ and <install>/gui/ and share
    one answer - and then beside the executable too, for a flat layout.
    """
    if not FROZEN:
        return ""
    executable_dir = os.path.dirname(os.path.abspath(sys.executable))
    for directory in (os.path.dirname(executable_dir), executable_dir):
        candidate = os.path.join(directory, INSTALL_CONFIG_NAME)
        if os.path.isfile(candidate):
            return candidate
    return ""


def configured_data_root():
    """The directory the installer was told to use, or "".

    Parsed by hand rather than with configparser: this module is imported on
    every launch, promises stdlib-only and cheap, and the file it reads is two
    lines written by our own installer. A malformed or unreadable one is not an
    error - it just does not answer, and the default below applies.
    """
    path = install_config_path()
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8-sig") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return ""
    for line in lines:
        line = line.strip()
        if not line or line.startswith((";", "#", "[")):
            continue
        key, separator, value = line.partition("=")
        if separator and key.strip().lower() == INSTALL_CONFIG_KEY:
            value = value.strip().strip('"')
            if value:
                return os.path.abspath(os.path.expanduser(os.path.expandvars(value)))
    return ""


def user_data_root():
    """The directory holding everything the user owns.

    $CMS_HOME wins, then the directory chosen at install time, then the default:
    an installed build uses ~/QAVector - or ~/ChromeMultiSession while that has
    not been moved yet - and a source checkout uses the checkout itself, which is
    what it has always done.

    The environment variable stays on top of the installed choice on purpose -
    it is what runs a second copy against a scratch directory without
    reinstalling, and a test must never be able to touch the real one.
    """
    override = os.environ.get(HOME_ENV, "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    chosen = configured_data_root()
    if chosen:
        return chosen
    if FROZEN:
        return _home_folder()
    return app_root()


def _home_folder():
    """~/QAVector - or ~/ChromeMultiSession while that is still the real one.

    Until the move has happened, or where it could not, the old folder is where
    the user's sessions and credentials are, and starting on an empty new one
    would look like having lost them.
    """
    home = os.path.expanduser("~")
    new = os.path.join(home, USER_DIR_NAME)
    old = os.path.join(home, LEGACY_USER_DIR_NAME)
    if not os.path.exists(new) and os.path.isdir(old):
        return old
    return new


def migrate_legacy_dir(old, new):
    """Move ``old`` to ``new`` once, leaving a link at ``old``. Returns the one in use.

    The link is what makes the move safe rather than merely tidy: absolute paths
    into the old folder are written in places this code never sees - a runner's
    script in services.json, a desktop link, a test checkout's generated conf -
    and every one of them keeps working through it. So where no link can be made
    the folder goes back where it was and the old name stays the one in use.

    Nothing happens when ``new`` already exists (it is never overwritten), when
    ``old`` is not there, or when ``old`` is itself a link. Never raises.
    cms_gui.core.move_folder is the GUI's mirror of this; keep the two in step.
    """
    if os.path.lexists(new):
        return new
    if not os.path.isdir(old):
        return new
    if os.path.islink(old):
        return old
    try:
        os.rename(old, new)
    except OSError:
        return old          # in use, or on another device: stay where it is
    try:
        _link_directory(new, old)
    except OSError:
        try:
            os.rename(new, old)
        except OSError:
            return new
        return old
    return new


def _link_directory(target, link):
    """A link at ``link`` to the folder ``target``: a symlink, a junction on Windows.

    A junction there because a Windows symlink needs a privilege most accounts do
    not have, and a junction needs none.
    """
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(target, link)
    else:
        os.symlink(target, link, target_is_directory=True)


# -- the individual locations -------------------------------------------------
def sessions_dir():
    """Chrome profiles and the auto-login extensions generated per profile."""
    return os.path.join(user_data_root(), "user_sessions")


def reports_dir():
    """Run artifacts: screenshots, DOM dumps, result.json."""
    return os.path.join(user_data_root(), "reports")


def config_path():
    """The users.json the launcher reads when --config is not given."""
    return os.path.join(user_data_root(), "users.json")


def logsources_path():
    """The logsources.json --server-log reads: server connections and log files.

    Beside users.json rather than inside it: a log belongs to an environment, not
    to a login, and users.json's schema is part of the public API (see the README's
    Compatibility section). It sits in the user's data root for the same reason
    users.json does - it names real hosts and can carry an access token.
    """
    return os.path.join(user_data_root(), "logsources.json")


def bundled_flows_dir():
    """The flows tree that ships with the app: read-only, replaced on upgrade."""
    return os.path.join(app_root(), "flows")


def user_flows_dir():
    """The flows tree the user owns, where anything they write goes."""
    return os.path.join(user_data_root(), "flows")


def flows_search_path():
    """Where to look for a flow, nearest first.

    The user's own tree, then the one that ships with the app. Two things fall
    out of that order, and both are the point: a scenario recorded or edited here
    is found before anything bundled with the same id, and a scenario in the
    user's tree can still ``use:`` the blocks and named selectors the app ships,
    without copying them.

    In a source checkout the two are the same directory and the list collapses to
    one entry, so a checkout resolves flows exactly as it always has.
    """
    seen, path = set(), []
    for candidate in (user_flows_dir(), bundled_flows_dir()):
        key = os.path.normpath(os.path.abspath(candidate))
        if key not in seen:
            seen.add(key)
            path.append(candidate)
    return path


def flows_dir():
    """The flows tree to write to, and the first one searched."""
    return user_flows_dir()


def extensions_dir():
    """The vendored, unpacked Chrome extensions. Read-only; installs copy out."""
    return os.path.join(app_root(), "extensions")


def bundled_version():
    """The version written into the bundle at build time, or "" in a checkout.

    importlib.metadata cannot answer this in a frozen app - there is no installed
    distribution to look up - so the build script drops a VERSION file next to the
    other resources and session_launcher.version() falls back to it.
    """
    try:
        with open(os.path.join(app_root(), "VERSION"), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


# -- first run ----------------------------------------------------------------
def ensure_user_data_root(example_config=None):
    """Create the user's directories, and seed users.json if it is missing.

    Safe to call on every start: it only ever fills in what is absent, so an
    upgrade or a second launch leaves existing sessions, reports and credentials
    exactly where they are. Returns the root.

    users.json is seeded only when the data root is a directory of its own. In a
    source checkout the root *is* the checkout, and there the absence of users.json
    is meaningful - it is what sends a developer to --init-users-json - so nothing
    is written.

    The first start after the rename to QAVector moves ~/ChromeMultiSession to
    ~/QAVector here - only for an installed build on its default folder: never
    under $CMS_HOME, which is how a test or a second copy stays off the real one,
    and never onto a folder the installer was told to use, which is the user's.
    """
    if FROZEN and not os.environ.get(HOME_ENV, "").strip() and not configured_data_root():
        home = os.path.expanduser("~")
        migrate_legacy_dir(os.path.join(home, LEGACY_USER_DIR_NAME),
                           os.path.join(home, USER_DIR_NAME))
    root = user_data_root()
    for path in (root, sessions_dir(), reports_dir(),
                 # Scenarios are written here - by the editor, and later by the
                 # recorder - so the directory has to exist before either runs.
                 os.path.join(user_flows_dir(), "scenarios")):
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            return root  # read-only home or similar; the caller will fail louder
    if os.path.abspath(root) == os.path.abspath(app_root()):
        return root
    config = config_path()
    if not os.path.exists(config):
        source = example_config or os.path.join(app_root(), "users.example.json")
        try:
            with open(source, encoding="utf-8") as fh:
                text = fh.read()
            # 0600 from the start: this file grows real passwords.
            fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            pass  # no template bundled, or the file appeared underneath us
    return root


# -- talking to foreign executables -------------------------------------------
def clean_subprocess_env(env=None):
    """A copy of the environment safe to hand to a non-bundled executable.

    PyInstaller's bootloader prepends the bundle directory to LD_LIBRARY_PATH so
    the frozen app finds its own shared libraries - and children inherit it. Chrome
    started that way picks up our libssl/libstdc++ instead of the system's and
    dies. The bootloader stashes the original value in LD_LIBRARY_PATH_ORIG for
    exactly this purpose, so restore it (or drop the variable when there was none).

    A no-op outside a frozen build, and harmless anywhere the variables are unset.
    """
    result = dict(os.environ if env is None else env)
    for name in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH"):
        original = result.pop(name + "_ORIG", None)
        if original is not None:
            result[name] = original
        elif FROZEN:
            result.pop(name, None)
    return result
