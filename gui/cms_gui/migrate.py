"""The one-time move from the project's old name to QAVector.

Until 0.15.0 the project was chrome-multi-session, and three things on disk
carried that name:

* ``~/ChromeMultiSession`` - the user's own folder: services.json,
  logsources.json and the scenarios the GUI writes, and in an installed build
  users.json, the Chrome profiles and the reports as well;
* ``<data>/chrome-multi-session`` - the GUI's history, saved configurations and
  archived logs (``~/.local/share`` on Linux);
* the QSettings store ``chrome-multi-session``/``gui`` - every preference
  (``~/.config/chrome-multi-session/gui.conf`` on Linux, the registry on Windows).

Each folder is moved to its new name and a link is left at the old one. The
link is what makes the move safe rather than merely tidy: absolute paths into
the old folder are written all over the place - a runner's script in
services.json, a desktop link, a test checkout's generated conf, the settings
themselves - and every one of them keeps working through it. Where no link can
be made the folder is not moved at all (see ``core.move_folder``), and whatever
reads these folders falls back to the old name while it is still the real one.

The settings are copied rather than moved, because on Windows they are registry
keys, not a folder. A path in them that points into a moved folder is rewritten
on the way, so Settings shows where things are now. The old store is left as
it was: nothing reads it once the new one has anything in it, and it is a way
back.

Run once at startup, before anything opens a setting or the data directory.
Idempotent, and it never raises: a move that fails leaves the old name in use,
which is exactly how the application ran before.
"""

import logging
import os
import re

from PySide6.QtCore import QSettings

from . import core, settings, store

log = logging.getLogger(__name__)


def run():
    """Carry the old name's folders and settings over. Returns the moves in force."""
    moved = []
    try:
        # Never under $CMS_HOME: that is a second copy or a test run pointed at a
        # scratch directory, and it must not be able to reach the real one.
        if not os.environ.get("CMS_HOME", "").strip():
            home = os.path.expanduser("~")
            moved.append(_move(os.path.join(home, core.LEGACY_USER_DIR_NAME),
                               os.path.join(home, core.USER_DIR_NAME)))
        base = store.data_base()
        moved.append(_move(os.path.join(base, store.LEGACY_APP_NAME),
                           os.path.join(base, store.APP_NAME)))
        moved = [pair for pair in moved if pair]
        copy_settings(settings.LEGACY_ORG, settings.ORG, settings.APP, moved)
    except Exception as exc:     # a failed move must never stop the app starting
        log.warning("Could not carry the old settings over: %s", exc)
    return moved


def _move(old, new):
    """``(old, new)`` when ``new`` is the folder in use afterwards, else None."""
    return (old, new) if core.move_folder(old, new) == new else None


def copy_settings(old_org, new_org, app, moved=()):
    """Copy every preference from the old store into an empty new one.

    ``moved`` are ``(old folder, new folder)`` pairs: a value naming a path inside
    an old folder is rewritten to the new one. Only into an empty store, so a
    preference changed since is never overwritten by the one it replaced.
    Returns True when it copied anything.
    """
    target = QSettings(new_org, app)
    if target.allKeys():
        return False
    source = QSettings(old_org, app)
    keys = source.allKeys()
    if not keys:
        return False
    for key in keys:
        value = source.value(key)
        if isinstance(value, str):
            value = rewrite_paths(value, moved)
        elif isinstance(value, list):
            value = [rewrite_paths(item, moved) if isinstance(item, str) else item
                     for item in value]
        target.setValue(key, value)
    target.sync()
    return True


def rewrite_paths(text, moved):
    """``text`` with every path inside an old folder pointing into the new one."""
    for old, new in moved:
        # A whole path component only: ~/ChromeMultiSessionOld is somebody else's.
        pattern = re.escape(old) + r"(?=$|[\\/\"',;\s])"
        text = re.sub(pattern, lambda _match, new=new: new, text)
    return text
