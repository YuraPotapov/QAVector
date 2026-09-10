"""The link to the core: where the launcher is, and what it says it can do.

The GUI never imports ``session_launcher`` - it spawns it. That keeps the two
environments independent (the GUI needs PySide6, the core needs playwright and
cryptography, and on Windows they are often not the same Python), and it means
the core stays the single source of truth: what environments, users, scenarios
and extensions exist all come from ``--describe``.

That boundary survives packaging unchanged, because an installed build ships the
core as its own executable next to the GUI's. The only thing that differs is the
shape of the command: a checkout runs ``<python> session_launcher.py ...``, an
installed build runs ``<core-exe> ...``. Both are expressed here as a script plus
an *optional* interpreter - an empty interpreter means the script runs itself.
"""

import json
import os
import subprocess
import sys
import tempfile

#: The core executable an installed build ships. Its name is fixed by the
#: PyInstaller spec (packaging/pyinstaller/core.spec).
CORE_EXE = "qavector-core" + (".exe" if os.name == "nt" else "")

#: Points the GUI at a core executable explicitly. The .deb's launcher wrapper
#: sets it; it is also the way to test one build's GUI against another's core.
CORE_EXE_ENV = "CMS_CORE_EXE"


class CoreError(Exception):
    """The launcher could not be run, or answered with something unusable."""


def _venv_python(root):
    """The interpreter inside a project's virtualenv, if it has one."""
    for parts in (("bin", "python3"), ("bin", "python"), ("Scripts", "python.exe")):
        candidate = os.path.join(root, ".venv", *parts)
        if os.path.isfile(candidate):
            return candidate
    return None


def needs_interpreter(path):
    """True when ``path`` is a Python source file rather than a program.

    The one rule that tells the two layouts apart, and it is the honest one: a
    ``.py`` has to be handed to an interpreter, anything else does not.
    """
    return bool(path) and os.path.splitext(path)[1].lower() in (".py", ".pyw")


#: The installer writes the directory the user picked into this file, beside the
#: installation. Mirrored from ``runtime_paths.INSTALL_CONFIG_NAME``.
INSTALL_CONFIG_NAME = "cms.ini"
INSTALL_CONFIG_KEY = "data_dir"

#: The user's own folder: where an installed core keeps users.json, profiles and
#: reports, and where services.json and logsources.json default to in any build.
#: Named for the project; the name it had before 0.15.0 is still read wherever
#: that folder has not been moved yet (see cms_gui.migrate).
USER_DIR_NAME = "QAVector"
LEGACY_USER_DIR_NAME = "ChromeMultiSession"


def home_folder():
    """``~/QAVector`` - or ``~/ChromeMultiSession`` while that is still the real one.

    The move happens once, at startup (cms_gui.migrate). Until it has - or where
    it could not, which leaves the old folder in place - the old one is where the
    user's files are, and reading an empty new one would look like losing them.
    """
    home = os.path.expanduser("~")
    new = os.path.join(home, USER_DIR_NAME)
    old = os.path.join(home, LEGACY_USER_DIR_NAME)
    if not os.path.exists(new) and os.path.isdir(old):
        return old
    return new


