"""Launch projects: which environments belong together, where each one opens,
and the variables its address is written with.

An **environment** here is a place to sign in to: a name, an address (which can
carry ``{{variables}}``) and an **account key** - the users.json ``env`` value its
accounts are filed under. The key is also the prefix of every profile folder
(``<env>-<login>``), so it is what must never change when the address does: the
same accounts on another port of the same application are the same environment
with another ``port``, and keep their signed-in profiles.

A **project** groups environments and decides what variables mean: ``port`` is a
port, ``host`` a string, and each may have a default. Environments give values;
a saved configuration and a single run can give nearer ones (see
:mod:`cms_gui.launchvars`).

Nobody has to set any of this up. Every users.json ``env`` value no environment
here claims is offered anyway, as a *virtual* environment of the reserved
**Workspace** project, named after its alias and opening its origin - the page
works the day it is installed. The first change made to one of them writes it
down here, under the id it already had (``env:<value>``), so a configuration
saved against it before that still finds it after.

The file is the GUI's own (the launcher has never heard of it), with the same
shape and the same load / validate / save / fingerprint contract as
``cycleprojects.json``: a version, a list, unknown keys kept, an atomic write
with one ``.bak``, and a writer that refuses what :func:`validate` complains
about. No Qt here.
"""

import collections
import json
import os
import uuid

from . import launchvars, usersfile

#: Bumped when the shape changes in a way an older build could not read.
VERSION = 1

FILE_NAME = "launch-projects.json"

#: The project every environment belongs to until it is put in another one.
WORKSPACE = "workspace"
WORKSPACE_NAME = "Workspace"

#: How an unclaimed users.json value is named as an environment id.
VIRTUAL_PREFIX = "env:"

PROJECT_KEYS = ("id", "name", "variables", "environments")
ENVIRONMENT_KEYS = ("id", "name", "account_key", "address", "variables")


class LaunchProjectsError(Exception):
    """The projects file could not be read or written."""


class EnvironmentRow(object):
    """One environment as the file holds it; ``extra`` keeps what is not known."""

    def __init__(self, id="", name="", account_key="", address="", variables=None,
                 extra=None):
        self.id = (id or "").strip()
        self.name = (name or "").strip()
        self.account_key = (account_key or "").strip()
        self.address = (address or "").strip()
        self.variables = dict(variables or {})
        self.extra = dict(extra or {})

    @classmethod
    def from_entry(cls, entry):
        if not isinstance(entry, dict):
            raise LaunchProjectsError("an environment has to be an object, not %s"
                                      % type(entry).__name__)
        variables = entry.get("variables") or {}
        if not isinstance(variables, dict):
            raise LaunchProjectsError("the variables of environment %r have to be an "
                                      "object" % entry.get("name", ""))
        return cls(entry.get("id", ""), entry.get("name", ""),
                   entry.get("account_key", ""), entry.get("address", ""), variables,
                   {key: value for key, value in entry.items()
                    if key not in ENVIRONMENT_KEYS})

    def to_entry(self):
        entry = dict(self.extra)
        entry.update({"id": self.id, "name": self.name,
                      "account_key": self.account_key})
        if self.address:
            entry["address"] = self.address
        if self.variables:
            entry["variables"] = dict(self.variables)
        return entry

    def copy(self):
        return EnvironmentRow(self.id, self.name, self.account_key, self.address,
                              dict(self.variables), dict(self.extra))


class ProjectRow(object):
    """One project: its variables and its environments, in their order."""

    def __init__(self, id="", name="", variables=None, environments=None, extra=None):
        self.id = (id or "").strip()
        self.name = (name or "").strip()
        self.variables = dict(variables or {})
        self.environments = list(environments or [])
        self.extra = dict(extra or {})

    @classmethod
    def from_entry(cls, entry):
        if not isinstance(entry, dict):
            raise LaunchProjectsError("a project has to be an object, not %s"
                                      % type(entry).__name__)
        variables = entry.get("variables") or {}
        environments = entry.get("environments") or []
        if not isinstance(variables, dict) or not isinstance(environments, list):
            raise LaunchProjectsError("project %r: variables has to be an object and "
                                      "environments a list" % entry.get("name", ""))
        return cls(entry.get("id", ""), entry.get("name", ""),
                   {name: dict(spec) if isinstance(spec, dict) else spec
                    for name, spec in variables.items()},
                   [EnvironmentRow.from_entry(row) for row in environments],
                   {key: value for key, value in entry.items()
                    if key not in PROJECT_KEYS})

    def to_entry(self):
        entry = dict(self.extra)
        entry.update({"id": self.id, "name": self.name})
        if self.variables:
            entry["variables"] = {name: dict(spec) for name, spec in self.variables.items()}
        entry["environments"] = [row.to_entry() for row in self.environments]
        return entry

    def copy(self):
        return ProjectRow(self.id, self.name,
                          {name: dict(spec) for name, spec in self.variables.items()},
                          [row.copy() for row in self.environments], dict(self.extra))

    def definitions(self):
        """``{name: launchvars.Definition}`` - the ones that can be read."""
        return launchvars.definitions_from(self.variables)[0]


