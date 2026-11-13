"""Notice when the scenarios folder changes outside this window (ADR-004 D4).

An agent writes a scenario's YAML with its own tools; so does a text editor, or
git. The Scenarios page used to show the old list until Refresh. :class:`FolderWatch`
tells it instead - and costs nothing while nothing changes:

* **No polling.** ``QFileSystemWatcher`` is inotify on Linux: the kernel wakes
  this window when a watched folder or file changes, and otherwise never.
  Folders are watched for files coming and going; each scenario file for its
  text changing in place (a folder's watch does not see that).
* **One answer per burst.** An agent writing several files, an editor saving
  through a temporary file - every event restarts one short timer, and only
  its end does any work.
* **A cheap look first.** At the timer's end the folder's signature - names,
  sizes, modification times, by ``os.scandir`` - is compared with the last one.
  Unchanged (this window's own save, already re-read; a file touched and put
  back) is nothing to do. :meth:`settle` takes the signature again right after
  this window writes, so its own saves never come back as news.

Only then is :attr:`changed` emitted. What it costs after that is the page's
business: the window re-reads the inventory off the GUI thread.
"""

import os

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

#: The quiet a burst of writes is allowed before anything is done.
DELAY_MS = 300
#: Files a folder of scenarios is watched for at most (inotify watches are a
#: per-user budget other programs share).
MAX_FILES = 4000
SUFFIXES = (".yaml", ".yml")


def _scan(root):
    """(signature, folders, files) of the scenario files under ``root``."""
    signature, folders, files = [], [], []
    if not root or not os.path.isdir(root):
        return (), folders, files
    todo = [root]
    while todo:
        folder = todo.pop()
        folders.append(folder)
        try:
            entries = sorted(os.scandir(folder), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith("."):
                continue                    # .git, an editor's swap file, a temporary save
            try:
                if entry.is_dir(follow_symlinks=False):
                    todo.append(entry.path)
                elif entry.name.endswith(SUFFIXES) and len(files) < MAX_FILES:
                    stat = entry.stat()
                    files.append(entry.path)
                    signature.append((entry.path, stat.st_size, stat.st_mtime_ns))
            except OSError:
                continue                    # gone between listing and looking: the next event says so
    return tuple(signature), folders, files


class FolderWatch(QObject):
    """Watches one folder of scenarios; :attr:`changed` when what is in it changed."""

    changed = Signal()

    def __init__(self, parent=None, delay_ms=DELAY_MS):
        super().__init__(parent)
        self._root = ""
        self._signature = ()
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._poke)
        self._watcher.fileChanged.connect(self._poke)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(self._look)
        #: How many times the folder was looked at, and how many changes told - for tests.
        self.looks = 0
        self.told = 0

    @property
    def root(self):
        return self._root

    def set_root(self, root):
        """Watch ``root`` (the user's scenarios folder) instead of what was watched."""
        root = os.path.abspath(root) if root else ""
        if root == self._root:
            return
        self._root = root
        self._timer.stop()
        self.settle()

    def settle(self):
        """Take the folder as it is now as known: after this window wrote to it."""
        self._signature, folders, files = _scan(self._root)
        self._follow(folders, files)

    def _poke(self, _path):
        self._timer.start()             # restarted by every event of a burst

    def _look(self):
        self.looks += 1
        signature, folders, files = _scan(self._root)
        # A file saved by renaming a new one over it is a new file to the kernel:
        # watch what is there now.
        self._follow(folders, files)
        if signature == self._signature:
            return
        self._signature = signature
        self.told += 1
        self.changed.emit()

    def _follow(self, folders, files):
        wanted = set(folders) | set(files)
        watched = set(self._watcher.directories()) | set(self._watcher.files())
        gone = sorted(watched - wanted)
        if gone:
            self._watcher.removePaths(gone)
        new = sorted(path for path in wanted - watched if os.path.exists(path))
        if new:
            self._watcher.addPaths(new)
