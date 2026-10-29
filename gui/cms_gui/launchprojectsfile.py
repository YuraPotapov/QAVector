"""Launch projects: environments, the variables each one holds, and the
folders they are kept in.

An **environment** is what Postman calls one: a name and its variables. One of
them, ``url``, is where its windows open, and it may be written with the others
(``http://localhost:{{port}}/web`` with ``port`` = ``8069``). Variables have no
types and no defaults from anywhere else - each is a name and the text it
stands for.

Its accounts are users.json rows, filed under an **account key**: the ``env``
value those rows carry. The key is also the prefix of every profile folder
(``<env>-<login>``), so it is what must never change when the name or the
address does - another port of the same application is the same environment
with another ``port``, and keeps its signed-in profiles. Nobody types it: an
environment that came from users.json keeps the value it had, a new one is given
one made from its name (:func:`new_key`), and neither is shown outside
developer wording.

A **project** is a folder of environments and nothing more.

Nobody has to set any of this up. Every users.json ``env`` value no environment
here claims is offered anyway, as a *virtual* environment of the reserved
**Workspace** project, named after its alias and opening its origin - the page
works the day it is installed. The first change made to one of them writes it
down here, under the id it already had (``env:<value>``), so a configuration
saved against it before that still finds it after.

Version 1 of the file had typed variables defined by the project, with
defaults, and an ``address`` beside them; it is read as version 2 - the address
becomes ``url`` and each environment takes the values its address used - and the
first save keeps the file it was as ``launch-projects.json.v1``.

The file is the GUI's own (the launcher has never heard of it), with the same
shape and the same load / validate / save / fingerprint contract as
``cycleprojects.json``: a version, a list, unknown keys kept, an atomic write
with one ``.bak``, and a writer that refuses what :func:`validate` complains
about. No Qt here.
"""

import collections
import hashlib
import json
import os
import re
import shutil
import unicodedata
import uuid

from . import launchvars, usersfile

#: Bumped when the shape changes in a way an older build could not read.
VERSION = 2

FILE_NAME = "launch-projects.json"

#: The project every environment belongs to until it is put in another one.
WORKSPACE = "workspace"
WORKSPACE_NAME = "Workspace"

#: How an unclaimed users.json value is named as an environment id.
VIRTUAL_PREFIX = "env:"

#: The variable that says where an environment's windows open.
URL = "url"

#: The label of an environment's values where a variable says where it came from.
ENVIRONMENT = "environment"

PROJECT_KEYS = ("id", "name", "environments")
ENVIRONMENT_KEYS = ("id", "name", "account_key", "variables")


class LaunchProjectsError(Exception):
    """The projects file could not be read or written."""


