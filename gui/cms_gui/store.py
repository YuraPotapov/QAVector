"""Small JSON documents the GUI owns, on disk rather than in QSettings.

QSettings is the right home for a preference - a path, a flag, the last page.
It is the wrong home for a growing document: the history is hundreds of entries
deep and every write rewrites the whole blob, and an archived log is a file, not
a value. So anything that grows lives here instead, next to the logs it refers
to, in a directory the user can open and inspect.

Writes are atomic (temp file + ``os.replace``) and reads never raise: a
half-written or hand-edited file degrades to the default rather than taking a
widget down with it. That mirrors ``usersfile.save``, which is kept separate
because it also keeps a ``.bak`` and knows the shape of ``users.json``.
"""

import json
import os
import shutil
import tempfile
import time

from PySide6.QtCore import QStandardPaths

#: The GUI's own directory under the platform's data location, named for the
#: project. The one the old name used is moved here once at startup (migrate),
#: and read in its place for as long as it has not been.
APP_NAME = "qavector"
LEGACY_APP_NAME = "chrome-multi-session"
APP_DIR_NAME = os.path.join(APP_NAME, "gui")


def data_base():
    """The platform's data location: ~/.local/share on Linux, AppData on Windows."""
    base = QStandardPaths.writableLocation(QStandardPaths.GenericDataLocation)
    return base or os.path.join(os.path.expanduser("~"), ".local", "share")


def app_data_dir():
    """The GUI's own data directory, created if it is not there yet.

    Built from GenericDataLocation and the name spelled out here rather than
    from AppDataLocation, because that one is derived from the application's
    organisation name, which would move the history the day that name changed.
    """
    base = data_base()
    path = os.path.join(base, APP_DIR_NAME)
    legacy = os.path.join(base, LEGACY_APP_NAME, "gui")
    if not os.path.exists(os.path.join(base, APP_NAME)) and os.path.isdir(legacy):
        # Not moved yet - or it could not be. The history is where it is, and an
        # empty new directory would look like having lost it.
        path = legacy
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def logs_dir(directory=None):
    """Where per-run log archives go."""
    path = os.path.join(directory or app_data_dir(), "logs")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


class JsonStore:
    """One JSON file, read forgivingly and written atomically."""

    def __init__(self, path, default=None):
        self.path = path
        self._default = default if default is not None else {}
        #: True when the file is there but could not be read. Kept because the
        #: next save would otherwise write the empty default straight over it.
        self.unreadable = False

    def load(self):
        # utf-8-sig, not utf-8: a file written by a Windows shell carries a BOM
        # (PowerShell 5.1 spells "utf8" that way), and json.load refuses one.
        # That is not a hypothetical - it is how a full set of saved launch
        # configurations read back as none at all.
        self.unreadable = False
        try:
            with open(self.path, encoding="utf-8-sig") as handle:
                value = json.load(handle)
        except OSError:
            return self._copy_default()          # absent: nothing to lose
        except ValueError:
            self.unreadable = True
            return self._copy_default()
        if type(value) is not type(self._default):
            self.unreadable = True
            return self._copy_default()
        return value

    def preserve_unreadable(self):
        """Copy a file we could not parse aside, before anything overwrites it.

        Losing what someone saved is worse than any error message, and an
        unreadable file is still their data - it may be one stray byte from
        being readable again.
        """
        if not self.unreadable:
            return ""
        backup = "%s.unreadable-%s" % (self.path, time.strftime("%Y%m%d-%H%M%S"))
        try:
            shutil.copy2(self.path, backup)
        except OSError:
            return ""
        self.unreadable = False
        return backup

    def save(self, value):
        """Replace the file's contents; returns True when it reached disk."""
        directory = os.path.dirname(self.path) or "."
        self.preserve_unreadable()
        try:
            os.makedirs(directory, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=directory, prefix=".tmp-", delete=False)
            try:
                with handle:
                    json.dump(value, handle, indent=2, ensure_ascii=False, default=str)
                os.replace(handle.name, self.path)
            except BaseException:
                _unlink(handle.name)
                raise
        except OSError:
            return False
        return True

    def _copy_default(self):
        return json.loads(json.dumps(self._default))


class NamedConfigs:
    """``{name: document}`` on disk - the saved Launch Sessions configurations."""

    def __init__(self, path):
        self._store = JsonStore(path, default={})
        self._items = self._from_disk()

    def _from_disk(self):
        """What the file holds right now, entries that are documents only."""
        return {k: v for k, v in self._store.load().items()
                if isinstance(v, dict)}

    def names(self):
        # Read afresh: a second window of this app is a second copy of this
        # object, and the list should show what was saved, not what was on disk
        # when this one started.
        self._items = self._from_disk()
        return sorted(self._items, key=str.lower)

    def get(self, name):
        value = self._items.get(name)
        return json.loads(json.dumps(value)) if isinstance(value, dict) else None

    def put(self, name, document):
        # Read, change, write - never write a snapshot taken at startup. Saving
        # a stale one is how one window's save erased another's, and how a file
        # that failed to parse once turned into an empty file for good.
        items = self._from_disk()
        items[name] = document
        self._store.save(items)
        self._items = items

    def remove(self, name):
        items = self._from_disk()
        if items.pop(name, None) is not None:
            self._store.save(items)
        self._items = items

    def rename(self, old, new):
        items = self._from_disk()
        if old in items and new and new != old:
            items[new] = items.pop(old)
            self._store.save(items)
        self._items = items

    def unique_name(self, base):
        """``base``, or ``base (2)`` - the first form nothing else is using."""
        if base not in self._items:
            return base
        index = 2
        while "%s (%d)" % (base, index) in self._items:
            index += 1
        return "%s (%d)" % (base, index)


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass
