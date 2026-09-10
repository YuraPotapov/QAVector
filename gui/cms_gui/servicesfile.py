"""Reading, validating and writing ``services.json`` - the projects and their runners.

A **project** is a name, the directory its parts live in, and the list of
services that make it up. It is the one grouping this application did not have -
environments come from ``users.json`` and describe *where a run points*, which is a
different question from *what is running on this machine*. A local app server,
its database container and its log all belong to one project; two of those three
had nowhere to say so.

The file is the GUI's own - the launcher neither reads nor needs it - but it is
kept beside ``logsources.json`` in the data root rather than in the GUI's private
directory, because it is configuration a person writes and would look for next to
the rest of theirs.

Everything about its handling is deliberately the same as
:mod:`~cms_gui.logsourcesfile`: unknown keys survive a round trip, ``validate``
*returns* its problems rather than raising so a half-typed row can sit in the
editor, and ``save`` is atomic with a one-slot ``.bak``. A settings dict belongs to
its runner type and is passed through untouched, so a type that grows a field does
not need this module to know.
"""

import json
import os

from . import core
from . import criteria as criteria_mod
from . import runnertypes
# One clock for both files. The Services & Logs page shows rows from this file
# and from logsources.json in the same block and sorts them the same way, so the
# stamp they carry has to be written the same way too.
from .logsourcesfile import stamp

#: The schema version written out. Nothing reads it yet; it is here so that a
#: future change has somewhere to look rather than having to guess from shape.
VERSION = 1

# Keys this module understands. Anything else in an entry is preserved as-is.
PROJECT_KEYS = ("name", "dir", "expanded", "runners", "added")
RUNNER_KEYS = ("name", "type", "detach", "settings", "depends", "criteria",
               "added")


class ServicesFileError(Exception):
    pass


class RunnerRow:
    """One service, plus whatever unknown keys came with it."""

    def __init__(self, name="", type="", detach=None, settings=None,
                 depends=(), criteria=(), extra=None, added=""):
        self.name = name
        self.type = type or ""
        # When this row was created, or "" for one written before the editor
        # stamped them. Read by the page's ordering and by nothing else - what
        # starts before what is decided by `depends`, never by this.
        self.added = added or ""
        runner = runnertypes.get(self.type)
        # None means "whatever this kind is by default", so a row written before a
        # type existed, or by hand, still opens with the sane answer rather than
        # with False - which for a container would be a lie.
        self.detach = (bool(runner.detach_default) if runner else False) \
            if detach is None else bool(detach)
        self.settings = dict(settings or {})
        # Other services in the same project that have to be up first. Names, not
        # indices: a name is what the person typed and what survives a row being
        # moved, and the ordering is a property of the project rather than of the
        # list it happens to be written in.
        self.depends = [d for d in depends if isinstance(d, str) and d.strip()]
        # What its own log says about where it has got to. Shown beside the
        # service and nothing more - see cms_gui.criteria.
        self.criteria = list(criteria)
        self.extra = dict(extra or {})

    @property
    def runner_type(self):
        """The :class:`runnertypes.RunnerType`, or None if the file names one we
        do not have. The page shows such a row, greyed - deleting somebody's
        configuration because this build is older than it would be worse."""
        return runnertypes.get(self.type)

    @classmethod
    def from_entry(cls, entry):
        if not isinstance(entry, dict):
            raise ServicesFileError("every runner must be a JSON object, got %r"
                                    % (entry,))
        settings = entry.get("settings", {})
        if not isinstance(settings, dict):
            raise ServicesFileError("runner %r: 'settings' must be a JSON object."
                                    % (entry.get("name", "?"),))
        depends = entry.get("depends", [])
        if isinstance(depends, str):
            depends = [depends]
        if not isinstance(depends, (list, tuple)):
            raise ServicesFileError("runner %r: 'depends' must be a JSON array of "
                                    "service names." % (entry.get("name", "?"),))
        rules = entry.get("criteria", [])
        if not isinstance(rules, list):
            raise ServicesFileError("runner %r: 'criteria' must be a JSON array."
                                    % (entry.get("name", "?"),))
        try:
            parsed = [criteria_mod.CriterionRow.from_entry(one) for one in rules]
        except ValueError as exc:
            raise ServicesFileError("runner %r: %s"
                                    % (entry.get("name", "?"), exc))
        return cls(name=entry.get("name", "") or "",
                   type=entry.get("type", "") or "",
                   detach=entry.get("detach"),
                   settings=settings,
                   depends=depends,
                   criteria=parsed,
                   added=entry.get("added", "") or "",
                   extra={k: v for k, v in entry.items() if k not in RUNNER_KEYS})

    def to_entry(self):
        entry = dict(self.extra)
        entry["name"] = self.name
        entry["type"] = self.type
        entry["detach"] = bool(self.detach)
        entry["settings"] = dict(self.settings)
        # Only when there are any: a service that waits for nothing should read
        # in the file the same as it did before this existed.
        if self.depends:
            entry["depends"] = list(self.depends)
        if self.criteria:
            entry["criteria"] = [one.to_entry() for one in self.criteria]
        # Only when the row has one, for the same reason as the two above.
        if self.added:
            entry["added"] = self.added
        return entry

    def summary(self):
        """What the page's Config column shows: the command, near enough."""
        runner = self.runner_type
        if runner is None:
            return ""
        try:
            return runner.summary(self.settings)
        except ValueError:
            # An unlexable command line is a problem validate() already reports;
            # it must not also stop the row from being drawn.
            return ""

    def copy(self):
        return RunnerRow(self.name, self.type, self.detach, dict(self.settings),
                         list(self.depends),
                         [one.copy() for one in self.criteria], dict(self.extra),
                         self.added)