def new_id(prefix):
    """A fresh id: ``prefix`` and eight hex digits."""
    return "%s-%s" % (prefix, uuid.uuid4().hex[:8])


# -- the file -----------------------------------------------------------------
def load(path):
    """Read the file; ``[]`` when it is not there yet (nobody has set anything)."""
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8-sig") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as exc:
        raise LaunchProjectsError("cannot read %s: %s" % (path, exc))
    if not isinstance(document, dict):
        raise LaunchProjectsError("%s has to contain an object" % path)
    version = document.get("version", VERSION)
    if isinstance(version, int) and version > VERSION:
        # Written by a newer build: reading it as this one would drop what it
        # added, and saving would then write that loss to disk.
        raise LaunchProjectsError("%s was written by a newer QAVector (version %d)."
                                  % (path, version))
    rows = document.get("projects")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise LaunchProjectsError("%s: projects has to be a list" % path)
    return [ProjectRow.from_entry(entry) for entry in rows]


def validate(projects):
    """Everything wrong with these projects, as messages. Never raises.

    Strict about what the rest relies on - ids, names, account keys, what
    variables are and the types of the values given to them - and lenient about
    addresses: one may be half-written while its variables are being defined,
    and the page says what is wrong with it where it is shown.
    """
    problems, project_ids, project_names, env_ids = [], set(), set(), set()
    for index, project in enumerate(projects):
        where = project.name or "project %d" % (index + 1)
        if not project.id:
            problems.append("%s has no id." % where)
        elif project.id in project_ids:
            problems.append("%s: two projects share the id %r." % (where, project.id))
        project_ids.add(project.id)
        if not project.name:
            problems.append("%s has no name." % where)
        elif project.name.casefold() in project_names:
            problems.append("%s: two projects share this name." % project.name)
        project_names.add(project.name.casefold())
        definitions, defined = launchvars.definitions_from(project.variables)
        problems.extend("%s: %s" % (where, problem.message) for problem in defined)
        names = set()
        for row in project.environments:
            here = "%s / %s" % (where, row.name or row.id or "an environment")
            if not row.id:
                problems.append("%s has no id." % here)
            elif row.id in env_ids:
                problems.append("%s: two environments share the id %r." % (here, row.id))
            env_ids.add(row.id)
            if not row.name:
                problems.append("%s has no name." % here)
            elif row.name.casefold() in names:
                problems.append("%s: two environments of %s share this name."
                                % (here, where))
            names.add(row.name.casefold())
            if not row.account_key:
                problems.append("%s has no account key (the users.json env its "
                                "accounts are filed under)." % here)
            else:
                problems.extend("%s: %s" % (here, message)
                                for message in usersfile.env_problems([row.account_key]))
            for name, value in row.variables.items():
                definition = definitions.get(name)
                if definition is None:
                    problems.append("%s gives %s a value, but %s does not define it."
                                    % (here, name, where))
                    continue
                if value is None:
                    continue
                _typed, error = launchvars.coerce(definition, value)
                if error:
                    problems.append("%s: %s %s." % (here, name, error))
    return problems


def save(path, projects):
    """Write the projects back, atomically, keeping one backup."""
    problems = validate(projects)
    if problems:
        raise LaunchProjectsError("\n".join(problems))
    if not path:
        raise LaunchProjectsError("No launch projects path configured.")
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
        raise LaunchProjectsError("cannot write %s: %s" % (path, exc))
    return path


def fingerprint(path):
    """(mtime, size) - enough to notice another window wrote the file."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def default_path():
    """Beside ``services.json`` in the user's own data root, never in a checkout."""
    from . import servicesfile
    return os.path.join(os.path.dirname(servicesfile.default_path()), FILE_NAME)


def resolve_path(configured):
    """The path actually in force: what Settings says, or the default."""
    configured = (configured or "").strip()
    return os.path.expanduser(configured) if configured else default_path()


# -- environments as the page offers them -------------------------------------
#: One environment as offered: the file's, or a virtual one for an unclaimed
#: users.json value. ``origin`` and ``accounts`` come from the inventory.
Environment = collections.namedtuple(
    "Environment",
    "id project_id name account_key address variables origin accounts virtual")


