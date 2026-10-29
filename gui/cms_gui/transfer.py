"""Export and import: projects, environments, users and cycles, in one file.

What is exported is chosen by what it means, not by which file it lives in. A
project brings every environment in it and every user of those; an environment
brings its users; a user brings itself and the names of the environments it
signs in to; a cycle project brings its cycles. The file is JSON::

    {"format": "qavector-export", "version": 1, "exported": "2026-10-28T21:30",
     "projects":       [{"name": "Helpdesk"}],
     "environments":   [{"project": "Helpdesk", "name": "Local",
                         "variables": {"url": "http://localhost:{{port}}", "port": "8069"}}],
     "users":          [{"role": "Agent", "login": "agent", "password": "...",
                         "environments": [{"project": "Helpdesk", "environment": "Local",
                                           "scenarios": ["smoke"]}]}],
     "cycle_projects": [{"name": "Claim", "description": "", "colour": ""}],
     "cycles":         [{"id": "ready", "project": "Claim", "yaml": "...",
                         "secrets": {"jira_token": "..."}}]}

Passwords and secret values are written as they are - the file is as private as
users.json, and is written readable by its owner only. Environments are named,
never keyed: an account key is this machine's business (it names profile
folders), so an imported environment is given one of its own.

Importing says first what it would add and what is already here; for what is
already here it either **adds to the existing one** (an environment's missing
variables and users, a project's environments, a cycle's new text) or **adds a
copy** beside it, named "... (copy)". No Qt here; cycles go through the core,
which validates them, so the part that writes cycles is handed a core.
"""

import collections
import datetime
import json
import os
import re
import shutil
import tempfile

from . import cycleprojectsfile as cpf, launchprojectsfile as lpf, usersfile

FORMAT = "qavector-export"
VERSION = 1
SUFFIX = ".qavector.json"

#: What is already here gets this.
MERGE, COPY = "merge", "copy"


class TransferError(Exception):
    """An export file could not be read or written."""


# -- exporting ----------------------------------------------------------------
def _project_names(projects, catalog):
    names = dict(lpf.project_names(projects, catalog))
    names.setdefault(lpf.WORKSPACE, lpf.WORKSPACE_NAME)
    return names


def launch_part(projects, catalog, rows, project_ids=(), env_ids=(), people=()):
    """``{projects, environments, users}`` for what was chosen.

    ``project_ids`` brings every environment of those projects; ``env_ids`` the
    environments named; both bring their users, with what they run there.
    ``people`` (``usersfile.Credential`` keys) brings those users with every
    environment they sign in to, named - whether or not it is exported too.
    """
    names = _project_names(projects, catalog)
    chosen = [env for env in catalog
              if env.project_id in set(project_ids) or env.id in set(env_ids)]
    by_key = {}
    for env in catalog:
        by_key.setdefault(env.account_key, env)
    found = {"projects": [], "environments": [], "users": []}
    seen_projects = []
    for project_id in list(project_ids) + [env.project_id for env in chosen]:
        if project_id not in seen_projects:
            seen_projects.append(project_id)
            found["projects"].append({"name": names.get(project_id, project_id)})
    for env in chosen:
        variables = {lpf.URL: env.address or env.origin} if (env.address or env.origin) \
            else {}
        variables.update(env.variables)
        found["environments"].append({"project": names.get(env.project_id, ""),
                                      "name": env.name, "variables": variables})
    keys = {env.account_key for env in chosen}
    for person in usersfile.credentials(rows):
        if not person.login:
            continue
        whole = person.key in set(people)
        places = []
        for index in person.indices:
            row = rows[index]
            env = by_key.get(row.env)
            if env is None or not (whole or row.env in keys):
                continue
            places.append({"project": names.get(env.project_id, ""),
                           "environment": env.name, "scenarios": list(row.tests)})
        if whole or places:
            found["users"].append({"role": person.role, "login": person.login,
                                   "password": person.password, "environments": places})
    return found


