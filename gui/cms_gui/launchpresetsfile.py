"""Saved Launch Sessions configurations, each with an id of its own.

A configuration used to be filed under its name, which made the name its
identity: renaming one broke every desktop link pointing at it, and two projects
could not both have a "Smoke". Here a configuration has a stable id, belongs to
a project and to one environment, and carries a **revision** that goes up with
each save - so a save made in another window since this one opened it is noticed
rather than written over.

The file lives with the GUI's other private documents (``store.app_data_dir()``)
and is read the forgiving way ``configs.json`` always was: a file that cannot be
parsed reads as empty and is copied aside before anything is written over it.

Its first version is made from ``configs.json`` (see :func:`migrate_configs`),
which is left exactly as it was - the copy to go back to, and what an older
build still reads. A configuration whose environment cannot be told yet keeps
what it was saved with (``legacy``) until the inventory says which environment
that is (:func:`bind_environments`); one that no environment matches any more
stays visible, flagged, rather than being rebound to a guess.

No Qt here.
"""

import copy
import os
import time
import uuid

from . import store

VERSION = 1
FILE_NAME = "launch-presets.json"
CONFIGS_FILE = "configs.json"

KEYS = ("id", "name", "project_id", "environment_id", "revision", "config",
        "variables", "legacy", "all_environments")

#: The keys of a launch configuration that are the environment - context here,
#: held by ``environment_id`` rather than inside the document.
ENV_KEYS = ("environment", "environment_value")


class PresetConflict(Exception):
    """The configuration was saved somewhere else since it was opened here."""

    def __init__(self, current):
        super().__init__("\"%s\" was saved elsewhere since it was opened here."
                         % current.name)
        self.current = current


class Preset(object):
    """One saved configuration."""

    def __init__(self, id="", name="", project_id="", environment_id="", revision=0,
                 config=None, variables=None, legacy=None, all_environments=False,
                 extra=None):
        self.id = id or ""
        self.name = name or ""
        self.project_id = project_id or ""
        self.environment_id = environment_id or ""
        self.revision = int(revision or 0)
        self.config = dict(config or {})
        self.variables = dict(variables or {})
        #: {"alias", "value"} it was saved with, while no environment is known.
        self.legacy = dict(legacy) if legacy else None
        #: Saved with "All environments": one address for every account.
        self.all_environments = bool(all_environments)
        self.extra = dict(extra or {})

    @classmethod
    def from_entry(cls, entry):
        return cls(entry.get("id", ""), entry.get("name", ""),
                   entry.get("project_id", ""), entry.get("environment_id", ""),
                   entry.get("revision", 0),
                   entry.get("config") if isinstance(entry.get("config"), dict) else {},
                   entry.get("variables") if isinstance(entry.get("variables"),
                                                        dict) else {},
                   entry.get("legacy") if isinstance(entry.get("legacy"), dict) else None,
                   entry.get("all_environments", False),
                   {key: value for key, value in entry.items() if key not in KEYS})

    def to_entry(self):
        entry = dict(self.extra)
        entry.update({"id": self.id, "name": self.name, "project_id": self.project_id,
                      "environment_id": self.environment_id or None,
                      "revision": self.revision, "config": copy.deepcopy(self.config)})
        if self.variables:
            entry["variables"] = dict(self.variables)
        if self.legacy:
            entry["legacy"] = dict(self.legacy)
        if self.all_environments:
            entry["all_environments"] = True
        return entry

    def copy(self):
        return Preset.from_entry(copy.deepcopy(self.to_entry()))

    def __eq__(self, other):
        return isinstance(other, Preset) and self.to_entry() == other.to_entry()

    __hash__ = None

    def __repr__(self):
        return "Preset(%r, %r)" % (self.id, self.name)


def new_id():
    return "c-%s" % uuid.uuid4().hex[:8]


def stamp():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def without_environment(config):
    """``config`` with the environment taken out: that is the preset's context."""
    return {key: copy.deepcopy(value) for key, value in (config or {}).items()
            if key not in ENV_KEYS}


