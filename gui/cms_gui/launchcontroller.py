"""Where Launch Sessions is, and the working copy of everything it has open.

The page draws controls; this decides what they are showing: which environment,
which saved configuration of it (or a new one), and what the working copy of
that configuration holds. A working copy is kept for every configuration that
has been edited and not saved, and for the new one of every environment, so
moving between them never asks "discard your changes?" - the edit is simply
where it was left (``launch-drafts.json``).

Configurations are listed by environment, as the page navigates: an
environment, then one of its configurations. A configuration saved before its
environment could be told (see ``launchpresetsfile.bind_environments``) is
listed under a stand-in for the environment it named - "localhost (missing)" -
and one saved with "All environments" under that, so nothing saved is ever out
of reach; the rules then say what is wrong with it, and "Use with another
environment" puts it somewhere it can run.

Variables are resolved here too, because the places they come from are the
context: the environment's values, the values the saved configuration holds,
and this run's (the working copy's) changes to them - each value labelled with
the place that gave it.
"""

import json
import os

from PySide6.QtCore import QObject, Signal

from . import launch, launchprojectsfile as lpf, launchpresetsfile as lps, launchvars, store

#: The preset id of "(unsaved)": the new configuration of an environment.
NEW = ""

#: Stand-in environments for configurations no environment holds.
MISSING_PREFIX = "missing:"
ALL_ENVIRONMENTS = "all"

#: Where a variable's value came from, as the page labels it.
SOURCE_ENVIRONMENT = lpf.ENVIRONMENT
SOURCE_CONFIGURATION = "configuration"
SOURCE_RUN = "this run"

DRAFTS_FILE = "launch-drafts.json"
DRAFTS_VERSION = 2