class ProjectRow:
    """One project: a name, where it lives, and the services in it."""

    def __init__(self, name="", dir="", expanded=True, runners=(), extra=None,
                 added=""):
        self.name = name
        self.dir = dir
        # The fold state a project opens with when the GUI has never been told
        # otherwise. What it is folded to *now* is a view preference and lives in
        # QSettings - see cms_gui.settings.folds.
        self.expanded = bool(expanded)
        #: When this project was created; "" for one that predates the stamp.
        self.added = added or ""
        self.runners = list(runners)
        self.extra = dict(extra or {})

    @classmethod
    def from_entry(cls, entry):
        if not isinstance(entry, dict):
            raise ServicesFileError("every project must be a JSON object, got %r"
                                    % (entry,))
        runners = entry.get("runners", [])
        if not isinstance(runners, list):
            raise ServicesFileError("project %r: 'runners' must be a JSON array."
                                    % (entry.get("name", "?"),))
        return cls(name=entry.get("name", "") or "",
                   dir=entry.get("dir", "") or "",
                   # Default open: a project whose block is shut says nothing, and
                   # a file written by hand should not have to opt in to being read.
                   expanded=entry.get("expanded", True),
                   runners=[RunnerRow.from_entry(r) for r in runners],
                   added=entry.get("added", "") or "",
                   extra={k: v for k, v in entry.items() if k not in PROJECT_KEYS})

    def to_entry(self):
        entry = dict(self.extra)
        entry["name"] = self.name
        entry["dir"] = self.dir
        entry["expanded"] = bool(self.expanded)
        if self.added:
            entry["added"] = self.added
        entry["runners"] = [runner.to_entry() for runner in self.runners]
        return entry

    def runner(self, name):
        for row in self.runners:
            if row.name == name:
                return row
        return None

    def start_order(self):
        """The runners, ordered so nothing is started before what it waits for.

        A depth-first walk rather than a full topological sort, because the graph
        is small and this keeps the person's own order everywhere it does not
        matter: a project whose services depend on nothing starts them top to
        bottom, which is what they wrote down. A cycle is refused by
        :func:`validate`, and is broken here rather than recursed into, so a file
        edited by hand cannot hang the page.
        """
        ordered, seen, visiting = [], set(), set()

        def visit(row):
            if row.name in seen or row.name in visiting:
                return
            visiting.add(row.name)
            for name in row.depends:
                dependency = self.runner(name)
                if dependency is not None:
                    visit(dependency)
            visiting.discard(row.name)
            seen.add(row.name)
            ordered.append(row)

        for row in self.runners:
            visit(row)
        return ordered

    def needed_by(self, name):
        """Every runner that waits, directly or not, on this one."""
        found, frontier = set(), [name]
        while frontier:
            current = frontier.pop()
            for row in self.runners:
                if row.name in found or current not in row.depends:
                    continue
                found.add(row.name)
                frontier.append(row.name)
        return [row for row in self.runners if row.name in found]

    def copy(self):
        return ProjectRow(self.name, self.dir, self.expanded,
                          [r.copy() for r in self.runners], dict(self.extra),
                          self.added)


