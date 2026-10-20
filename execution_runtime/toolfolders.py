"""Folders a restricted program needs only because of the tools it runs.

A program held to its project folder still has to start. When it is ``python3``
from the system, nothing is needed: ``/usr`` is always readable. When it is a
virtualenv outside the project, a CLI installed under the home folder, or a
script whose interpreter lives there, the program would fail to start - and the
person would see "No such file or directory" for something they never thought
of as a file. So the adapter asks here, and the folders found are granted
read-only and shown in the window as "added automatically".

Configuration that a tool keeps for itself (``~/.claude`` for the agent CLI,
``~/.gitconfig``) is granted the same way. Keys are never on this list:
``~/.ssh`` stays hidden unless somebody grants it on purpose.
"""

import os
import shutil

from .restrictions import within

SYSTEM_ROOTS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc", "/opt")

#: What each agent CLI keeps for itself, granted read-write: its sign-in and its
#: session files live there, and it refuses to start without writing them.
#: A single file in the home folder cannot be bound read-write - the tool
#: replaces it by renaming a new copy over it, which a mount point refuses - so
#: those are handed over as a private copy the program may rewrite freely.
AGENT_CONFIG = {"claude": ("~/.claude", "~/.claude.json", "~/.config/claude"),
                "codex": ("~/.codex",), "gemini": ("~/.gemini",)}
GIT_CONFIG = ("~/.gitconfig", "~/.config/git")


def _expand(path, home):
    return home + path[1:] if path.startswith("~") else path


def _system(path):
    return any(within(root, path) for root in SYSTEM_ROOTS)


def _which(name, env):
    if os.sep in name:
        return name if os.path.isfile(name) else ""
    return shutil.which(name, path=(env or {}).get("PATH") or os.environ.get("PATH", "")) or ""


def _interpreter(path):
    """The interpreter a ``#!`` script names, or ""."""
    try:
        with open(path, "rb") as handle:
            line = handle.readline(512)
    except OSError:
        return ""
    if not line.startswith(b"#!"):
        return ""
    words = line[2:].decode("utf-8", "replace").split()
    if not words:
        return ""
    if os.path.basename(words[0]) == "env" and len(words) > 1:
        return words[1] if not words[1].startswith("-") else (words[2] if len(words) > 2 else "")
    return words[0]


def _prefix(executable):
    """The installation an executable belongs to: ``.../bin/x`` -> ``...``."""
    folder = os.path.dirname(executable)
    return os.path.dirname(folder) if os.path.basename(folder) == "bin" else folder


def for_command(argv, env=None, home=None, visible=()):
    """``(argv, folders)`` - the command as it should be started, and what it needs.

    ``visible`` are folders the program already gets (its project folder);
    anything inside them needs nothing more. The first argument is replaced
    by its real path when the name it was called by would not be reachable
    inside the sandbox (a symlink under ``~/.local/bin``).
    """
    home = home or os.path.expanduser("~")
    folders = []

    def grant(path, access="read_only", reason=""):
        path = os.path.normpath(path)
        if not path or path == "/" or _system(path) or any(within(one, path) for one in visible):
            return
        if not os.path.lexists(path):
            return
        if any(one["path"] == path for one in folders):
            return
        folders.append({"path": path, "access": access, "reason": reason})

    argv = list(argv)
    if not argv:
        return argv, folders
    called = os.path.basename(argv[0])
    found = _which(argv[0], env)
    if found:
        real = os.path.realpath(found)
        if not _system(real) and not any(within(one, real) for one in visible):
            grant(_prefix(real), reason="%s, the program this runs" % called)
            if os.path.realpath(found) != os.path.abspath(found):
                argv[0] = real
            # A virtualenv outside the project also needs the Python it was made from.
            config = os.path.join(_prefix(real), "pyvenv.cfg")
            if os.path.isfile(config):
                try:
                    with open(config, encoding="utf-8") as handle:
                        for line in handle:
                            key, _, value = line.partition("=")
                            if key.strip() == "home" and value.strip():
                                grant(_prefix(os.path.join(value.strip(), "python")), reason="the Python this environment was made from")
                except OSError:
                    pass
        script = _interpreter(real)
        if script:
            located = _which(script, env)
            if located and not _system(os.path.realpath(located)):
                grant(_prefix(os.path.realpath(located)), reason="the interpreter %s needs" % called)
    for config in AGENT_CONFIG.get(called, ()):
        path = _expand(config, home)
        if os.path.isfile(path):
            grant(path, "copy", "%s keeps its settings here (a private copy)" % called)
        else:
            grant(path, "read_write", "%s keeps its sign-in and sessions here" % called)
    if called.startswith("python"):
        local = os.path.join(home, ".local", "lib")
        if os.path.isdir(local) and any(one.startswith("python") for one in os.listdir(local)):
            grant(local, reason="Python packages installed for your user")
    for config in GIT_CONFIG:
        grant(_expand(config, home), reason="your git name and settings")
    return argv, folders