class LaunchController(QObject):
    """The context of Launch Sessions and its stores; see the module docstring."""

    #: The environments or the configurations on offer changed.
    changed = Signal()
    #: launch-projects.json was changed from here (an address, a variable).
    registry_changed = Signal()

    def __init__(self, settings, registry, presets_path=None, drafts_path=None,
                 configs_path=None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.registry = registry
        self.persistent = True
        self.inventory = None
        self._open_stores(presets_path, drafts_path, configs_path)

    def _open_stores(self, presets_path=None, drafts_path=None, configs_path=None):
        """The configurations and drafts of the project in use, and where it was."""
        data = self.settings.data_dir()
        self.presets = lps.Presets(presets_path or os.path.join(data, lps.FILE_NAME))
        self._drafts = store.JsonStore(drafts_path or os.path.join(data, DRAFTS_FILE),
                                       default={})
        self._draft_written = {}
        if not self.settings.project_folder:
            # configs.json is Workspace's, from before configurations had ids;
            # another project starts with none of them.
            names = lps.migrate_configs(
                self.presets, configs_path or os.path.join(data, lps.CONFIGS_FILE),
                lpf.WORKSPACE)
            self._migrate_settings(names)
        context = self.settings.launch_context()
        self.project_id = str(context.get("project") or lpf.WORKSPACE)
        self.environment_id = str(context.get("environment") or "")
        self.preset_id = str(context.get("preset") or "")
        if self.preset_id and self.presets.get(self.preset_id) is None:
            self.preset_id = NEW
        #: The revision of the open configuration when it was opened: a save
        #: finding another one means somebody saved it since.
        self.opened_revision = None

    def repoint(self):
        """Another project was picked: its configurations, drafts and context."""
        self.inventory = None
        self._open_stores()

    # -- one-time moves -------------------------------------------------------
    def _migrate_settings(self, names):
        """Carry what QSettings said by name over to ids, once."""
        settings = self.settings
        if names and not settings.launch_context():
            name = settings.launch_config_name
            if name in names:
                settings.save_launch_context({"environment": "", "preset": names[name]})
        links = settings.desktop_links()
        if links and any(key not in {p.id for p in self.presets.all()} for key in links):
            moved = {}
            for key, link in links.items():
                preset, _candidates = self.presets.find(key)
                moved[preset.id if preset is not None else key] = link
            settings.save_desktop_links(moved)
        drafts = self._drafts.load()
        if drafts and "version" not in drafts:
            # The first working copies were filed by configuration name, "" for
            # (unsaved); file them under what they are now.
            moved = {}
            for name, state in drafts.items():
                if not isinstance(state, dict):
                    continue
                if name:
                    preset, _candidates = self.presets.find(name)
                    if preset is not None:
                        moved["preset:" + preset.id] = {"state": state,
                                                        "base": preset.revision}
                else:
                    value = state.get("environment_value") or ""
                    key = "new:" + (lpf.VIRTUAL_PREFIX + value if value else "")
                    moved[key] = {"state": state, "base": 0}
            self._drafts.save({"version": DRAFTS_VERSION, "drafts": moved})

    def adopt_env_urls(self):
        """Write the addresses the Environments page kept by env value into
        those environments, once the inventory says which ones they are."""
        urls = self.settings.env_urls()
        if not urls or self.inventory is None or self.registry.error:
            return
        envs = {env.account_key: env for env in self.registry.environments(self.inventory)}

        def change(projects):
            for value, url in urls.items():
                env = envs.get(value)
                if env is not None and not lpf.environment_row(projects, env).address:
                    lpf.environment_row(projects, env).address = url
        try:
            self.registry.update(change)
        except lpf.LaunchProjectsError:
            return
        for value in urls:
            self.settings.set_env_url(value, "")

    # -- inventory ------------------------------------------------------------
    def set_inventory(self, inventory):
        self.inventory = inventory
        self.registry.reload()
        self.adopt_env_urls()
        lps.bind_environments(self.presets, self.registry.environments(inventory),
                              inventory)
        preset = self.preset()
        if preset is not None:
            # The environment the open configuration belongs to - known only now.
            self.environment_id = self.preset_environment_id(preset)
        elif not self.environment_id or self.environment() is None:
            envs = self.environments(self.project_id) or self.environments()
            self.environment_id = envs[0].id if envs else ""
        env = self.environment()
        if env is not None:
            self.project_id = env.project_id
        self._remember()
        self.changed.emit()

    # -- what there is --------------------------------------------------------
    def projects(self):
        """``[(id, name)]`` of the projects to offer; the current one always."""
        names = lpf.project_names(self.registry.projects, self.environments())
        if self.project_id and self.project_id not in {pid for pid, _name in names}:
            project = self.registry.project(self.project_id)
            names.append((self.project_id, project.name if project else self.project_id))
        return names

    def environments(self, project_id=None):
        """Every environment to offer - of one project, when ``project_id`` says:
        the registry's and virtual ones, then the stand-ins for configurations
        that name none of them."""
        found = self._environments()
        if project_id is None:
            return found
        return [env for env in found if env.project_id == project_id]

    def _environments(self):
        envs = self.registry.environments(self.inventory)
        known = {env.id for env in envs}
        extra = []
        for preset in self.presets.all():
            env_id = self.preset_environment_id(preset, envs)
            if env_id in known:
                continue
            known.add(env_id)
            extra.append(self._stand_in(env_id, preset))
        # A stand-in only for an environment something here still names: the
        # open configuration's. One remembered from nothing - a context written
        # before a project was switched, say - is not offered as a place.
        if self.environment_id and self.environment_id not in known \
                and self._stand_in_kind(self.environment_id) and self.preset() is not None:
            extra.append(self._stand_in(self.environment_id, None))
        return envs + extra

    def _stand_in_kind(self, env_id):
        if env_id == ALL_ENVIRONMENTS:
            return "all"
        if env_id.startswith(MISSING_PREFIX):
            return "missing"
        return ""

    def _stand_in(self, env_id, preset):
        project_id = (preset.project_id if preset is not None else "") or \
            (self.project_id if env_id == self.environment_id else "") or lpf.WORKSPACE
        if env_id == ALL_ENVIRONMENTS:
            return lpf.Environment(ALL_ENVIRONMENTS, project_id, "All environments",
                                   "", "", {}, "", 0, True)
        legacy = (preset.legacy if preset is not None else None) or {}
        if env_id.startswith(MISSING_PREFIX):
            name = legacy.get("alias") or env_id[len(MISSING_PREFIX):]
        else:
            name = env_id
        return lpf.Environment(env_id, project_id, name, legacy.get("value") or "",
                               "", {}, "", 0, True)

    def is_stand_in(self, env):
        """True for "localhost (missing)" and "All environments": no real place."""
        return env is not None and (env.id == ALL_ENVIRONMENTS
                                    or env.id.startswith(MISSING_PREFIX)
                                    or lpf.find(self.registry.environments(self.inventory),
                                                env.id) is None)

    def preset_environment_id(self, preset, envs=None):
        """The id of the environment ``preset`` is listed under."""
        if preset.all_environments:
            return ALL_ENVIRONMENTS
        if preset.environment_id:
            env = lpf.find(envs if envs is not None
                           else self.registry.environments(self.inventory),
                           preset.environment_id)
            return env.id if env is not None else MISSING_PREFIX + preset.environment_id
        legacy = preset.legacy or {}
        if not legacy.get("alias") and not legacy.get("value"):
            # Saved with no environment at all (before that was written down as
            # "All environments"): that is what it runs as.
            return ALL_ENVIRONMENTS
        return MISSING_PREFIX + (legacy.get("alias") or legacy.get("value"))

    def presets_here(self, env_id=None):
        """The configurations of an environment (default: the current one), by name."""
        env_id = self.environment_id if env_id is None else env_id
        envs = self.registry.environments(self.inventory)
        found = [preset for preset in self.presets.all()
                 if self.preset_environment_id(preset, envs) == env_id]
        return sorted(found, key=lambda preset: preset.name.lower())

    def environment(self, env_id=None):
        env_id = self.environment_id if env_id is None else env_id
        if not env_id:
            return None
        for env in self.environments():
            if env.id == env_id:
                return env
        return None

    def preset(self):
        return self.presets.get(self.preset_id) if self.preset_id else None

    def project(self, env=None):
        env = env or self.environment()
        return self.registry.project(env.project_id if env else lpf.WORKSPACE)

    # -- the configuration as the page shows it ---------------------------------
    def environment_fields(self, env=None):
        """``(environment, environment_value)`` of a launch configuration here.

        The environment's name and its account key; "" and "" for "All
        environments"; a missing one's saved alias and value, which the rules
        then report as missing.
        """
        env = env if env is not None else self.environment()
        if env is None or env.id == ALL_ENVIRONMENTS:
            return "", ""
        return env.name, env.account_key

    def compose(self, preset, env=None):
        """The full launch configuration ``preset`` means in ``env``."""
        config = launch.merged(preset.config if preset is not None else {})
        config["environment"], config["environment_value"] = self.environment_fields(env)
        config["variables"] = dict(preset.variables) if preset is not None else {}
        return config

    # -- moving around --------------------------------------------------------
    def opening(self, current=None):
        """``(state, baseline)`` to show for the context as it is now.

        The working copy when there is one; otherwise the saved configuration.
        A new configuration with no working copy starts from ``current`` - the
        settings on screen - in this environment, the way changing the
        environment always kept everything else. ``baseline`` is the saved
        configuration edits are measured against; None for a new one.
        """
        env = self.environment()
        preset = self.preset()
        baseline = self.compose(preset, env) if preset is not None else None
        draft = self._draft_entry(self.draft_key())
        if draft is not None:
            state = launch.merged(draft["state"])
            self.opened_revision = draft.get("base") if preset is not None else None
        elif baseline is not None:
            state = json.loads(json.dumps(baseline))
            self.opened_revision = preset.revision
        else:
            state = launch.merged(current or {})
            self.opened_revision = None
        state["environment"], state["environment_value"] = self.environment_fields(env)
        state.setdefault("variables", {})
        return state, baseline

    def select_project(self, project_id, current=None):
        """Go to a project: to the environment it was left at, else its first."""
        self.project_id = project_id
        envs = self.environments(project_id)
        last = self.settings.launch_last_environments().get(project_id, "")
        env_id = last if any(env.id == last for env in envs) else \
            (envs[0].id if envs else "")
        return self.select_environment(env_id, current)

    def select_environment(self, env_id, current=None):
        """Go to ``env_id``, to what was last open there."""
        self.environment_id = env_id
        env = self.environment()
        if env is not None:
            self.project_id = env.project_id
        last = self.settings.launch_last_presets().get(env_id, NEW)
        self.preset_id = last if last and any(
            preset.id == last for preset in self.presets_here(env_id)) else NEW
        self._remember()
        return self.opening(current)

    def select_preset(self, preset_id, current=None):
        self.preset_id = preset_id or NEW
        preset = self.preset()
        if preset is not None:
            self.environment_id = self.preset_environment_id(preset)
            env = self.environment()
            if env is not None:
                self.project_id = env.project_id
        self._remember()
        return self.opening(current)

    def open_saved(self, token):
        """Open what a desktop link names, as saved: ``(state, baseline)`` or None.

        Returns ``(None, candidates)`` when the name is shared, so the caller
        can say which configurations it could have meant.
        """
        preset, candidates = self.presets.find(token)
        if preset is None:
            return None, candidates
        self.preset_id = preset.id
        self.environment_id = self.preset_environment_id(preset)
        env = self.environment()
        if env is not None:
            self.project_id = env.project_id
        self._remember()
        baseline = self.compose(preset)
        self.opened_revision = preset.revision
        return (json.loads(json.dumps(baseline)), baseline), []

    def open_unsaved(self, config):
        """Put a run brought back from History on screen as new, in its environment.

        Found by the accounts it launched (the env value), then by name; one no
        environment holds any more is opened under its stand-in, flagged.
        """
        config = launch.merged(config)
        value = config.get("environment_value") or ""
        name = config.get("environment") or ""
        envs = self.registry.environments(self.inventory)
        holders = [env for env in envs if value and env.account_key == value] or \
            [env for env in envs if name and env.name == name]
        if not name:
            env_id = ALL_ENVIRONMENTS
        elif len(holders) == 1:
            env_id = holders[0].id
        else:
            env_id = MISSING_PREFIX + name
        self.environment_id, self.preset_id = env_id, NEW
        env = self.environment()
        if env is not None:
            self.project_id = env.project_id
        self._remember()
        state = config
        state["environment"], state["environment_value"] = self.environment_fields(
            self.environment() or self._stand_in(env_id, None))
        self.opened_revision = None
        return state

    def _remember(self):
        if not self.persistent:
            return
        self.settings.save_launch_context({"project": self.project_id,
                                           "environment": self.environment_id,
                                           "preset": self.preset_id})
        if self.environment_id:
            last = self.settings.launch_last_presets()
            last[self.environment_id] = self.preset_id
            self.settings.save_launch_last_presets(last)
            places = self.settings.launch_last_environments()
            places[self.project_id] = self.environment_id
            self.settings.save_launch_last_environments(places)

    # -- working copies -------------------------------------------------------
    def draft_key(self, env_id=None, preset_id=None):
        preset_id = self.preset_id if preset_id is None else preset_id
        if preset_id:
            return "preset:" + preset_id
        return "new:" + (self.environment_id if env_id is None else env_id)

    def _drafts_document(self):
        document = self._drafts.load()
        if document.get("version") != DRAFTS_VERSION or \
                not isinstance(document.get("drafts"), dict):
            return {"version": DRAFTS_VERSION, "drafts": {}}
        return document

    def _draft_entry(self, key):
        entry = self._drafts_document()["drafts"].get(key)
        if isinstance(entry, dict) and isinstance(entry.get("state"), dict):
            return entry
        return None

    def draft(self, key=None):
        entry = self._draft_entry(key or self.draft_key())
        return entry["state"] if entry is not None else None

    def keep_draft(self, state, dirty, signature):
        """Remember ``state`` as the working copy here, or forget it.

        A saved configuration keeps one while it differs from what is saved; a
        new one always keeps what it holds. ``signature`` is the state reduced
        to what "the same" means, so nothing is written while it has not
        changed - this runs on every keystroke.
        """
        if not self.persistent:
            return
        key = self.draft_key()
        keep = dirty or not self.preset_id
        mark = signature if keep else None
        if self._draft_written.get(key, False) == mark:
            return
        document = self._drafts_document()
        drafts = document["drafts"]
        if keep:
            drafts[key] = {"state": state, "base": self.opened_revision or 0}
        elif key in drafts:
            del drafts[key]
        else:
            self._draft_written[key] = None
            return
        if self._drafts.save(document):
            self._draft_written[key] = mark

    def drop_draft(self, key=None):
        if not self.persistent:
            return
        key = key or self.draft_key()
        document = self._drafts_document()
        if document["drafts"].pop(key, None) is not None:
            self._drafts.save(document)
        self._draft_written.pop(key, None)

    # -- saving ---------------------------------------------------------------
    def _storage_environment(self, preset):
        """Point ``preset`` at the current environment, in the form it allows."""
        env = self.environment()
        preset.legacy, preset.all_environments, preset.environment_id = None, False, ""
        if env is None:
            # No environment picked: the run is every account at the address
            # typed - what "All environments" is - not an environment gone.
            preset.all_environments = True
            preset.project_id = self.project_id or lpf.WORKSPACE
            return
        if env.id == ALL_ENVIRONMENTS:
            preset.all_environments = True
        elif env.id.startswith(MISSING_PREFIX):
            preset.legacy = {"alias": env.name, "value": env.account_key}
        else:
            preset.environment_id = env.id
        preset.project_id = env.project_id or lpf.WORKSPACE

    @staticmethod
    def _split(state):
        config = lps.without_environment(state)
        variables = dict(config.pop("variables", None) or {})
        return config, variables

    def save(self, state, name=None, overwrite=False):
        """Save ``state`` as the open configuration, or as a new one called ``name``.

        Raises PresetConflict when the open configuration was saved elsewhere
        since it was opened here - unless ``overwrite``.
        """
        preset = self.preset()
        config, variables = self._split(state)
        if preset is None:
            env = self.environment()
            project_id = env.project_id if env is not None else lpf.WORKSPACE
            preset = lps.Preset("", self.presets.unique_name(project_id, name or "Launch"),
                                project_id, "", 0, config, variables)
            self._storage_environment(preset)
            new_key = self.draft_key()
            saved = self.presets.put(preset)
            self.drop_draft(new_key)
        else:
            preset.config, preset.variables = config, variables
            saved = self.presets.put(preset, None if overwrite else self.opened_revision)
            self.drop_draft()
        self.preset_id = saved.id
        self.opened_revision = saved.revision
        if not self.environment_id:
            # Saved with none picked: it is listed - and shown - under the
            # environment it was written down as.
            self.environment_id = self.preset_environment_id(saved)
        self._remember()
        self.changed.emit()
        return saved

    def save_as(self, state, name):
        """A new configuration of this environment from ``state``; the open one
        keeps what it had saved (its edits went into the copy)."""
        if self.preset_id:
            self.drop_draft()
        self.preset_id = NEW
        return self.save(state, name)

    def rename(self, name):
        preset = self.preset()
        if preset is None or not name or name == preset.name:
            return preset
        preset.name = self.presets.unique_name(preset.project_id, name)
        saved = self.presets.put(preset)
        self.opened_revision = saved.revision if self.opened_revision is not None \
            else None
        self.changed.emit()
        return saved

    def delete(self):
        preset = self.preset()
        if preset is None:
            return
        self.drop_draft()
        self.presets.remove(preset.id)
        self.preset_id = NEW
        self._remember()
        self.changed.emit()

    def revert(self):
        """Forget the working copy of the open configuration: what it saved."""
        self.drop_draft()
        preset = self.preset()
        if preset is not None:
            self.opened_revision = preset.revision
        return self.compose(preset) if preset is not None else None

    def use_with_environment(self, env_id, state, move=False):
        """Put the open configuration in another environment.

        A copy by default - the original keeps running where it was. ``move``
        rebinds it instead: what a configuration whose environment is gone
        needs, since there is nothing left to keep it for.
        """
        preset = self.preset()
        config, variables = self._split(state)
        target = self.environment(env_id)
        if target is None:
            return None
        self.environment_id = env_id
        if preset is not None and move:
            preset.config, preset.variables = config, variables
            self._storage_environment(preset)
            self.drop_draft()
            saved = self.presets.put(preset)
        else:
            base = preset.name if preset is not None else "Launch"
            clone = lps.Preset("", "", target.project_id, "", 0, config, variables)
            self._storage_environment(clone)
            clone.name = self.presets.unique_name(clone.project_id,
                                                  "%s (%s)" % (base, target.name))
            if preset is not None:
                self.drop_draft()
            saved = self.presets.put(clone)
        self.preset_id = saved.id
        self.opened_revision = saved.revision
        self._remember()
        self.changed.emit()
        return saved

    # -- variables ------------------------------------------------------------
    def definitions(self, env=None):
        """The variables the environment gives values to (all text)."""
        return lpf.scope(self._values(env)).definitions

    def _values(self, env=None):
        env = env if env is not None else self.environment()
        return dict(env.variables) if env is not None and not self.is_stand_in(env) \
            else {}

    def scope(self, state):
        """The places this run's variables come from, nearest last.

        The working copy's values are this configuration's: one that equals
        what is saved is labelled the configuration's, one that differs - or
        belongs to a configuration not saved yet - this run's, with the saved
        value as what it overrides.
        """
        preset = self.preset()
        saved = dict(preset.variables) if preset is not None else {}
        draft = dict((state or {}).get("variables") or {})
        return lpf.scope(self._values(), [
            launchvars.Layer(SOURCE_CONFIGURATION,
                             {name: saved[name] for name in saved if name in draft}),
            launchvars.Layer(SOURCE_RUN,
                             {name: value for name, value in draft.items()
                              if name not in saved or saved[name] != value})])

    def env_urls(self, env=None):
        """``{account key: address}`` for the rules - the current environment's."""
        env = env if env is not None else self.environment()
        if env is None or self.is_stand_in(env) or not env.address:
            return {}
        return {env.account_key: env.address}

    def _env_row_change(self, apply):
        env = self.environment()
        if env is None or self.is_stand_in(env):
            raise lpf.LaunchProjectsError("This environment is not one that can be "
                                          "changed here.")
        self.registry.update(lambda projects: apply(lpf.environment_row(projects, env)))
        self.registry_changed.emit()
        self.changed.emit()

    def set_environment_address(self, address):
        self._env_row_change(lambda row: setattr(row, "address", (address or "").strip()))

    def set_environment_value(self, name, value):
        def apply(row):
            if value is None:
                row.variables.pop(name, None)
            else:
                row.variables[name] = lpf.as_text(value)
        self._env_row_change(apply)

    # -- links ----------------------------------------------------------------
    def link_target(self):
        """What a desktop link to the open configuration names: its id."""
        return self.preset_id