def catalog(projects, inventory=None):
    """Every environment there is to launch, in order: the file's, then virtual.

    A users.json value that no environment of the file claims becomes a virtual
    environment of the Workspace project, so nothing the launcher knows about is
    ever missing from the page just because nobody has organised it.
    """
    known = {}
    for env in (inventory.envs if inventory else []):
        if env.get("value"):
            known[env["value"]] = env
    found, claimed = [], set()
    for project in projects:
        for row in project.environments:
            facts = known.get(row.account_key) or {}
            found.append(Environment(row.id, project.id, row.name, row.account_key,
                                     row.address, dict(row.variables),
                                     facts.get("origin") or "",
                                     int(facts.get("count") or 0), False))
            claimed.add(row.account_key)
    for value, env in known.items():
        if value not in claimed:
            found.append(Environment(VIRTUAL_PREFIX + value, WORKSPACE,
                                     env.get("alias") or value, value, "", {},
                                     env.get("origin") or "",
                                     int(env.get("count") or 0), True))
    return found


def find(environments, env_id):
    """The environment ``env_id`` names, or None.

    A virtual id carries the users.json value it was made from, so one saved
    before that value was put in an environment of its own still finds it -
    as long as exactly one environment holds that value.
    """
    for env in environments:
        if env.id == env_id:
            return env
    if env_id and env_id.startswith(VIRTUAL_PREFIX):
        value = env_id[len(VIRTUAL_PREFIX):]
        holders = [env for env in environments if env.account_key == value]
        if len(holders) == 1:
            return holders[0]
    return None


def project_names(projects, environments):
    """``[(id, name)]`` of the projects to offer: the file's, Workspace last.

    Workspace is offered whenever it has an environment, and always when there
    is no other project - it is where everything starts.
    """
    names = [(project.id, project.name) for project in projects
             if project.id != WORKSPACE]
    workspace = next((project for project in projects if project.id == WORKSPACE), None)
    if not names or any(env.project_id == WORKSPACE for env in environments):
        names.append((WORKSPACE, workspace.name if workspace else WORKSPACE_NAME))
    return names


# -- changing it --------------------------------------------------------------
def project_row(projects, project_id):
    """The file's row for ``project_id`` (Workspace is written down when asked for)."""
    for project in projects:
        if project.id == project_id:
            return project
    if project_id == WORKSPACE:
        project = ProjectRow(WORKSPACE, WORKSPACE_NAME)
        projects.append(project)
        return project
    raise LaunchProjectsError("there is no project %r" % project_id)


def environment_row(projects, environment):
    """The file's row for ``environment``, written down first if it is virtual."""
    for project in projects:
        for row in project.environments:
            if row.id == environment.id:
                return row
    project = project_row(projects, environment.project_id or WORKSPACE)
    row = EnvironmentRow(environment.id, environment.name, environment.account_key,
                         environment.address, environment.variables)
    project.environments.append(row)
    return row


def define(projects, project_id, name, kind, default=None):
    """Define (or redefine) ``name`` in a project; returns its Definition."""
    if not launchvars.valid_name(name):
        raise LaunchProjectsError("\"%s\" cannot be a variable's name." % name)
    if kind not in launchvars.TYPES:
        raise LaunchProjectsError("%r is not a variable type." % kind)
    project = project_row(projects, project_id)
    spec = dict(project.variables.get(name) or {})
    spec["type"] = kind
    if default is None:
        spec.pop("default", None)
    else:
        spec["default"] = default
    project.variables[name] = spec
    return project.definitions().get(name)


class Registry(object):
    """The file as one window holds it: read forgivingly, changed by re-reading.

    Every change goes through :meth:`update`, which reads the file afresh,
    changes that and writes it - never a copy taken at startup, so one window's
    save cannot quietly undo another's. While the file cannot be read, nothing
    is written over it.
    """

    def __init__(self, path):
        self.path = path
        self.projects = []
        self.error = ""
        self.reload()

    def reload(self):
        try:
            self.projects = load(self.path)
            self.error = ""
        except LaunchProjectsError as exc:
            self.projects = []
            self.error = str(exc)

    def update(self, change):
        """Apply ``change(projects)`` to the file as it is now, and save it.

        Returns what ``change`` returned. Raises LaunchProjectsError - with the
        file untouched - when it cannot be read, or the result would not be one
        :func:`validate` accepts.
        """
        self.reload()
        if self.error:
            raise LaunchProjectsError("%s\nNothing was changed: fix the file first."
                                      % self.error)
        projects = [project.copy() for project in self.projects]
        result = change(projects)
        save(self.path, projects)
        self.projects = projects
        return result

    def environments(self, inventory=None):
        return catalog(self.projects, inventory)

    def project(self, project_id):
        for project in self.projects:
            if project.id == project_id:
                return project
        if project_id == WORKSPACE:
            return ProjectRow(WORKSPACE, WORKSPACE_NAME)
        return None

    def address_for_value(self, value, inventory=None):
        """The address of the one environment filed under ``value``; "" otherwise.

        For the Command page, which names an environment by its users.json
        value: when two environments share accounts there is no telling which
        address was meant, and the launcher's own origin is the honest answer.
        """
        holders = [env for env in self.environments(inventory)
                   if env.account_key == value]
        return holders[0].address if len(holders) == 1 else ""