def load(path):
    """Read ``services.json`` into rows. Missing file = nothing (not an error)."""
    if not path or not os.path.exists(path):
        return []
    try:
        # utf-8-sig for the same reason logsourcesfile uses it: a file written by
        # a Windows shell carries a byte-order mark and strict utf-8 reads it as
        # content.
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except ValueError as exc:
        raise ServicesFileError("%s is not valid JSON: %s" % (path, exc))
    except OSError as exc:
        raise ServicesFileError("cannot read %s: %s" % (path, exc))
    if not isinstance(data, dict):
        raise ServicesFileError("%s must be a JSON object with 'projects'." % path)
    projects = data.get("projects", [])
    if not isinstance(projects, list):
        raise ServicesFileError("%s: 'projects' must be a JSON array." % path)
    return [ProjectRow.from_entry(entry) for entry in projects]


def validate(projects):
    """Every problem that would make this file unusable, as messages.

    Returned rather than raised, so the editor shows them all at once and a row
    can be half-typed without an exception.
    """
    problems = []
    seen_projects = {}
    for index, project in enumerate(projects, start=1):
        where = "project %d" % index
        if project.name:
            where += " (%s)" % project.name
        if not project.name.strip():
            problems.append("%s: a name is required." % where)
        elif project.name in seen_projects:
            problems.append("%s: duplicates project %d - names must be unique."
                            % (where, seen_projects[project.name]))
        else:
            seen_projects[project.name] = index
        directory = (project.dir or "").strip()
        # A directory that is not there yet is fine - a project can be configured
        # before the checkout exists. One that is a *file* never becomes right.
        if directory and os.path.isfile(os.path.expanduser(directory)):
            problems.append("%s: %s is a file, not a directory."
                            % (where, directory))

        seen_runners = {}
        for position, runner in enumerate(project.runners, start=1):
            spot = "%s, runner %d" % (where, position)
            if runner.name:
                spot += " (%s)" % runner.name
            if not runner.name.strip():
                problems.append("%s: a name is required." % spot)
            elif runner.name in seen_runners:
                problems.append("%s: duplicates runner %d - names must be unique "
                                "within a project."
                                % (spot, seen_runners[runner.name]))
            else:
                seen_runners[runner.name] = position
            seen_criteria = {}
            for position, criterion in enumerate(runner.criteria, start=1):
                mark = "%s, criterion %d" % (spot, position)
                if criterion.name:
                    mark += " (%s)" % criterion.name
                    if criterion.name in seen_criteria:
                        problems.append("%s: duplicates criterion %d - names must "
                                        "be unique within a service."
                                        % (mark, seen_criteria[criterion.name]))
                    else:
                        seen_criteria[criterion.name] = position
                problems.extend(criteria_mod.problems(criterion, mark))

            for name in runner.depends:
                if name == runner.name:
                    problems.append("%s: it cannot wait for itself." % spot)
                elif project.runner(name) is None:
                    problems.append("%s: waits for %r, which is not a service in "
                                    "this project." % (spot, name))
            kind = runner.runner_type
            if kind is None:
                problems.append("%s: unknown type %r. Known: %s."
                                % (spot, runner.type,
                                   ", ".join(t.id for t in runnertypes.TYPES)))
                continue
            problems.extend("%s: %s" % (spot, message)
                            for message in kind.problems(runner.settings))

        # A ring of services each waiting for the next can never start, and the
        # walk that orders them would recurse forever if it trusted the file.
        for cycle in _cycles(project):
            if len(cycle) == 2 and cycle[0] == cycle[1]:
                continue          # a self-loop, already reported in its own words
            # " -> " rather than an arrow glyph: a literal symbol renders as a
            # box in DejaVu and as its ASCII stand-in on Windows, which is the
            # rule test_no_module_hard_codes_a_symbol exists to hold.
            problems.append("%s: %s wait for each other in a loop, so none of "
                            "them could ever start."
                            % (where, " -> ".join(cycle)))
    return problems


