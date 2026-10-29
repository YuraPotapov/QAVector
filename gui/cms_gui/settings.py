"""Persistent GUI settings (QSettings) - paths, per-environment overrides, presets.

Only things that belong to the *front-end* live here. Anything the launcher
itself owns (users, environments, scenarios) stays in ``users.json`` and the
flows tree, so the CLI and the GUI never disagree about them.
"""

import json
import os

from PySide6.QtCore import QSettings

from . import projectsfile

ORG = "qavector"
APP = "gui"
#: Where the settings were kept before the rename, when QAVector was
#: chrome-multi-session. Copied into ORG once, at startup - see migrate.
LEGACY_ORG = "chrome-multi-session"


class Settings:
    """Thin, typed wrapper so pages never spell a settings key by hand.

    It answers for the project in use (:mod:`cms_gui.projectsfile`). For
    Workspace every path is the setting, as it always was; for any other project
    every path is in the project's folder and cannot be set here, and the state
    a page leaves behind - where Launch Sessions was, which sections are open -
    is kept per project, so one project never opens on another's.
    """

    def __init__(self):
        self._qs = QSettings(ORG, APP)
        #: The project in use, and its folder ("" for Workspace).
        self.project_id = projectsfile.WORKSPACE
        self.project_folder = ""
        # Which core runs is not a setting any more: an installed GUI runs the
        # core installed beside it, and one started from a checkout runs that
        # checkout's (core.autodetect). A path saved by an earlier version won
        # over both - an installed build once pointed at a checkout kept
        # running the checkout's code with nothing on screen saying so - so it
        # is forgotten rather than left to be read.
        for key in ("core/script", "core/interpreter"):
            if self._qs.contains(key):
                self._qs.remove(key)

    # -- the project in use ---------------------------------------------------
    def use_project(self, project):
        """Answer for ``project`` (a projectsfile.Project) from now on."""
        self.project_id = project.id if project is not None else projectsfile.WORKSPACE
        self.project_folder = project.folder if project is not None else ""

    @property
    def current_project(self):
        """The id of the project picked last; Workspace before any was."""
        return self._qs.value("project/current", projectsfile.WORKSPACE, str) \
            or projectsfile.WORKSPACE

    @current_project.setter
    def current_project(self, value):
        self._qs.setValue("project/current", value or projectsfile.WORKSPACE)

    def _project_path(self, field):
        return getattr(projectsfile.paths(self.project_folder), field)

    def _scoped(self, key):
        """``key`` for the project in use: Workspace keeps the keys it had."""
        if not self.project_folder:
            return key
        return "projects/%s/%s" % (self.project_id, key)

    def data_dir(self):
        """Where the GUI keeps its own files of this project: configurations,
        drafts, run history."""
        if self.project_folder:
            os.makedirs(self.project_folder, exist_ok=True)
            return self.project_folder
        from . import store
        return store.app_data_dir()

    @property
    def cycle_inactivity_seconds(self):
        return max(10, self._qs.value("cycles/inactivity_seconds", 60, int))

    @cycle_inactivity_seconds.setter
    def cycle_inactivity_seconds(self, value):
        self._qs.setValue("cycles/inactivity_seconds", max(10, int(value)))

    @property
    def config(self):
        """--config value; empty means the core's own default (users.json)."""
        if self.project_folder:
            return self._project_path("users")
        return self._qs.value("core/config", "", str)

    @config.setter
    def config(self, value):
        if self.project_folder:
            return          # a project's files are its folder's
        self._qs.setValue("core/config", value or "")

    @property
    def services_path(self):
        """Where services.json lives; empty means the default beside the rest.

        The GUI's own file, so unlike the three above it is nobody else's
        business where it goes - and it has a default worth overriding: a source
        checkout would otherwise put it in the checkout.
        """
        if self.project_folder:
            return self._project_path("services")
        return self._qs.value("services/path", "", str)

    @services_path.setter
    def services_path(self, value):
        if self.project_folder:
            return          # a project's files are its folder's
        self._qs.setValue("services/path", value or "")

    @property
    def cycle_projects_path(self):
        """Where cycleprojects.json lives; empty means the default beside it.

        The Cycles section's own list of projects, and its own file rather than
        a corner of services.json: a project can have cycles and no services at
        all. Like services.json it is the GUI's alone, so where it goes is
        nobody else's business - and it has a default worth overriding for the
        same reason, since a source checkout would otherwise put it among the
        code.
        """
        if self.project_folder:
            return self._project_path("cycle_projects")
        return self._qs.value("cycles/projects_path", "", str)

    @cycle_projects_path.setter
    def cycle_projects_path(self, value):
        if self.project_folder:
            return          # a project's files are its folder's
        self._qs.setValue("cycles/projects_path", value or "")

    @property
    def cycle_secrets_path(self):
        """Where the encrypted store of secret variables lives.

        Its own file, beside the user's data and never in the checkout, for the
        reason ``users.json`` is: a cycle file is committed and shipped inside
        the build, so a value written there travels to everyone who clones the
        project. The path is here so it can point at a private volume, and it
        travels to the core on every call (``--cycle-secrets-file``) rather than
        each side guessing - the same bargain ``flows_path`` makes.
        """
        if self.project_folder:
            return self._project_path("cycle_secrets")
        return self._qs.value("cycles/secrets_path", "", str)

    @cycle_secrets_path.setter
    def cycle_secrets_path(self, value):
        if self.project_folder:
            return          # a project's files are its folder's
        self._qs.setValue("cycles/secrets_path", value or "")

    @property
    def cycle_memory_path(self):
        """Where what the project remembers between runs is kept.

        Its own file rather than a corner of the secrets one, and plain JSON
        rather than encrypted: secrets are hidden because reading them is the
        harm, while "why did it skip that task" is a question somebody will
        ask and the answer being readable is worth more than a confidentiality
        this does not need.
        """
        if self.project_folder:
            return self._project_path("cycle_memory")
        return self._qs.value("cycles/memory_path", "", str)

    @cycle_memory_path.setter
    def cycle_memory_path(self, value):
        if self.project_folder:
            return          # a project's files are its folder's
        self._qs.setValue("cycles/memory_path", value or "")

    @property
    def cycle_runs_path(self):
        """Where cycle runs are written, and read back by resume and sessions.

        Empty leaves it to the core: cycle-runs under its data root, which for a
        core run from a source checkout is the checkout itself.
        """
        if self.project_folder:
            return self._project_path("cycle_runs")
        return self._qs.value("cycles/runs_path", "", str)

    @cycle_runs_path.setter
    def cycle_runs_path(self, value):
        if self.project_folder:
            return          # a project's files are its folder's
        self._qs.setValue("cycles/runs_path", value or "")

    @property
    def flows_path(self):
        """Where the scenarios tree lives; empty means the core's own default.

        The same bargain as ``log_sources_path``: the GUI writes scenarios there
        and a run reads them from there, so the path travels with every call
        (``--flows-dir``) rather than each side guessing. And it has a default
        worth overriding for the same reason ``services_path`` does - in a source
        checkout the core's default is the checkout itself, so everything written
        lands among the code.
        """
        if self.project_folder:
            return self._project_path("flows")
        return self._qs.value("flows/path", "", str)

    @flows_path.setter
    def flows_path(self, value):
        if self.project_folder:
            return          # a project's files are its folder's
        self._qs.setValue("flows/path", value or "")

    @property
    def cycles_path(self):
        """Where the cycles tree lives; empty means the core's own default.

        The same bargain as :attr:`flows_path`, and wanted for a sharper
        reason. In a source checkout the core's default *is* the checkout, so
        the cycles a person edits are the template files that ship with the
        application - and saving one writes their own Jira instance and work
        email into a file that is committed. Pointing this somewhere of their
        own is what keeps the two apart.
        """
        if self.project_folder:
            return self._project_path("cycles")
        return self._qs.value("cycles/path", "", str)

    @cycles_path.setter
    def cycles_path(self, value):
        if self.project_folder:
            return          # a project's files are its folder's
        self._qs.setValue("cycles/path", value or "")

    @property
    def log_sources_path(self):
        """Where logsources.json lives; empty means the default under ~.

        Unlike services.json this one is *also* the launcher's, so wherever it
        goes the path travels with every call the GUI makes into the core
        (``--log-sources``). Otherwise the file being edited here and the file a
        run reads come apart, and the page's own Open buttons read the wrong one.
        """
        if self.project_folder:
            return self._project_path("log_sources")
        return self._qs.value("logsources/path", "", str)

    @log_sources_path.setter
    def log_sources_path(self, value):
        if self.project_folder:
            return          # a project's files are its folder's
        self._qs.setValue("logsources/path", value or "")

    # -- window ---------------------------------------------------------------
    def geometry(self):
        return self._qs.value("window/geometry", None)

    def save_geometry(self, data):
        self._qs.setValue("window/geometry", data)

    @property
    def page(self):
        return self._qs.value("window/page", "launch", str)

    @page.setter
    def page(self, value):
        self._qs.setValue("window/page", value or "launch")

    @property
    def dark_mode(self):
        return self._qs.value("window/dark_mode", False, type=bool)

    @dark_mode.setter
    def dark_mode(self, value):
        self._qs.setValue("window/dark_mode", bool(value))

    @property
    def developer_mode(self):
        """Whether the low-level surfaces (Command page, raw command) show."""
        return self._qs.value("window/developer_mode", False, bool)

    @developer_mode.setter
    def developer_mode(self, value):
        self._qs.setValue("window/developer_mode", bool(value))

    @property
    def sidebar_collapsed(self):
        """Whether the rail is showing marks only, without their labels."""
        return self._qs.value("window/sidebar_collapsed", False, bool)

    @sidebar_collapsed.setter
    def sidebar_collapsed(self, value):
        self._qs.setValue("window/sidebar_collapsed", bool(value))

    def hidden_nav_items(self):
        """Nav keys the user has taken off the rail, as a list.

        Stored as what is *hidden* rather than what is shown, so a page added in
        a later version arrives on the rail instead of having to be found and
        switched on.
        """
        return self._json("window/hidden_nav", [])

    def save_hidden_nav_items(self, keys):
        self._qs.setValue("window/hidden_nav", json.dumps(sorted(set(keys))))

    def folds(self, page):
        """Which sections on ``page`` are open, as {section key: bool}.

        A fold is a view preference, not a document: collapsing a project on
        Services & Logs must survive closing the window whether or not anything
        was saved, and must not light up a Save button. Stored as what the person
        actually chose rather than as what is shut, so a section added in a later
        version arrives at its own default instead of at somebody's old answer.
        """
        return self._json(self._scoped("folds/" + page), {})

    def save_folds(self, page, states):
        self._qs.setValue(self._scoped("folds/" + page), json.dumps(states, sort_keys=True))

    def splitter(self, key):
        """How one splitter was last left: {"sizes": [...], "open": [...]}.

        ``open`` is what each panel measured the last time it was open, so a
        panel folded shut before the window closed still comes back at its own
        size rather than a guess. A view preference like :meth:`folds`, and
        for the same reason kept here rather than in any document.
        """
        return self._json("splitters/" + key, {})

    def save_splitter(self, key, state):
        self._qs.setValue("splitters/" + key, json.dumps(state, sort_keys=True))

    @property
    def always_on_top(self):
        """Whether the window stays above others - including a run's Chrome windows."""
        return self._qs.value("window/always_on_top", False, bool)

    @always_on_top.setter
    def always_on_top(self, value):
        self._qs.setValue("window/always_on_top", bool(value))

    @property
    def stages_detached(self):
        """Whether the Cycles page's Stages lives in a window of its own.

        Kept across restarts: somebody who put the feed on a second screen
        wants it there tomorrow. Only a person closing that window clears it;
        the application closing it on the way out does not.
        """
        return self._qs.value("cycles/stages_detached", False, bool)

    @stages_detached.setter
    def stages_detached(self, value):
        self._qs.setValue("cycles/stages_detached", bool(value))

    def stages_geometry(self):
        """Where the Stages window was, as ``QWidget.saveGeometry`` wrote it."""
        return self._qs.value("cycles/stages_geometry", None)

    def save_stages_geometry(self, data):
        self._qs.setValue("cycles/stages_geometry", data)

    @property
    def launch_summary_expanded(self):
        """Whether the Launch page's footer shows its summary, notes and preview."""
        return self._qs.value("launch/summary_expanded", True, bool)

    @launch_summary_expanded.setter
    def launch_summary_expanded(self, value):
        self._qs.setValue("launch/summary_expanded", bool(value))

    @property
    def run_source(self):
        """Which page the toolbar's RUN acts on: "launch" or "commands"."""
        return self._qs.value("window/run_source", "launch", str)

    @run_source.setter
    def run_source(self, value):
        self._qs.setValue("window/run_source", value or "launch")

    # -- per-environment extras (the core has no place for these) -------------
    def env_urls(self):
        """``{env value: address}`` set on the Environments page.

        Keyed by the users.json ``env`` value rather than the alias: the alias
        is worked out from the address and renames itself when another
        environment with the same host appears, which would strand an address
        filed under it. One JSON value rather than a key per environment,
        because an env value is often a URL and QSettings reads every "/" in a
        key as a group.
        """
        if self.project_folder:
            return {}           # Workspace's, from before projects
        return {key: value for key, value in self._json("env/urls", {}).items()
                if isinstance(key, str) and isinstance(value, str) and value}

    def set_env_url(self, value, url):
        urls = self.env_urls()
        url = (url or "").strip()
        if url:
            urls[value] = url
        else:
            urls.pop(value, None)
        self._qs.setValue("env/urls", json.dumps(urls, sort_keys=True))

    def legacy_env_overrides(self):
        """``{alias: url}`` saved by the old URL override, which nothing applied.

        Kept rather than adopted: switching them on now would change where a
        run opens without anyone having asked for it today. The Environments
        page offers each one to apply or discard.
        """
        if self.project_folder:
            return {}           # Workspace's, from before projects
        self._qs.beginGroup("env/override")
        try:
            found = {alias: self._qs.value(alias, "", str)
                     for alias in self._qs.childKeys()}
        finally:
            self._qs.endGroup()
        return {alias: url for alias, url in found.items() if url}

    def discard_legacy_env_override(self, alias):
        self._qs.remove("env/override/" + alias)

    # -- what the observing pages were last looking at ------------------------
    @property
    def artifacts_dir(self):
        """The report folder the Artifacts page should reopen on."""
        return self._qs.value(self._scoped("observe/artifacts_dir"), "", str)

    @artifacts_dir.setter
    def artifacts_dir(self, value):
        self._qs.setValue(self._scoped("observe/artifacts_dir"), value or "")

    @property
    def log_source(self):
        """An archived log's path, or "" for the running log."""
        return self._qs.value(self._scoped("observe/log_source"), "", str)

    @log_source.setter
    def log_source(self, value):
        self._qs.setValue(self._scoped("observe/log_source"), value or "")

    def directory(self, name, default=""):
        """Default --flows-dir / --reports-dir / --sessions-dir for runs."""
        if self.project_folder:
            field = {"reports": "reports", "sessions": "sessions",
                     "flows": "flows"}.get(name)
            return self._project_path(field) if field else default
        return self._qs.value("dirs/" + name, default, str)

    def set_directory(self, name, value):
        if self.project_folder:
            return              # a project's folders are its own
        self._qs.setValue("dirs/" + name, value or "")

    # -- command form ---------------------------------------------------------
    def command_state(self):
        """The last command the user built, as a dict (empty on first run)."""
        raw = self._qs.value(self._scoped("command/state"), "", str)
        try:
            value = json.loads(raw) if raw else {}
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def save_command_state(self, state):
        self._qs.setValue(self._scoped("command/state"), json.dumps(state))

    # -- launch sessions form -------------------------------------------------
    def launch_state(self):
        """The last configuration the user built on Launch Sessions.

        Named configurations are a growing document and live in the GUI's data
        directory (see :mod:`cms_gui.store`); this is only the working copy, the
        counterpart of :meth:`command_state`.
        """
        return self._json(self._scoped("launch/state"), {})

    def save_launch_state(self, config):
        self._qs.setValue(self._scoped("launch/state"), json.dumps(config))

    def launch_context(self):
        """Where Launch Sessions was: ``{"environment": id, "preset": id}``.

        By id rather than by name: a configuration can be renamed, and two
        projects can each have one called "Smoke".
        """
        return self._json(self._scoped("launch/context"), {})

    def save_launch_context(self, context):
        self._qs.setValue(self._scoped("launch/context"), json.dumps(context, sort_keys=True))

    def launch_last_presets(self):
        """``{environment id: preset id}`` - what was open in each, last time."""
        return {key: value for key, value in self._json(self._scoped("launch/last"), {}).items()
                if isinstance(key, str) and isinstance(value, str)}

    def save_launch_last_presets(self, last):
        self._qs.setValue(self._scoped("launch/last"), json.dumps(last, sort_keys=True))

    def launch_last_environments(self):
        """``{project id: environment id}`` - where each project was left."""
        return {key: value for key, value in self._json(self._scoped("launch/last_env"), {}).items()
                if isinstance(key, str) and isinstance(value, str)}

    def save_launch_last_environments(self, last):
        self._qs.setValue(self._scoped("launch/last_env"), json.dumps(last, sort_keys=True))

    @property
    def launch_projects_path(self):
        """Where launch-projects.json lives; empty means beside services.json.

        The GUI's own file - the launcher has never heard of it - with a default
        worth overriding for the reason the other two have one: a source
        checkout would otherwise put it among the code.
        """
        if self.project_folder:
            return self._project_path("launch_projects")
        return self._qs.value("launch/projects_path", "", str)

    @launch_projects_path.setter
    def launch_projects_path(self, value):
        if self.project_folder:
            return          # a project's files are its folder's
        self._qs.setValue("launch/projects_path", value or "")

    @property
    def launch_config_name(self):
        """Which saved configuration is currently loaded ("" = unsaved)."""
        return self._qs.value("launch/config_name", "", str)

    @launch_config_name.setter
    def launch_config_name(self, value):
        self._qs.setValue("launch/config_name", value or "")

    def _json(self, key, default):
        raw = self._qs.value(key, "", str)
        try:
            value = json.loads(raw) if raw else default
        except ValueError:
            return default
        return value if isinstance(value, type(default)) else default

    def hotkey_overrides(self):
        """Portable key combinations; a present empty value means unassigned.

        Unknown IDs belong to temporarily unavailable plugins and are retained.
        """
        return {key: value for key, value in self._json("hotkeys/overrides", {}).items()
                if isinstance(key, str) and isinstance(value, str)}

    def save_hotkey_overrides(self, overrides):
        """Commit before changing live keys, restoring the previous cache on error."""
        key = "hotkeys/overrides"
        existed = self._qs.contains(key)
        previous = self._qs.value(key, "", str)
        self._qs.setValue(key, json.dumps(overrides, sort_keys=True))
        self._qs.sync()
        if self._qs.status() != QSettings.NoError:
            if existed:
                self._qs.setValue(key, previous)
            else:
                self._qs.remove(key)
            self._qs.sync()
            raise OSError("Could not save hotkeys. Check that the settings folder is writable.")

    def presets(self):
        """Saved command forms: {name: state dict}."""
        raw = self._qs.value("command/presets", "", str)
        try:
            value = json.loads(raw) if raw else {}
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def save_presets(self, presets):
        self._qs.setValue("command/presets", json.dumps(presets))

    def desktop_links(self):
        """Saved custom desktop links for configurations: {config_name: {path, name, icon}}."""
        raw = self._qs.value("launch/desktop_links", "", str)
        try:
            value = json.loads(raw) if raw else {}
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def save_desktop_links(self, links):
        self._qs.setValue("launch/desktop_links", json.dumps(links))