class Presets(object):
    """``launch-presets.json``: read afresh for every question, changed by re-reading.

    The same discipline as ``store.NamedConfigs``: a second window of the
    application is a second copy of this object, so nothing is answered from,
    or written back as, a snapshot taken at startup.
    """

    def __init__(self, path):
        self.path = path
        self._store = store.JsonStore(path, default={})

    # -- reading --------------------------------------------------------------
    def _document(self):
        document = self._store.load()
        presets = document.get("presets")
        if not isinstance(presets, list):
            presets = []
        document["presets"] = [entry for entry in presets if isinstance(entry, dict)]
        return document

    @property
    def unreadable(self):
        """True when the file is there but could not be read (it reads as empty)."""
        self._store.load()
        return self._store.unreadable

    def all(self):
        return [Preset.from_entry(entry) for entry in self._document()["presets"]]

    def get(self, preset_id):
        for preset in self.all():
            if preset.id == preset_id:
                return preset
        return None

    def migrated(self):
        return isinstance(self._document().get("migrated"), dict)

    def find(self, token):
        """``(preset, candidates)`` for what a desktop link names.

        An id first - what every link written from now on carries. Then a name,
        for the links written before ids existed: a name only one configuration
        has, or the name a configuration had when it was migrated (it may have
        been renamed since). Two configurations sharing it is not guessed at:
        the preset is None and the candidates are named.
        """
        presets = self.all()
        for preset in presets:
            if preset.id == token:
                return preset, []
        named = [preset for preset in presets if preset.name == token]
        if len(named) == 1:
            return named[0], []
        if named:
            return None, named
        legacy = (self._document().get("migrated") or {}).get("names") or {}
        if token in legacy:
            for preset in presets:
                if preset.id == legacy[token]:
                    return preset, []
        return None, []

    def unique_name(self, project_id, base):
        """``base``, or ``base (2)`` - the first form no configuration of the
        project is using."""
        taken = {preset.name for preset in self.all() if preset.project_id == project_id}
        if base not in taken:
            return base
        index = 2
        while "%s (%d)" % (base, index) in taken:
            index += 1
        return "%s (%d)" % (base, index)

    # -- writing --------------------------------------------------------------
    def _write(self, document):
        document["version"] = VERSION
        if not self._store.save(document):
            raise OSError("cannot write %s" % self.path)

    def put(self, preset, base_revision=None):
        """Save ``preset``; returns what was written (its revision moved on).

        ``base_revision`` is the revision it was opened at: when the file holds
        another one, somebody saved it since, and :class:`PresetConflict` says
        so instead of writing over their save. None skips the check.
        """
        document = self._document()
        entries = document["presets"]
        saved = preset.copy()
        for index, entry in enumerate(entries):
            if entry.get("id") == saved.id:
                current = Preset.from_entry(entry)
                if base_revision is not None and current.revision != base_revision:
                    raise PresetConflict(current)
                saved.revision = current.revision + 1
                entries[index] = saved.to_entry()
                break
        else:
            saved.id = saved.id or new_id()
            saved.revision = max(1, saved.revision + 1)
            entries.append(saved.to_entry())
        self._write(document)
        return saved

    def remove(self, preset_id):
        document = self._document()
        before = len(document["presets"])
        document["presets"] = [entry for entry in document["presets"]
                               if entry.get("id") != preset_id]
        if len(document["presets"]) != before:
            self._write(document)

    def replace_all(self, presets, migrated=None):
        """Write ``presets`` as the whole list (migration and binding)."""
        document = self._document()
        document["presets"] = [preset.to_entry() for preset in presets]
        if migrated is not None:
            document["migrated"] = migrated
        self._write(document)


# -- where the first version comes from -----------------------------------------
def migrate_configs(presets, configs_path, project_id):
    """Make ``launch-presets.json``'s first version from ``configs.json``.

    Once: the marker it leaves says so, and a second call does nothing. Every
    configuration keeps its name and settings and gets an id; its environment
    is kept as it was saved (``legacy``) for :func:`bind_environments` to look
    up, because that needs the inventory and this runs before it arrives.
    ``configs.json`` itself is not touched. Returns ``{name: id}``.
    """
    if presets.migrated():
        return {}
    existing = presets.all()
    configs = store.JsonStore(configs_path, default={}).load() \
        if configs_path and os.path.exists(configs_path) else {}
    names = {}
    for name, config in sorted(configs.items(), key=lambda item: str(item[0]).lower()):
        if not isinstance(config, dict):
            continue
        alias = str(config.get("environment") or "")
        preset = Preset(new_id(), str(name), project_id, "", 1,
                        without_environment(config), {},
                        {"alias": alias, "value": str(config.get("environment_value")
                                                     or "")} if alias else None,
                        all_environments=not alias)
        existing.append(preset)
        names[str(name)] = preset.id
    presets.replace_all(existing, {"from": CONFIGS_FILE, "at": stamp(), "names": names})
    return names


def bind_environments(presets, environments, inventory):
    """Give every configuration still holding a ``legacy`` environment its id.

    The value it was saved with first (the environment's identity), then the
    alias as the inventory reads it today. Bound only when exactly one
    environment holds those accounts; otherwise it stays as it is, and the page
    shows it as needing an environment. Returns how many were bound.
    """
    every = presets.all()
    bound = 0
    for preset in every:
        if not preset.legacy:
            continue
        value = preset.legacy.get("value") or ""
        if not value and inventory is not None:
            value = inventory.env_value(preset.legacy.get("alias") or "")
        holders = [env for env in environments if value and env.account_key == value]
        if len(holders) == 1:
            preset.environment_id = holders[0].id
            preset.project_id = holders[0].project_id
            preset.legacy = None
            bound += 1
    if bound:
        presets.replace_all(every)
    return bound