def as_text(value):
    """A variable's value as the text it stands for."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class EnvironmentRow(object):
    """One environment as the file holds it; ``extra`` keeps what is not known.

    ``address`` is its ``url`` variable, kept with the others; it can still be
    given and read on its own, since that is the one most things want.
    """

    def __init__(self, id="", name="", account_key="", address="", variables=None,
                 extra=None):
        self.id = (id or "").strip()
        self.name = (name or "").strip()
        self.account_key = (account_key or "").strip()
        self.variables = {str(name): as_text(value)
                          for name, value in (variables or {}).items()
                          if value is not None}
        if (address or "").strip():
            self.address = address
        self.extra = dict(extra or {})

    @property
    def address(self):
        return self.variables.get(URL, "")

    @address.setter
    def address(self, value):
        text = (value or "").strip()
        if text:
            # url first, wherever it was given: it is the one read first.
            rest = {name: value for name, value in self.variables.items() if name != URL}
            self.variables = dict({URL: text}, **rest)
        else:
            self.variables.pop(URL, None)

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
                   entry.get("account_key", ""), "", variables,
                   {key: value for key, value in entry.items()
                    if key not in ENVIRONMENT_KEYS})

    def to_entry(self):
        entry = dict(self.extra)
        entry.update({"id": self.id, "name": self.name,
                      "account_key": self.account_key})
        if self.variables:
            entry["variables"] = dict(self.variables)
        return entry

    def copy(self):
        return EnvironmentRow(self.id, self.name, self.account_key, "",
                              dict(self.variables), dict(self.extra))


class ProjectRow(object):
    """One project: a folder of environments, in their order."""

    def __init__(self, id="", name="", environments=None, extra=None):
        self.id = (id or "").strip()
        self.name = (name or "").strip()
        self.environments = list(environments or [])
        self.extra = dict(extra or {})

    @classmethod
    def from_entry(cls, entry):
        if not isinstance(entry, dict):
            raise LaunchProjectsError("a project has to be an object, not %s"
                                      % type(entry).__name__)
        environments = entry.get("environments") or []
        if not isinstance(environments, list):
            raise LaunchProjectsError("project %r: environments has to be a list"
                                      % entry.get("name", ""))
        return cls(entry.get("id", ""), entry.get("name", ""),
                   [EnvironmentRow.from_entry(row) for row in environments],
                   {key: value for key, value in entry.items()
                    if key not in PROJECT_KEYS})

    def to_entry(self):
        entry = dict(self.extra)
        entry.update({"id": self.id, "name": self.name})
        entry["environments"] = [row.to_entry() for row in self.environments]
        return entry

    def copy(self):
        return ProjectRow(self.id, self.name,
                          [row.copy() for row in self.environments], dict(self.extra))


def new_id(prefix):
    """A fresh id: ``prefix`` and eight hex digits."""
    return "%s-%s" % (prefix, uuid.uuid4().hex[:8])


def new_key(name, taken=()):
    """The account key of a new environment, made from its name - once.

    Lower-case ASCII words joined by ``-`` (``Helpdesk Local`` ->
    ``helpdesk-local``), so the profile folders it names read as what they are,
    and with a number after it when the plain one is taken or would leave the
    launcher unable to tell two environments apart. Never derived again: a
    renamed environment keeps the key it was given, and so its profiles.
    """
    plain = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore")
    base = re.sub(r"[^a-z0-9]+", "-", plain.decode("ascii").lower()).strip("-") or "env"
    taken = [value for value in taken if value]
    folded = {value.casefold() for value in taken}
    before = len(usersfile.env_problems(taken))
    candidate, number = base, 1
    while candidate.casefold() in folded \
            or len(usersfile.env_problems(taken + [candidate])) > before:
        number += 1
        candidate = "%s-%d" % (base, number)
    return candidate


def scope(environment_values=None, nearer=()):
    """``launchvars.Scope`` for an address: the environment's values, then the
    ``nearer`` layers (a configuration's, this run's).

    A variable is any name one of them gives a value - every one of them text,
    as in Postman. ``url`` is the address itself, never a value in it.
    """
    layers = [launchvars.Layer(ENVIRONMENT, dict(environment_values or {}))]
    layers.extend(nearer)
    names = []
    for layer in layers:
        for name, value in (layer.values or {}).items():
            if value is not None and name != URL and name not in names \
                    and launchvars.valid_name(name):
                names.append(name)
    definitions = {name: launchvars.Definition(name, "string", None, True, ())
                   for name in names}
    return launchvars.Scope(definitions, layers)


def upgrade_v1(rows):
    """Version 1 projects as version 2: ``[ProjectRow]``.

    The address becomes ``url``. Each environment keeps the values it gave and
    takes, from its project's defaults, the ones its address used and it did
    not give - so it opens where it did. A project's variable nobody's address
    used is let go with the version-1 file kept beside.
    """
    projects = []
    for entry in rows:
        if not isinstance(entry, dict):
            raise LaunchProjectsError("a project has to be an object, not %s"
                                      % type(entry).__name__)
        definitions = launchvars.definitions_from(entry.get("variables") or {})[0] \
            if isinstance(entry.get("variables") or {}, dict) else {}
        environments = []
        for env in entry.get("environments") or []:
            if not isinstance(env, dict):
                raise LaunchProjectsError("an environment has to be an object, not %s"
                                          % type(env).__name__)
            address = env.get("address") or ""
            given = env.get("variables") or {}
            if not isinstance(given, dict):
                raise LaunchProjectsError("the variables of environment %r have to be "
                                          "an object" % env.get("name", ""))
            values = {}
            for name in launchvars.references(address) + list(given):
                value = given.get(name)
                definition = definitions.get(name)
                if value is None and definition is not None:
                    value = definition.default
                if value is not None and name != URL:
                    values[name] = launchvars.text_of(definition, value) \
                        if definition is not None else as_text(value)
            environments.append(EnvironmentRow(
                env.get("id", ""), env.get("name", ""), env.get("account_key", ""),
                address, values,
                {key: value for key, value in env.items()
                 if key not in ENVIRONMENT_KEYS + ("address",)}))
        projects.append(ProjectRow(
            entry.get("id", ""), entry.get("name", ""), environments,
            {key: value for key, value in entry.items()
             if key not in PROJECT_KEYS + ("variables",)}))
    return projects


def settle_ids(projects):
    """Give an environment that left its users.json value an id of its own.

    A virtual environment is written down under the id it had, ``env:<value>``,
    so the configurations made on it stay with it. Given another account key
    afterwards, it no longer holds ``<value>`` - which is then unclaimed again
    and comes back as a virtual environment under that very id: two
    environments with one id, and choosing either opened the other. Such a row
    takes an id made from what it was and what it is now, the same on every
    reading, so a file written before this was caught is mended the same way
    whether or not it is saved again. The configurations go with the id, back
    to the accounts they were made for.
    """
    for project in projects:
        for row in project.environments:
            if row.id.startswith(VIRTUAL_PREFIX) \
                    and row.id[len(VIRTUAL_PREFIX):] != row.account_key:
                digest = hashlib.sha1(("%s|%s" % (row.id, row.account_key))
                                      .encode("utf-8")).hexdigest()
                row.id = "e-%s" % digest[:8]
    return projects


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
    if isinstance(version, int) and version < 2:
        return settle_ids(upgrade_v1(rows))
    return settle_ids([ProjectRow.from_entry(entry) for entry in rows])


def validate(projects):
    """Everything wrong with these projects, as messages. Never raises.

    Strict about what the rest relies on - ids, names, account keys and the
    names of variables - and lenient about values: an address may be
    half-written while the variables it uses are being added, and the page says
    what is wrong with it where it is shown.
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
            for name in row.variables:
                if name != URL and not launchvars.valid_name(name):
                    problems.append(
                        "%s: \"%s\" cannot be a variable's name - letters, digits and "
                        "_, not starting with a digit, and not one of %s."
                        % (here, name, ", ".join(sorted(launchvars.RESERVED))))
    return problems


def _keep_version_1(path):
    """Copy a version-1 file aside, once, before it is written as version 2."""
    kept = path + ".v1"
    if os.path.exists(kept) or not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8-sig") as handle:
            version = json.load(handle).get("version", VERSION)
    except (OSError, ValueError, AttributeError):
        return
    if isinstance(version, int) and version < 2:
        try:
            shutil.copy2(path, kept)
        except OSError:
            pass        # the .bak still holds it; a missing copy must not block saving


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
    _keep_version_1(path)
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
#: users.json value. ``address`` is its ``url``, ``variables`` the others;
#: ``origin`` and ``accounts`` come from the inventory.
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
                                     row.address,
                                     {name: value for name, value in row.variables.items()
                                      if name != URL},
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
        settle_ids(projects)
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
