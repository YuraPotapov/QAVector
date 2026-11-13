"""Where an agent's program is on this computer, without anybody saying.

A person who installed an agent CLI should not need to know where it went. The
program is looked for, in order:

1. the program a connection names, when it names one - and only there: a path
   somebody chose is never swapped for another;
2. PATH;
3. the places its installers put it (``places``: ``(label, pattern)`` pairs).
   A desktop app that carries the CLI inside it, npm's global folders, nvm's
   Node versions (newest first), Homebrew, ``~/.local/bin``. QAVector started
   from a desktop menu often has a shorter PATH than a terminal, which is why
   these are looked at even when the terminal finds the program.

Nothing is run and nothing is written: a few ``stat`` calls, cheap enough to
repeat whenever a program is needed, so an app that updated or moved its CLI
is found again.
"""

import glob
import os
import re
import shutil

#: What ``where`` says for a program found on PATH, or named by a connection.
ON_PATH, GIVEN = "PATH", "given"


def runnable(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _version_key(path):
    """Newest Node first for nvm-style folders (``.../v18.20.8/bin/x``)."""
    return [int(part) for part in re.findall(r"\d+", path)]


def _expand(pattern):
    pattern = os.path.expandvars(os.path.expanduser(pattern))
    if "$" in pattern or "%" in pattern:
        return []                       # a variable this system does not set
    if not glob.has_magic(pattern):
        return [pattern]
    return sorted(glob.glob(pattern), key=_version_key, reverse=True)


def find(name, places=(), binary=""):
    """``(path, where)`` for a program; ``("", "")`` when it is nowhere.

    ``where`` is :data:`GIVEN`, :data:`ON_PATH` or the label of the place it
    was found in.
    """
    given = (binary or "").strip()
    if given and given != name:
        if os.path.dirname(os.path.expanduser(given)):
            path = os.path.abspath(os.path.expanduser(given))
            return (path, GIVEN) if runnable(path) else ("", "")
        found = shutil.which(given)
        return (found, ON_PATH) if found else ("", "")
    found = shutil.which(name)
    if found:
        return found, ON_PATH
    for label, pattern in places:
        for path in _expand(pattern):
            if runnable(path):
                return os.path.abspath(path), label
    return "", ""


def node_places(command):
    """Where Node package managers put a global ``command`` on this platform."""
    if os.name == "nt":
        return (("npm", "%APPDATA%/npm/" + command + ".cmd"),
                ("Volta", "%LOCALAPPDATA%/Volta/bin/" + command + ".exe"))
    return (("npm", "~/.npm-global/bin/" + command),
            ("npm", "~/.local/share/npm/bin/" + command),
            ("nvm", "~/.nvm/versions/node/*/bin/" + command),
            ("fnm", "~/.local/share/fnm/node-versions/*/installation/bin/" + command),
            ("Volta", "~/.volta/bin/" + command),
            ("Bun", "~/.bun/bin/" + command))


def common_places(command):
    """Per-user and package-manager folders that are often not on a menu's PATH."""
    if os.name == "nt":
        return (("~/.local/bin", "~/.local/bin/" + command + ".exe"),)
    return (("~/.local/bin", "~/.local/bin/" + command),
            ("Homebrew", "/opt/homebrew/bin/" + command),
            ("Homebrew", "/home/linuxbrew/.linuxbrew/bin/" + command),
            ("Homebrew", "/usr/local/bin/" + command))
