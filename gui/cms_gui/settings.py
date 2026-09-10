"""Persistent GUI settings (QSettings) - paths, per-environment overrides, presets.

Only things that belong to the *front-end* live here. Anything the launcher
itself owns (users, environments, scenarios) stays in ``users.json`` and the
flows tree, so the CLI and the GUI never disagree about them.
"""

import json

from PySide6.QtCore import QSettings

ORG = "qavector"
APP = "gui"
#: Where the settings were kept before 0.15.0, when QAVector was
#: chrome-multi-session. Copied into ORG once, at startup - see migrate.
LEGACY_ORG = "chrome-multi-session"


class Settings:
    """Thin, typed wrapper so pages never spell a settings key by hand."""

    def __init__(self):
        self._qs = QSettings(ORG, APP)

    # -- core location --------------------------------------------------------
    @property
    def core_script(self):
        return self._qs.value("core/script", "", str)

    @core_script.setter
    def core_script(self, value):
        self._qs.setValue("core/script", value or "")

    @property
    def interpreter(self):
        return self._qs.value("core/interpreter", "", str)

    @interpreter.setter
    def interpreter(self, value):
        self._qs.setValue("core/interpreter", value or "")

    @property
    def config(self):
        """--config value; empty means the core's own default (users.json)."""
        return self._qs.value("core/config", "", str)

    @config.setter
    def config(self, value):
        self._qs.setValue("core/config", value or "")

    @property
    def services_path(self):
        """Where services.json lives; empty means the default beside the rest.

        The GUI's own file, so unlike the three above it is nobody else's
        business where it goes - and it has a default worth overriding: a source
        checkout would otherwise put it in the checkout.
        """
        return self._qs.value("services/path", "", str)

    @services_path.setter
    def services_path(self, value):
        self._qs.setValue("services/path", value or "")

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
        return self._qs.value("flows/path", "", str)

    @flows_path.setter
    def flows_path(self, value):
        self._qs.setValue("flows/path", value or "")

    @property
    def log_sources_path(self):
        """Where logsources.json lives; empty means the default under ~.

        Unlike services.json this one is *also* the launcher's, so wherever it
        goes the path travels with every call the GUI makes into the core
        (``--log-sources``). Otherwise the file being edited here and the file a
        run reads come apart, and the page's own Open buttons read the wrong one.
        """
        return self._qs.value("logsources/path", "", str)

    @log_sources_path.setter
    def log_sources_path(self, value):
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
        return self._json("folds/" + page, {})

    def save_folds(self, page, states):
        self._qs.setValue("folds/" + page, json.dumps(states, sort_keys=True))

    @property
    def always_on_top(self):
        """Whether the window stays above others - including a run's Chrome windows."""
        return self._qs.value("window/always_on_top", False, bool)

    @always_on_top.setter
    def always_on_top(self, value):
        self._qs.setValue("window/always_on_top", bool(value))

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
    def env_override(self, alias):
        return self._qs.value("env/override/" + alias, "", str)

    def set_env_override(self, alias, url):
        self._qs.setValue("env/override/" + alias, url or "")

    # -- what the observing pages were last looking at ------------------------
    @property
    def artifacts_dir(self):
        """The report folder the Artifacts page should reopen on."""
        return self._qs.value("observe/artifacts_dir", "", str)

    @artifacts_dir.setter
    def artifacts_dir(self, value):
        self._qs.setValue("observe/artifacts_dir", value or "")

    @property
    def log_source(self):
        """An archived log's path, or "" for the running log."""
        return self._qs.value("observe/log_source", "", str)

    @log_source.setter
    def log_source(self, value):
        self._qs.setValue("observe/log_source", value or "")

    def directory(self, name, default=""):
        """Default --flows-dir / --reports-dir / --sessions-dir for runs."""
        return self._qs.value("dirs/" + name, default, str)

    def set_directory(self, name, value):
        self._qs.setValue("dirs/" + name, value or "")

    # -- command form ---------------------------------------------------------
    def command_state(self):
        """The last command the user built, as a dict (empty on first run)."""
        raw = self._qs.value("command/state", "", str)
        try:
            value = json.loads(raw) if raw else {}
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def save_command_state(self, state):
        self._qs.setValue("command/state", json.dumps(state))

    # -- launch sessions form -------------------------------------------------
    def launch_state(self):
        """The last configuration the user built on Launch Sessions.

        Named configurations are a growing document and live in the GUI's data
        directory (see :mod:`cms_gui.store`); this is only the working copy, the
        counterpart of :meth:`command_state`.
        """
        return self._json("launch/state", {})

    def save_launch_state(self, config):
        self._qs.setValue("launch/state", json.dumps(config))

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
