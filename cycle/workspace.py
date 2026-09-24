"""Where a run puts its files, and what its directory is called.

Every run gets a directory of its own::

    <cycle-runs>/20260916-193412-nightly/
        metadata.json          the whole run record
        logs/cycle.jsonl       the event stream, replayable
        steps/<id>/            stdout.log, stderr.log, outputs.json
        artifacts/             anything a plugin produced that is not a step's
        reports/               run.json, run.html

One directory rather than files scattered by kind, because everything the
design document wants out of a workspace - isolation, cleanup, reproducibility,
debugging - is served by being able to zip one path and attach it to a bug
report, and by ``rm -r`` being a complete cleanup.

Deliberately separate from ``engine/artifacts.py``, which owns the layout for
scenario reports and keeps it byte for byte for the sake of everything that
already reads it. This is the same idea for a bigger unit of work, and it fixes
one thing that layout got wrong rather than inheriting it: ``new_run_dir``
stamps a name to the second, so two runs starting in the same second share a
directory and interleave their files. :func:`new_run_id` notices and adds a
suffix.
"""

import os
import re
import time

import runtime_paths

#: The timestamp a run id starts with. Sorts lexically into chronological order,
#: which is what makes a directory listing a history.
STAMP = "%Y%m%d-%H%M%S"

#: What may appear in the cycle half of a run id. A run id becomes a directory
#: name on two filesystems and a field in every event, so it is kept to what is
#: safe everywhere rather than to what the current platform happens to allow.
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")

#: The subdirectories every run has, whether or not it fills them. Present from
#: the start so a half-finished run looks like a run rather than like a
#: directory something went wrong in.
SUBDIRS = ("logs", "steps", "artifacts", "reports")


def runs_root(root=None):
    """Where run directories go. ``None`` means the user's own data root."""
    return root or runtime_paths.cycle_runs_dir()


def safe_id(text):
    """``text`` reduced to what is safe in a filename on any platform."""
    cleaned = _UNSAFE.sub("-", str(text or "")).strip("-")
    return cleaned or "cycle"


def new_run_id(cycle_id, root=None, when=None):
    """A run id that no existing run directory already has.

    Two runs of the same cycle in the same second would otherwise be given the
    same name, share a directory, and write over each other's files - which is
    what ``engine/artifacts.new_run_dir`` does today. A suffix is added until
    the name is free, so the second run in a second is ``...-nightly-2``.
    """
    stamp = time.strftime(STAMP, time.localtime(when) if when else time.localtime())
    base = "%s-%s" % (stamp, safe_id(cycle_id))
    where = runs_root(root)
    candidate, suffix = base, 1
    while os.path.exists(os.path.join(where, candidate)):
        suffix += 1
        candidate = "%s-%d" % (base, suffix)
    return candidate


def create(run_id, root=None):
    """Make the run's directory and its subdirectories; return the path."""
    path = os.path.join(runs_root(root), run_id)
    for name in ("",) + SUBDIRS:
        os.makedirs(os.path.join(path, name) if name else path, exist_ok=True)
    return path


def path_of(run_id, root=None):
    """Where a run's directory is, whether or not it exists."""
    return os.path.join(runs_root(root), run_id)


def step_dir(workspace, step_id, *parts):
    """A step's own directory, created on demand."""
    path = os.path.join(workspace, "steps", safe_id(step_id), *parts)
    os.makedirs(path, exist_ok=True)
    return path


def artifact_path(workspace, name):
    """Where a run-level artifact goes - one not attributable to a single step."""
    directory = os.path.join(workspace, "artifacts")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, os.path.basename(name))


def report_path(workspace, name):
    directory = os.path.join(workspace, "reports")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, os.path.basename(name))


def log_path(workspace, name="cycle.jsonl"):
    directory = os.path.join(workspace, "logs")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, os.path.basename(name))


def relative(workspace, path):
    """``path`` as it should be recorded: relative to ``workspace``.

    Nothing stored in a run record is absolute, so the record still reads
    correctly after the directory is moved to another machine. A path outside
    the workspace comes back untouched - a string of ``..`` would mean less
    than the path it replaced.
    """
    try:
        found = os.path.relpath(os.path.abspath(path), os.path.abspath(workspace))
    except ValueError:                       # a different drive on Windows
        return path
    return path if found.startswith(os.pardir) else found


def existing_runs(root=None):
    """Run ids on disk, newest first. Nothing is parsed; this is a listing.

    Newest first falls out of the timestamp prefix sorting lexically, which is
    why the id is shaped the way it is.
    """
    where = runs_root(root)
    if not os.path.isdir(where):
        return []
    return sorted((name for name in os.listdir(where)
                   if os.path.isdir(os.path.join(where, name))),
                  reverse=True)