def _cycles(project):
    """Every dependency loop in a project, each as the names going round it."""
    found, seen = [], set()
    for start in project.runners:
        if start.name in seen:
            continue
        stack, on_stack = [], set()

        def walk(row):
            if row.name in on_stack:
                # Report from where the loop closes, not from where we entered.
                found.append(stack[stack.index(row.name):] + [row.name])
                return True
            if row.name in seen:
                return False
            seen.add(row.name)
            stack.append(row.name)
            on_stack.add(row.name)
            for name in row.depends:
                dependency = project.runner(name)
                if dependency is not None and walk(dependency):
                    break
            stack.pop()
            on_stack.discard(row.name)
            return False

        walk(start)
    return found


def save(path, projects):
    """Write the projects back, atomically, keeping one backup of what was there."""
    problems = validate(projects)
    if problems:
        raise ServicesFileError("\n".join(problems))
    if not path:
        raise ServicesFileError("No services path configured.")
    payload = {"version": VERSION,
               "projects": [project.to_entry() for project in projects]}
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    temp = os.path.join(directory, ".%s.tmp" % os.path.basename(path))
    try:
        with open(temp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        if os.path.exists(path):
            backup = path + ".bak"
            try:
                if os.path.exists(backup):
                    os.remove(backup)
                os.replace(path, backup)
            except OSError:
                pass          # a missing backup must not block the save itself
        os.replace(temp, path)
    except OSError as exc:
        raise ServicesFileError("cannot write %s: %s" % (path, exc))
    finally:
        if os.path.exists(temp):
            try:
                os.remove(temp)
            except OSError:
                pass
    return path


def fingerprint(path):
    """(mtime, size) - enough to notice the file changed underneath the editor."""
    try:
        stat = os.stat(path)
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


#: The user's own folder. cms_gui.core keeps the GUI's copy of the core's name
#: for it, and resolves it to the old name's folder until that has moved.
USER_DIR_NAME = core.USER_DIR_NAME


def default_path():
    """Where ``services.json`` goes when nobody has said otherwise.

    Always under the user's own directory, never beside ``logsources.json``.
    That was the first arrangement and it is wrong in the case that matters most:
    from a source checkout the launcher's config path *is* the checkout, so the
    GUI's own file landed in somebody's repository - which is how it ended up
    needing a .gitignore entry to stay out of a commit.
    """
    return os.path.join(core.home_folder(), "services.json")


def legacy_path(log_sources_path):
    """Where it used to go: beside the launcher's own config.

    Still read when the default is not there, so an upgrade does not look like
    the projects were lost.
    """
    if not log_sources_path:
        return ""
    return os.path.join(os.path.dirname(os.path.abspath(log_sources_path)),
                        "services.json")


def resolve_path(configured, log_sources_path):
    """(path to read, path to write). They differ only while migrating.

    Settings wins; otherwise the default. A file at the old sibling location is
    read when there is nothing at the new one, and the next Save moves it - the
    old one is left alone rather than deleted, because it is still somebody's
    file and nothing here has to destroy it to be correct.
    """
    target = (configured or "").strip() or default_path()
    target = os.path.expanduser(target)
    if os.path.exists(target):
        return target, target
    legacy = legacy_path(log_sources_path)
    if legacy and os.path.abspath(legacy) != os.path.abspath(target) \
            and os.path.exists(legacy):
        return legacy, target
    return target, target


def path_beside(log_sources_path):
    """Deprecated spelling of :func:`legacy_path`, kept for one release."""
    return legacy_path(log_sources_path)