def move_folder(old, new):
    """Move ``old`` to ``new`` once, leaving a link at ``old``. Returns the one in use.

    A deliberate mirror of ``runtime_paths.migrate_legacy_dir`` in the core, which
    the GUI never imports. The link is what makes the move safe: absolute paths
    into the old folder live in services.json, the settings and desktop links,
    and every one of them keeps working through it. So where no link can be made
    the folder goes back where it was, and the old name stays the one in use.

    Nothing happens when ``new`` already exists - it is never overwritten - when
    ``old`` is not there, or when ``old`` is itself a link.
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
        _link_folder(new, old)
    except OSError:
        try:
            os.rename(new, old)
        except OSError:
            return new
        return old
    return new


def _link_folder(target, link):
    """A link at ``link`` to the folder ``target``: a symlink, a junction on Windows.

    A junction there because a Windows symlink needs a privilege most accounts do
    not have, and a junction needs none.
    """
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(target, link)
    else:
        os.symlink(target, link, target_is_directory=True)


def configured_data_root(program=""):
    """The data directory chosen at install time, or "".

    ``program`` is the core executable, because the answer belongs to the
    *installation* that owns it: cms.ini sits one level above <install>/core/
    and <install>/gui/, so either executable finds the same file.
    """
    anchor = os.path.abspath(program or sys.executable)
    directory = os.path.dirname(anchor)
    for where in (os.path.dirname(directory), directory):
        path = os.path.join(where, INSTALL_CONFIG_NAME)
        if not os.path.isfile(path):
            continue
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
                    return os.path.abspath(os.path.expanduser(
                        os.path.expandvars(value)))
        return ""
    return ""


def user_data_root(program=""):
    """Where an installed core keeps users.json, profiles and reports.

    A deliberate mirror of ``runtime_paths.user_data_root`` in the core, not an
    import of it: the GUI depends on PySide6 and nothing else, and the two are
    frozen into separate bundles. Keep the two in step - see runtime_paths.py.
    """
    override = os.environ.get("CMS_HOME", "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    chosen = configured_data_root(program)
    if chosen:
        return chosen
    return home_folder()


def ensure_user_data_root(program=""):
    """The user's data directory, created if this is the first run.

    The core creates its own directories on every start
    (``runtime_paths.ensure_user_data_root``) - but it is spawned *with this
    directory as its working directory*, and a working directory that does not
    exist yet means the process never starts at all: WinError 267 on Windows,
    ENOENT elsewhere. So the one directory the core cannot create for itself is
    created here, and the rest is still left to the core.

    Only the root, and only best-effort: seeding users.json stays the core's
    job, and a home that cannot be written fails louder further along.
    """
    root = user_data_root(program)
    try:
        os.makedirs(root, exist_ok=True)
    except OSError:
        pass
    return root


def frozen_core():
    """The bundled core executable, when this GUI is itself a frozen build.

    The installed layout puts the two bundles side by side
    (``<prefix>/gui/`` and ``<prefix>/core/``), so that is checked before the
    flat case of both executables sharing one directory.
    """
    if not getattr(sys, "frozen", False):
        return None
    override = os.environ.get(CORE_EXE_ENV, "").strip()
    if override and os.path.isfile(override):
        return override
    here = os.path.dirname(os.path.abspath(sys.executable))
    for candidate in (os.path.join(os.path.dirname(here), "core", CORE_EXE),
                      os.path.join(here, CORE_EXE)):
        if os.path.isfile(candidate):
            return candidate
    return None


def autodetect():
    """Guess (script, interpreter) for the core, or (None, None).

    An installed build answers with its bundled core executable and no
    interpreter. Otherwise this looks next to the GUI first - ``gui/`` lives
    inside the core checkout, which is the normal layout - then at the current
    directory.
    """
    executable = frozen_core()
    if executable:
        return executable, ""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # gui/
    for root in (os.path.dirname(here), os.getcwd()):
        script = os.path.join(root, "session_launcher.py")
        if os.path.isfile(script):
            return script, _venv_python(root) or sys.executable
    return None, None


class Core:
    """A configured core: an interpreter, a script, and the config it reads.

    Instances are cheap and immutable-ish; the GUI rebuilds one whenever the
    settings change rather than mutating a shared object.
    """

    def __init__(self, script=None, interpreter=None, config=None,
                 log_sources=None, flows_dir=None):
        auto_script, auto_python = autodetect()
        self.script = script or auto_script
        if not needs_interpreter(self.script):
            # A native executable runs itself, and no interpreter can change that -
            # so a stale ``core/interpreter`` setting left over from a source
            # checkout must not turn into "python qavector-core".
            self.interpreter = ""
        else:
            self.interpreter = interpreter or auto_python or sys.executable
        self.config = config or ""
        # Passed on every call rather than left to the core's own default. The
        # GUI edits this file, so the one it edits and the one a run reads have
        # to be the same one - see Settings -> Log sources.
        self.log_sources = log_sources or ""
        # And for the same reason: the Scenarios page writes into this tree and a
        # run reads from it, so both have to be told the one answer rather than
        # each falling back to the core's default - which in a checkout is the
        # checkout. See Settings -> Scenarios.
        #
        # Taken literally by the core: once this is set it is the ONLY tree, so
        # the blocks and selectors.yaml a scenario references have to live in it
        # too. That is the deliberate reading of the setting - "my scenarios come
        # from here" - rather than a layer over what ships with the application.
        self.flows_dir = flows_dir or ""

    @property
    def legacy_log_sources(self):
        """Where the launcher looks for logsources.json when nobody says.

        Its own data root, mirrored by :attr:`root` rather than imported. Read
        when the new default under the user's directory is not there yet, so an
        upgrade does not look like having lost the file - and computed without
        --describe, so it is the one path the --log-sources flag cannot distort.
        """
        return os.path.join(self.root, "logsources.json") if self.root else ""

    # -- shape of a command ---------------------------------------------------
    @property
    def prefix(self):
        """The argv slots that name the program: 1 for an exe, 2 for a script."""
        if not self.script:
            return []
        return [self.interpreter, self.script] if self.interpreter else [self.script]

    @property
    def root(self):
        """The directory to run the core in, and to resolve its config against.

        For a checkout that is the checkout. For an installed build the executable
        sits somewhere read-only, so it is the user's own data directory instead -
        the same one ``runtime_paths.user_data_root`` picks, mirrored rather than
        imported so the GUI keeps needing nothing but PySide6.
        """
        if not self.script:
            return ""
        if needs_interpreter(self.script):
            return os.path.dirname(self.script)
        return user_data_root(self.script)

    def spawn_dir(self):
        """``root``, created if it does not exist yet, for handing to a child.

        Reading ``root`` stays free of side effects - it is rendered in the
        Command page and the settings dialog - so the directory is created here,
        at the two places that actually start a core process.
        """
        if self.script and not needs_interpreter(self.script):
            ensure_user_data_root(self.script)
        return self.root or None

    @property
    def config_path(self):
        """The config the core will actually read (absolute)."""
        if self.config:
            return self.config if os.path.isabs(self.config) else os.path.join(
                self.root, self.config)
        return os.path.join(self.root, "users.json") if self.root else ""

    def is_configured(self):
        return bool(self.script and os.path.isfile(self.script)
                    and (not self.interpreter or os.path.isfile(self.interpreter)))

    def argv(self, *args):
        """Full command line: the program, the global flags, then ``args``."""
        if not self.script:
            raise CoreError("No core script configured (Settings -> Core script).")
        command = list(self.prefix)
        if self.config:
            command.append("--config=" + self.config)
        if self.log_sources:
            command.append("--log-sources=" + self.log_sources)
        if self.flows_dir:
            command.append("--flows-dir=" + self.flows_dir)
        command.extend(a for a in args if a)
        return command

    def display_argv(self, *args):
        """The same command, shortened for reading: basenames, no interpreter path."""
        command = self.argv(*args)
        cut = len(self.prefix)
        return " ".join([os.path.basename(part) for part in command[:cut]]
                        + command[cut:])

    # -- one-shot calls -------------------------------------------------------
    def run(self, *args, timeout=60):
        """Run the launcher to completion; returns (returncode, stdout, stderr)."""
        try:
            proc = subprocess.run(self.argv(*args), capture_output=True, text=True,
                                  timeout=timeout, cwd=self.spawn_dir(),
                                  # Windows: never flash a console window.
                                  creationflags=getattr(subprocess,
                                                        "CREATE_NO_WINDOW", 0))
        except FileNotFoundError as exc:
            raise CoreError("Cannot run %s: %s" % (self.interpreter, exc))
        except subprocess.TimeoutExpired:
            raise CoreError("%s did not answer within %ds." % (
                os.path.basename(self.script or "session_launcher.py"), timeout))
        except OSError as exc:
            raise CoreError(str(exc))
        return proc.returncode, proc.stdout, proc.stderr

    def describe(self):
        """The core's inventory as a dict (see ``session_launcher.describe``)."""
        code, out, err = self.run("--describe", timeout=90)
        text = (out or "").strip()
        if not text:
            raise CoreError("--describe printed nothing (exit %d).\n%s"
                            % (code, (err or "").strip()[:400]))
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise CoreError("--describe did not return JSON (%s).\n%s"
                            % (exc, text[:400]))
        if isinstance(payload, dict) and payload.get("error") and not payload.get("users"):
            raise CoreError(payload["error"])
        return payload

    # -- scenario files -------------------------------------------------------
    # The GUI has no YAML of its own - PySide6 is its only dependency - so the
    # scenario format stays entirely in the core and these four go through it.
    # Each answers with JSON whether or not it worked, so a failure is a payload
    # to render rather than an exception to catch in a widget.

    def flow_show(self, flow_id):
        """One scenario: its text, its steps, and whether it can be edited."""
        return self._flow_json("--flow-show=" + flow_id)

    def flow_save(self, flow_id, document):
        """Write a scenario from ``document`` ({"yaml": ...} or meta+steps).

        The document goes through a temp file rather than stdin so the same call
        can be run by hand from a shell when something looks wrong.
        """
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json",
                                             prefix="cms-flow-", delete=False)
        try:
            with handle:
                json.dump(document, handle, ensure_ascii=False, default=str)
            return self._flow_json("--flow-save=" + flow_id, "--from=" + handle.name)
        finally:
            try:
                os.unlink(handle.name)
            except OSError:
                pass

    def flow_delete(self, flow_id):
        return self._flow_json("--flow-delete=" + flow_id)

    def flow_import(self, path):
        return self._flow_json("--flow-import=" + path)

    def server_log_show(self, name, lines=None):
        """Read one configured backend log through the launcher.

        The GUI cannot read it itself - it never imports the engine, so it has no
        ssh, no docker and no tail of its own. ``lines`` is how many from the end;
        None asks for as much as the core's byte budget allows.

        A failure comes back as ``{"ok": False, "error": ...}`` rather than an
        exception: "this connection is wrong" is the useful answer, not a fault.
        """
        args = ["--server-log-show=" + name]
        args.append("--server-log-lines=%s" % ("all" if lines is None else lines))
        try:
            return self._flow_json(*args)
        except CoreError as exc:
            return {"log": name, "ok": False, "error": str(exc), "lines": []}

    def selectors_show(self):
        """The named-target map, and the user's own file as editable text."""
        return self._flow_json("--selectors-show")

    def selectors_save(self, yaml_text):
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json",
                                             prefix="cms-selectors-", delete=False)
        try:
            with handle:
                json.dump({"yaml": yaml_text}, handle, ensure_ascii=False)
            return self._flow_json("--selectors-save", "--from=" + handle.name)
        finally:
            try:
                os.unlink(handle.name)
            except OSError:
                pass

    def _flow_json(self, *args):
        """Run a --flow-* command and parse its answer.

        A non-zero exit is not an error here: "this did not compile" is the most
        useful thing these commands say, and it comes back with the exit code set.
        Only unparseable output is a real failure.
        """
        code, out, err = self.run(*args, timeout=60)
        text = (out or "").strip()
        if not text:
            raise CoreError("%s printed nothing (exit %d).\n%s"
                            % (args[0], code, (err or "").strip()[:400]))
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise CoreError("%s did not return JSON (%s).\n%s"
                            % (args[0], exc, text[:400]))
        payload.setdefault("problems", [])
        return payload

    def version(self):
        _code, out, _err = self.run("--version", timeout=30)
        return (out or "").strip()

    def help_text(self):
        _code, out, _err = self.run("--help", timeout=30)
        return out or ""


