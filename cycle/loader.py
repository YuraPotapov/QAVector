"""Finding and reading cycle files across the ``cycles/`` trees.

The same two-tree arrangement ``engine/loader.py`` uses for flows, for the same
reason: an installed build keeps what it ships inside the bundle, where nothing
can be written, so anything the user creates lives in a tree of their own that
is searched first. A user cycle shadows a bundled one with the same id. In a
source checkout both trees are the same directory and the list collapses to one
entry, so a checkout behaves as if there were only ever one.

Simpler than flows in one way: a cycle id is flat. Flows have dotted ids because
a block lives in a subdirectory and is pulled in by name from many scenarios;
cycles are not composed out of each other yet, so ``nightly`` is
``cycles/nightly.yaml`` and there is no second convention to remember.

``pyyaml`` is imported lazily, so importing this module - which ``--describe``
does on every start - costs nothing until a file is actually read.
"""

import os

import runtime_paths

from cycle.model import CycleError, parse_cycle

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CYCLES_DIR = os.path.join(PROJECT_ROOT, "cycles")

#: What a cycle file is called. ``.yml`` is read but never written: both
#: spellings are common enough that refusing one would be a papercut, and
#: writing both would leave two files claiming the same id.
EXTENSIONS = (".yaml", ".yml")


class CycleNotFound(CycleError):
    """No file exists for a requested cycle id."""


def search_path(cycles_dir=None):
    """The trees to look in, nearest first.

    ``None`` means the layered default. A single directory is taken literally -
    that is what ``--cycles-dir`` means. A list is taken literally too, which is
    how a caller stages a candidate tree in front of the real ones to validate
    something that has not been written yet.
    """
    if isinstance(cycles_dir, (list, tuple)):
        return [str(entry) for entry in cycles_dir] or [DEFAULT_CYCLES_DIR]
    if cycles_dir:
        return [cycles_dir]
    return runtime_paths.cycles_search_path() or [DEFAULT_CYCLES_DIR]


def _load_yaml(path):
    import yaml  # lazy: only needed when actually reading a cycle
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def candidate_paths(cycle_id, cycles_dir=None):
    """Every path ``cycle_id`` could name, best first, across every tree."""
    for root in search_path(cycles_dir):
        for extension in EXTENSIONS:
            yield os.path.join(root, cycle_id + extension)


def cycle_path(cycle_id, cycles_dir=None):
    """Resolve a cycle id to a file path, which may not exist.

    The first tree that has the file wins. When nothing does, the canonical path
    in the *nearest* tree comes back, because that is where the file would go
    and it makes "no cycle file for X" point somewhere useful.
    """
    for candidate in candidate_paths(cycle_id, cycles_dir):
        if os.path.exists(candidate):
            return candidate
    return canonical_path(cycle_id, cycles_dir)


def canonical_path(cycle_id, cycles_dir=None):
    """Where ``cycle_id`` belongs when it is written, whether or not it exists."""
    return os.path.join(search_path(cycles_dir)[0], cycle_id + EXTENSIONS[0])


def is_bundled(path, cycles_dir=None):
    """True when ``path`` is in a tree that ships with the app.

    What stops the editor deleting or overwriting something an upgrade will put
    back. In a source checkout the two trees are one, so nothing is bundled and
    everything is editable - which is what a developer wants.
    """
    trees = search_path(cycles_dir)
    if len(trees) < 2:
        return False
    target = os.path.normpath(os.path.abspath(path))
    writable = os.path.normpath(os.path.abspath(trees[0]))
    if target.startswith(writable + os.sep):
        return False
    return any(target.startswith(os.path.normpath(os.path.abspath(root)) + os.sep)
               for root in trees[1:])


def read_cycle(cycle_id, cycles_dir=None):
    """``(cycle, raw)`` for one cycle id. Raises :class:`CycleNotFound`.

    The raw mapping comes back alongside the parsed cycle because validation
    needs it: unknown keys can only be reported from what was actually written,
    since parsing has already dropped them.
    """
    path = cycle_path(cycle_id, cycles_dir)
    if not os.path.exists(path):
        raise CycleNotFound("no cycle file for %r (looked at %s)"
                            % (cycle_id, path))
    raw = _load_yaml(path)
    return parse_cycle(raw, cycle_id, source=path), raw


def load_cycle(cycle_id, cycles_dir=None):
    """One cycle, parsed. Raises :class:`CycleNotFound` or :class:`CycleError`."""
    return read_cycle(cycle_id, cycles_dir)[0]


def cycle_files(cycles_dir=None):
    """``{id: path}`` for every cycle in every tree, nearest tree winning.

    One id names one file even when two trees offer it, so a user cycle shadows
    a bundled one rather than both turning up and disagreeing about what should
    run.
    """
    found = {}
    for root in search_path(cycles_dir):
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            stem, extension = os.path.splitext(name)
            if extension not in EXTENSIONS or not stem:
                continue
            path = os.path.join(root, name)
            if os.path.isfile(path):
                found.setdefault(stem, path)
    return found


def discover(cycles_dir=None):
    """Every readable cycle as ``(id, cycle, raw, path)``, by id.

    A file that does not parse is skipped rather than raising: the inventory is
    read on every start, and one broken cycle must not be the reason the
    application cannot list the others. Whoever wants to know *why* it is
    missing asks for it by id and gets the error.
    """
    found = []
    for cycle_id, path in sorted(cycle_files(cycles_dir).items()):
        try:
            raw = _load_yaml(path)
            cycle = parse_cycle(raw, cycle_id, source=path)
        except (CycleError, OSError, ValueError):
            continue
        except Exception:                 # a yaml parse error, whatever it is
            continue
        found.append((cycle_id, cycle, raw, path))
    return found
