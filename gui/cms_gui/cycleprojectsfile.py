"""The Cycles section's own list of projects: read, checked, written.

A **project** here groups cycles. It is deliberately *not* the project on the
Services & Logs page, and this is its own file rather than a corner of
``services.json``, because the two answer different questions: a project can
have half a dozen cycles and no services configured at all, and making the
Cycles page depend on the services file would make that ordinary case look like
something half-finished. One list doing both jobs would also mean deleting a
services project silently took its cycles with it.

So the shape is the same as ``services.json`` - a version, a list, one row per
project - and the module is the same shape as ``servicesfile``: **load,
validate, save, fingerprint**, with the writer refusing anything ``validate``
complains about. Where it goes is a setting, like the other two files, because
in a source checkout the default would otherwise put it among the code.

Which cycles belong to a project is **not** recorded here. A cycle declares its
own ``project:`` in its own file, so the answer travels with the cycle: copy a
cycle file to another machine and it still knows where it belongs. A cycle
naming a project this file has never heard of is not an error either - it shows
as unassigned, the way a log source with an unknown stack does on the Services
page.
"""

import json
import os

#: Bumped when the shape changes in a way an older build could not read. Present
#: from the first version so there is something to compare against.
VERSION = 1

FILE_NAME = "cycleprojects.json"

#: What a row may carry. Anything else is kept but not understood - see
#: :meth:`ProjectRow.from_entry`.
KEYS = ("name", "description", "colour", "added")


class CycleProjectsFileError(Exception):
    """The projects file could not be read or written."""


class ProjectRow(object):
    """One project: a name, and whatever else is worth saying about it.

    ``extra`` keeps anything this version does not recognise, so a file written
    by a newer build and opened by an older one comes back out whole rather than
    losing the fields it did not know about.
    """

    def __init__(self, name="", description="", colour="", added="", extra=None):
        self.name = (name or "").strip()
        self.description = (description or "").strip()
        self.colour = (colour or "").strip()
        self.added = (added or "").strip()
        self.extra = dict(extra or {})

    @classmethod
    def from_entry(cls, entry):
        if not isinstance(entry, dict):
            raise CycleProjectsFileError("a project has to be an object, not %s"
                                         % type(entry).__name__)
        return cls(name=entry.get("name", ""),
                   description=entry.get("description", ""),
                   colour=entry.get("colour", ""),
                   added=entry.get("added", ""),
                   extra={key: value for key, value in entry.items()
                          if key not in KEYS})

    def to_entry(self):
        entry = dict(self.extra)
        entry["name"] = self.name
        for key in ("description", "colour", "added"):
            value = getattr(self, key)
            if value:
                entry[key] = value
        return entry

    def copy(self):
        return ProjectRow(self.name, self.description, self.colour, self.added,
                          dict(self.extra))

    def __repr__(self):
        return "ProjectRow(%r)" % self.name


def load(path):
    """Read the file. Returns ``[]`` when it is not there yet.

    A missing file is the ordinary state before anybody has made a project, so
    it is not an error. A file that exists and cannot be understood *is* one:
    carrying on would mean writing over it with an empty list.
    """
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8-sig") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as exc:
        raise CycleProjectsFileError("cannot read %s: %s" % (path, exc))
    if not isinstance(document, dict):
        raise CycleProjectsFileError("%s has to contain an object" % path)

    rows = document.get("projects")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise CycleProjectsFileError("%s: projects has to be a list" % path)
    return [ProjectRow.from_entry(entry) for entry in rows]


def validate(projects):
    """Everything wrong with these rows, as messages. Never raises."""
    problems, seen = [], set()
    for index, project in enumerate(projects):
        where = project.name or "project %d" % (index + 1)
        if not project.name:
            problems.append("%s has no name." % where)
        key = project.name.casefold()
        if key and key in seen:
            problems.append("%s: two projects share this name, so a cycle "
                            "naming it could mean either." % project.name)
        seen.add(key)
    return problems


def save(path, projects):
    """Write the projects back, atomically, keeping one backup.

    Refuses anything :func:`validate` complains about, for the reason
    ``servicesfile.save`` does: a file that cannot be read back is worse than
    the edit not being saved, and the caller can still show the problems.
    """
    problems = validate(projects)
    if problems:
        raise CycleProjectsFileError("\n".join(problems))
    if not path:
        raise CycleProjectsFileError("No projects path configured.")

    payload = {"version": VERSION,
               "projects": [project.to_entry() for project in projects]}
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    temp = os.path.join(directory, ".%s.tmp" % os.path.basename(path))
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.path.exists(path):
            backup = path + ".bak"
            try:
                if os.path.exists(backup):
                    os.remove(backup)
                os.replace(path, backup)
            except OSError:
                pass    # a missing backup must never block the save itself
        os.replace(temp, path)
    except OSError as exc:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise CycleProjectsFileError("cannot write %s: %s" % (path, exc))
    return path


def fingerprint(path):
    """What the file looked like when it was read, for the overwrite check.

    ``(size, mtime)`` rather than a hash: it is compared against itself a
    moment later, and reading a file twice to notice somebody else saved over
    it is a cost for nothing. None when there is no file.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_size, int(stat.st_mtime))


def default_path():
    """Where the file goes when nobody has said otherwise.

    Beside ``services.json`` in the user's own data root - the GUI's files live
    together, and none of them belongs in a source checkout.
    """
    from . import servicesfile
    return os.path.join(os.path.dirname(servicesfile.default_path()), FILE_NAME)


def resolve_path(configured):
    """The path actually in force: what Settings says, or the default."""
    configured = (configured or "").strip()
    return os.path.expanduser(configured) if configured else default_path()


def names(projects):
    """Just the names, in file order - what a picker offers."""
    return [project.name for project in projects if project.name]


def find(projects, name):
    """The row with this name, or None. Case-insensitive, like the check is."""
    wanted = (name or "").strip().casefold()
    for project in projects:
        if project.name.casefold() == wanted:
            return project
    return None