def cycles_part(cycle_rows, cycle_ids=(), project_names=(), cycle_projects=(),
                secrets=None):
    """``{cycle_projects, cycles}`` for what was chosen.

    ``project_names`` brings every cycle of those projects ("" is the cycles of
    none). ``secrets(cycle_id)`` gives a cycle's secret values, when it can.
    """
    wanted = [row for row in cycle_rows
              if row.get("id") in set(cycle_ids)
              or (row.get("project") or "") in set(project_names)]
    found = {"cycle_projects": [], "cycles": []}
    known = {project.name: project for project in cycle_projects}
    named = []
    for name in list(project_names) + [row.get("project") or "" for row in wanted]:
        if name and name not in named:
            named.append(name)
            project = known.get(name)
            found["cycle_projects"].append({
                "name": name,
                "description": project.description if project else "",
                "colour": project.colour if project else ""})
    for row in wanted:
        try:
            with open(row["path"], encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, KeyError) as exc:
            raise TransferError("cannot read cycle %s: %s" % (row.get("id"), exc))
        entry = {"id": row["id"], "project": row.get("project") or "", "yaml": text}
        values = secrets(row["id"]) if secrets is not None else None
        if values:
            entry["secrets"] = dict(values)
        found["cycles"].append(entry)
    return found


def bundle(*parts):
    """One export document from the parts."""
    document = {"format": FORMAT, "version": VERSION,
                "exported": datetime.datetime.now().isoformat(timespec="seconds"),
                "projects": [], "environments": [], "users": [],
                "cycle_projects": [], "cycles": []}
    for part in parts:
        for key, values in part.items():
            document[key].extend(values)
    return document