class Inventory:
    """Typed, forgiving access to a ``--describe`` payload.

    The GUI must keep working against a core that is older or newer than it, so
    every accessor tolerates a missing key rather than raising deep inside a
    widget.
    """

    def __init__(self, payload=None):
        self.payload = payload or {}

    def __bool__(self):
        return bool(self.payload)

    @property
    def version(self):
        return self.payload.get("version", "")

    @property
    def config_path(self):
        return self.payload.get("config_path", "")

    @property
    def envs(self):
        return list(self.payload.get("envs", []))

    @property
    def users(self):
        return list(self.payload.get("users", []))

    @property
    def scenarios(self):
        return list(self.payload.get("scenarios", []))

    @property
    def extensions(self):
        return list(self.payload.get("extensions", []))

    @property
    def log_sources(self):
        """Backend logs (--server-log), one row per (log, environment) pair."""
        return list(self.payload.get("log_sources", []))

    @property
    def log_sources_path(self):
        return self.payload.get("log_sources_path", "")

    def logs_for(self, env_value=None):
        """The distinct log names available, optionally within one environment.

        A name repeats across environments on purpose ("app" exists on every
        stand), so an unfiltered call de-duplicates rather than listing it twice.
        """
        names, rows = [], []
        for row in self.log_sources:
            if env_value and row.get("env") != env_value:
                continue
            name = row.get("name", "")
            if not name or name in names:
                continue
            names.append(name)
            rows.append(row)
        return rows

    @property
    def tags(self):
        return list(self.payload.get("tags", []))

    @property
    def warnings(self):
        return list(self.payload.get("warnings", []))

    @property
    def blocks(self):
        """The reusable flows a scenario reaches through ``use:``."""
        return list(self.payload.get("blocks", []))

    @property
    def selectors(self):
        """``{name: selector}`` - what a named target actually looks for."""
        value = self.payload.get("selectors")
        return dict(value) if isinstance(value, dict) else {}

    def flow_actions(self):
        """The step grammar the core accepts, grouped by argument shape.

        Empty against a core that predates it; the editor then falls back to its
        own list rather than offering nothing.
        """
        value = self.payload.get("flow_actions")
        return dict(value) if isinstance(value, dict) else {}

    def scenario(self, flow_id):
        """One scenario's row from --describe, or {}."""
        for row in self.scenarios:
            if row.get("id") == flow_id:
                return row
        return {}

    @property
    def chrome(self):
        """{path, version, message} - the browser the core found, if any.

        Empty for a core older than this key, which reads as "cannot tell" rather
        than "missing": never warn about a Chrome we simply did not ask about.
        """
        value = self.payload.get("chrome")
        return dict(value) if isinstance(value, dict) else {}

    def chrome_problem(self):
        """The "install Chrome" message, or "" when there is nothing to say.

        Keyed on ``message`` rather than on ``path``, because a browser that is
        present but cannot run is a problem too - and the core is the one that
        knows the difference.
        """
        chrome = self.chrome
        if not chrome:
            return ""   # a core too old to have been asked; not "missing"
        return chrome.get("message") or ""

    def env_aliases(self):
        return [e.get("alias", "") for e in self.envs]

    def env_value(self, alias):
        """The users.json ``env`` string behind an ``--env`` alias.

        ``--env`` matches on the alias, but the user rows carry the raw value, so
        anything selecting users by environment has to cross this bridge.
        """
        for env in self.envs:
            if env.get("alias") == alias:
                return env.get("value", "")
        return ""

    def logins(self, env_value=None):
        """Logins, optionally only those belonging to one environment value."""
        return [u.get("login", "") for u in self.users
                if env_value in (None, u.get("env"))]

    def choices(self, key, fallback):
        """A vocabulary the core advertises (log levels, overlay components...)."""
        value = self.payload.get(key)
        return list(value) if isinstance(value, list) and value else list(fallback)

    def dirs(self):
        return {
            "flows": self.payload.get("flows_dir", ""),
            "reports": self.payload.get("reports_dir", ""),
            "sessions": self.payload.get("sessions_dir", ""),
        }

    def summary(self):
        """One line for the toolbar: what this inventory contains.

        Plain words, not the flag that fetched them: the toolbar is read by
        someone deciding what to launch, and "--describe" answers a question
        they did not ask. The Command page and the Tools menu still name the
        flag, because that is where the flags are the subject.
        """
        if not self.payload:
            return "nothing read yet"
        return "%d environments · %d accounts · %d scenarios" % (
            len(self.envs), len(self.users), len(self.scenarios))
