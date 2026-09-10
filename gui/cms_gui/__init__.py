"""Desktop front-end for QAVector.

The GUI is a *client* of the launcher, never a copy of it: it spawns
``session_launcher.py`` through a configured interpreter, asks ``--describe``
what exists, and follows ``--events=-`` for what happens. Nothing about
environments, users or scenarios is defined here.
"""

import os
import re
import sys

#: Shown when nothing below can answer - a checkout with no pyproject.toml in
#: reach, which is a developer's own tree and not a release.
UNKNOWN_VERSION = "dev (not installed)"

_version = None


def version():
    """The version this build ships as, from the one place it is written down.

    The GUI and the core are one release: one pyproject, one .deb, one number.
    So this looks in the same three places ``session_launcher.version()`` looks,
    in the same order and for the same reasons - an installed distribution knows
    its own version, a frozen build has the VERSION file the packaging writes
    beside it, and a source checkout has pyproject.toml. What it deliberately
    does not do is keep a second copy here to drift out of step with that file.
    """
    global _version
    if _version is None:
        _version = (_installed_version() or _bundled_version()
                    or _checkout_version() or UNKNOWN_VERSION)
    return _version


def _installed_version():
    try:
        from importlib.metadata import version as _metadata_version
        return _metadata_version("qavector")
    except Exception:
        return ""


def _bundled_version():
    """The VERSION file a packaged build carries.

    The installed layout is ``<prefix>/{gui,core}/`` with VERSION at the prefix
    (packaging/build_deb.sh), so the parent is checked before the flat case of
    the file sitting next to the executable.
    """
    if not getattr(sys, "frozen", False):
        return ""
    here = os.path.dirname(os.path.abspath(sys.executable))
    roots = [os.path.dirname(here), here, getattr(sys, "_MEIPASS", "")]
    return _first_line(os.path.join(root, "VERSION") for root in roots if root)


def _checkout_version():
    """``version = "..."`` from the project's pyproject.toml, in a checkout.

    Read with a regex rather than a TOML parser: tomllib is 3.11+, the GUI runs
    on 3.9, and one key is not worth a dependency in the virtualenv.
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))    # gui/
    for root in (os.path.dirname(here), os.getcwd()):
        path = os.path.join(root, "pyproject.toml")
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        # Only the [project] table's own version, not a dependency's pin.
        project = re.split(r"(?m)^\[", text)
        for table in project:
            if not table.startswith("project]"):
                continue
            found = re.search(r"(?m)^\s*version\s*=\s*[\"']([^\"']+)[\"']", table)
            if found:
                return found.group(1)
    return ""


def _first_line(paths):
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                line = handle.readline().strip()
        except OSError:
            continue
        if line:
            return line
    return ""