def write(path, document):
    """Write ``document``, readable by its owner only - it holds passwords."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle, temp = tempfile.mkstemp(prefix=".qavector-export-", dir=directory)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except OSError as exc:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise TransferError("cannot write %s: %s" % (path, exc))
    return path


def counts(document):
    """``"2 environments, 5 users, 1 cycle"`` - what a document holds."""
    parts = []
    for key, one, many in (("projects", "project", "projects"),
                           ("environments", "environment", "environments"),
                           ("users", "user", "users"),
                           ("cycle_projects", "cycle project", "cycle projects"),
                           ("cycles", "cycle", "cycles")):
        number = len(document.get(key) or [])
        if number:
            parts.append("%d %s" % (number, one if number == 1 else many))
    return ", ".join(parts) or "nothing"


# -- reading ------------------------------------------------------------------
def read(path):
    """An export document from ``path``; a plain cycle file is read as one cycle."""
    try:
        with open(path, encoding="utf-8-sig") as handle:
            text = handle.read()
    except OSError as exc:
        raise TransferError("cannot read %s: %s" % (path, exc))
    if path.lower().endswith((".yaml", ".yml")):
        cycle_id = os.path.splitext(os.path.basename(path))[0]
        match = re.search(r"^project:\s*[\"']?([^\"'\n#]*?)[\"']?\s*(#.*)?$", text, re.M)
        return bundle({"cycles": [{"id": cycle_id, "yaml": text,
                                   "project": match.group(1).strip() if match else ""}]})
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise TransferError("%s is not an export file: %s" % (path, exc))
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise TransferError("%s is not a QAVector export file." % path)
    version = document.get("version", VERSION)
    if isinstance(version, int) and version > VERSION:
        raise TransferError("%s was written by a newer QAVector (version %d)."
                            % (path, version))
    for key in ("projects", "environments", "users", "cycle_projects", "cycles"):
        if not isinstance(document.get(key) or [], list):
            raise TransferError("%s: %s has to be a list." % (path, key))
        document[key] = [one for one in document.get(key) or [] if isinstance(one, dict)]
    return document


# -- what an import would do ----------------------------------------------------
#: One thing in a file: what it is, its name, and whether one is here already.
Item = collections.namedtuple("Item", "kind name exists")


def _folded(text):
    return (text or "").strip().casefold()


def _project_ids_by_name(projects, catalog):
    return {_folded(name): project_id
            for project_id, name in _project_names(projects, catalog).items()}


def _existing_env(catalog, project_id, name):
    for env in catalog:
        if env.project_id == project_id and _folded(env.name) == _folded(name):
            return env
    return None


def plan(document, projects, catalog, rows, cycle_project_names=(), cycle_ids=()):
    """``[Item]``: everything in ``document``, each said new or already here."""
    project_ids = _project_ids_by_name(projects, catalog)
    logins = {person.login for person in usersfile.credentials(rows)}
    items = []
    for project in document.get("projects") or []:
        items.append(Item("Project", project.get("name", ""),
                          _folded(project.get("name")) in project_ids))
    for env in document.get("environments") or []:
        project_id = project_ids.get(_folded(env.get("project")))
        here = project_id is not None and \
            _existing_env(catalog, project_id, env.get("name")) is not None
        items.append(Item("Environment", "%s / %s" % (env.get("project", ""),
                                                      env.get("name", "")), here))
    for user in document.get("users") or []:
        items.append(Item("User", user.get("login", ""), user.get("login") in logins))
    folded_projects = {_folded(name) for name in cycle_project_names}
    for project in document.get("cycle_projects") or []:
        items.append(Item("Cycle project", project.get("name", ""),
                          _folded(project.get("name")) in folded_projects))
    for cycle in document.get("cycles") or []:
        items.append(Item("Cycle", cycle.get("id", ""), cycle.get("id") in set(cycle_ids)))
    return items


def _copy_name(name, taken):
    folded = {_folded(one) for one in taken}
    candidate, number = "%s (copy)" % name, 1
    while _folded(candidate) in folded:
        number += 1
        candidate = "%s (copy %d)" % (name, number)
    return candidate


# -- importing projects, environments and users ---------------------------------
def apply_launch(document, mode, projects, catalog, rows):
    """Put the launch part of ``document`` into ``projects`` (launch-projects
    rows) and ``rows`` (users.json rows), both changed in place.

    Returns what was done, line by line, and what could not be, said why.
    """
    said = []
    project_ids = _project_ids_by_name(projects, catalog)
    target_projects = {}

    def project_for(name):
        folded = _folded(name) or _folded(lpf.WORKSPACE_NAME)
        if folded in target_projects:
            return target_projects[folded]
        existing = project_ids.get(folded)
        if existing is not None and (mode == MERGE or existing == lpf.WORKSPACE):
            lpf.project_row(projects, existing)
            target_projects[folded] = existing
            return existing
        taken = [project.name for project in projects] + [lpf.WORKSPACE_NAME]
        title = _copy_name(name, taken) if existing is not None else (name or "Imported")
        project_id = lpf.new_id("p")
        projects.append(lpf.ProjectRow(project_id, title))
        said.append("Added project %s." % title)
        target_projects[folded] = project_id
        return project_id

    for project in document.get("projects") or []:
        project_for(project.get("name", ""))

    taken_keys = [env.account_key for env in catalog] + [row.env for row in rows]
    env_keys = {}
    #: account key -> environment name, for saying where users went.
    labels = {env.account_key: env.name for env in catalog}
    joined = collections.OrderedDict()
    for env in document.get("environments") or []:
        project_id = project_for(env.get("project", ""))
        name = (env.get("name") or "").strip() or "Imported"
        variables = {str(k): lpf.as_text(v)
                     for k, v in (env.get("variables") or {}).items() if v is not None}
        existing = _existing_env(catalog, project_id, name)
        here = _folded(env.get("project")), _folded(env.get("name"))
        if existing is not None and mode == MERGE:
            row = lpf.environment_row(projects, existing)
            if existing.virtual and not row.address and existing.origin:
                row.address = existing.origin
            added = []
            for key, value in variables.items():
                if key == lpf.URL:
                    if not row.address:
                        row.address = value
                        added.append(key)
                elif key not in row.variables:
                    row.variables[key] = value
                    added.append(key)
            said.append("Added to environment %s%s." % (
                existing.name, " the variables %s" % ", ".join(added) if added else ""))
            env_keys[here] = existing.account_key
            continue
        siblings = [one.name for one in catalog if one.project_id == project_id] + \
            [one.name for project in projects if project.id == project_id
             for one in project.environments]
        if existing is not None:
            name = _copy_name(name, siblings)
        key = lpf.new_key(name, taken_keys)
        taken_keys.append(key)
        lpf.project_row(projects, project_id).environments.append(
            lpf.EnvironmentRow(lpf.new_id("e"), name, key, "", variables))
        labels[key] = name
        said.append("Added environment %s." % name)
        env_keys[here] = key

    def key_for(place):
        wanted = _folded(place.get("project")), _folded(place.get("environment"))
        if wanted in env_keys:
            return env_keys[wanted]
        project_id = project_ids.get(wanted[0])
        if project_id is not None:
            env = _existing_env(catalog, project_id, place.get("environment"))
            if env is not None:
                return env.account_key
        same = [env for env in catalog if _folded(env.name) == wanted[1]]
        return same[0].account_key if len(same) == 1 else ""

    for user in document.get("users") or []:
        login = (user.get("login") or "").strip()
        if not login:
            continue
        role = (user.get("role") or "").strip()
        password = user.get("password") or ""
        people = [person for person in usersfile.credentials(rows) if person.login == login]
        exact = [person for person in people
                 if person.password == password and person.role == role]
        if exact:
            person_key = exact[0].key
        elif people and mode == MERGE:
            person_key = people[0].key
        else:
            if people:
                role = _copy_name(role or login, [one.role for one in people])
            person_key = (login, password, role)
        places = []
        for place in user.get("environments") or []:
            key = key_for(place)
            if not key:
                said.append("%s: %s / %s is not here - left out."
                            % (login, place.get("project", ""),
                               place.get("environment", "")))
                continue
            places.append((key, usersfile.parse_tests(place.get("scenarios") or [])))
        if usersfile.credential(rows, person_key) is None:
            first = places.pop(0) if places else ("", [])
            clash = [row for row in rows if row.login == login and row.env == first[0]]
            if clash:
                said.append("%s is already in that environment with another password "
                            "or role - left out." % login)
                continue
            rows.append(usersfile.UserRow(first[0], role, login, password, first[1]))
            said.append("Added user %s." % login)
            if first[0]:
                joined.setdefault(labels.get(first[0], first[0]), []).append(login)
        for key, tests in places:
            try:
                if usersfile.assign(rows, person_key, key, tests):
                    joined.setdefault(labels.get(key, key), []).append(login)
            except usersfile.UsersFileError as exc:
                said.append(str(exc))
    for name, logins in joined.items():
        said.append("Into %s: %s." % (name, ", ".join(logins)))
    return said


# -- importing cycles -----------------------------------------------------------
def _cycle_copy_text(text, cycle_id):
    """The cycle as a copy: its top-level ``id:`` the copy's (the core wants it
    to match the file), and " (copy)" after a plain ``name:`` - so the copy
    reads as one in the list."""
    def suffixed(match):
        return "name: %s%s (copy)%s" % (match.group(1), match.group(2), match.group(1))
    text = re.sub(r"^id:[ \t]*.*$", "id: %s" % cycle_id, text, count=1, flags=re.M)
    return re.sub(r"^name:[ \t]*([\"']?)(.+?)\1[ \t]*$", suffixed, text, count=1,
                  flags=re.M)


def _safe_id(text):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text or "").strip("_") or "cycle"


def apply_cycles(document, mode, core, projects_path, cycle_ids):
    """Put the cycles of ``document`` in through ``core``; their projects in
    ``projects_path`` (cycleprojects.json). Returns what was done, said."""
    said = []
    try:
        projects = cpf.load(projects_path)
    except cpf.CycleProjectsFileError as exc:
        raise TransferError(str(exc))
    names = {_folded(project.name): project.name for project in projects}
    renamed = {}
    for entry in document.get("cycle_projects") or []:
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        if _folded(name) in names and mode == MERGE:
            renamed[name] = names[_folded(name)]
            continue
        title = _copy_name(name, list(names.values())) if _folded(name) in names else name
        projects.append(cpf.ProjectRow(title, entry.get("description", ""),
                                       entry.get("colour", ""),
                                       datetime.date.today().isoformat()))
        names[_folded(title)] = title
        renamed[name] = title
        said.append("Added cycle project %s." % title)
    if renamed:
        try:
            cpf.save(projects_path, projects)
        except cpf.CycleProjectsFileError as exc:
            raise TransferError(str(exc))

    here = set(cycle_ids)
    folder = tempfile.mkdtemp(prefix="qavector-import-")
    try:
        for entry in document.get("cycles") or []:
            cycle_id = _safe_id(entry.get("id"))
            text = entry.get("yaml") or ""
            if cycle_id in here and mode == MERGE:
                answer = core.cycle_save(cycle_id, {"yaml": text})
                verb = "Updated cycle %s." % cycle_id
            else:
                if cycle_id in here:
                    base, number = cycle_id + "_copy", 1
                    cycle_id = base
                    while cycle_id in here:
                        number += 1
                        cycle_id = "%s%d" % (base, number)
                    text = _cycle_copy_text(text, cycle_id)
                path = os.path.join(folder, cycle_id + ".yaml")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(text)
                answer = core.cycle_import(path)
                verb = "Added cycle %s." % cycle_id
            if not answer.get("ok"):
                said.append("Cycle %s was not imported: %s" % (
                    cycle_id, "; ".join(str(one) for one in answer.get("problems") or [])))
                continue
            here.add(cycle_id)
            said.append(verb)
            project = entry.get("project") or ""
            if project and renamed.get(project, project) != project:
                core.cycle_move([{"id": cycle_id, "project": renamed[project]}])
            for name, value in (entry.get("secrets") or {}).items():
                core.cycle_secret_set(cycle_id, name, value)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return said
