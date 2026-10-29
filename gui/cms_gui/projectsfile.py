"""Projects: each one a folder that holds everything shown while it is picked.

Picking a project in the top bar switches every page to that project's own
files - its users, environments, launch configurations, services, log sources,
scenarios, cycles, their secrets and memory, run history, reports and browser
profiles - so a project just added shows an empty application.

**Workspace** is the project that was always there: its files are wherever
Settings say, exactly as before projects existed, and nothing of it moves.
Every other project is a folder, ``~/QAVector/projects/<name>`` by default,
laid out the same way every time (:func:`paths`). What stays the same whichever
project is picked is how the window looks and behaves - theme, hotkeys, layout -
and the services already running.

The list itself is ``projects.json`` beside the GUI's other files: a version and
the projects, each an id, a name and its folder. A project's folder is never
deleted from here - taking a project off the list leaves its data on disk. No Qt.
"""

import collections
import json
import os
import re
import unicodedata
import uuid

from . import core

VERSION = 1
FILE_NAME = "projects.json"

#: The project whose paths are the settings.
WORKSPACE = "workspace"
WORKSPACE_NAME = "Workspace"

#: One project: its id, its name, and its folder ("" for Workspace).
Project = collections.namedtuple("Project", "id name folder")

#: Every file and folder of a project, by what it is.
Paths = collections.namedtuple(
    "Paths", "users launch_projects launch_presets launch_drafts services log_sources "
             "flows cycles cycle_projects cycle_layout cycle_secrets cycle_memory "
             "cycle_runs reports sessions history")

#: The folders a new project starts with: what the launcher refuses to run
#: without (a flows folder) and what it writes into.
SKELETON = ("flows/scenarios", "cycles", "cycle-runs", "reports", "user_sessions")


class ProjectsFileError(Exception):
    """The projects list could not be read or written."""


def default_path():
    return os.path.join(core.home_folder(), FILE_NAME)


def projects_root():
    """Where a new project's folder goes."""
    return os.path.join(core.home_folder(), "projects")


def workspace():
    return Project(WORKSPACE, WORKSPACE_NAME, "")


def paths(folder):
    """Every file of the project in ``folder``."""
    join = lambda *parts: os.path.join(folder, *parts)      # noqa: E731
    return Paths(users=join("users.json"),
                 launch_projects=join("launch-projects.json"),
                 launch_presets=join("launch-presets.json"),
                 launch_drafts=join("launch-drafts.json"),
                 services=join("services.json"),
                 log_sources=join("logsources.json"),
                 flows=join("flows"), cycles=join("cycles"),
                 cycle_projects=join("cycleprojects.json"),
                 cycle_layout=join("cyclelayout.json"),
                 cycle_secrets=join("cyclesecrets.json"),
                 cycle_memory=join("cyclememory.json"),
                 cycle_runs=join("cycle-runs"), reports=join("reports"),
                 sessions=join("user_sessions"), history=folder)


def make_folder(folder):
    """Lay out a project's folder; what is there already is left alone."""
    for part in SKELETON:
        os.makedirs(os.path.join(folder, part), exist_ok=True)
    return folder


# -- the list -----------------------------------------------------------------
def load(path=None):
    """``[Project]``, Workspace first - and only Workspace when there is no list."""
    path = path or default_path()
    found = [workspace()]
    if not os.path.exists(path):
        return found
    try:
        with open(path, encoding="utf-8-sig") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ProjectsFileError("cannot read %s: %s" % (path, exc))
    rows = document.get("projects") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise ProjectsFileError("%s: projects has to be a list" % path)
    for row in rows:
        if not isinstance(row, dict) or not row.get("id") or row.get("id") == WORKSPACE:
            continue
        found.append(Project(str(row["id"]), str(row.get("name") or row["id"]),
                             os.path.expanduser(str(row.get("folder") or ""))))
    return found


def save(projects, path=None):
    """Write the list (Workspace is implied, never written)."""
    path = path or default_path()
    rows = [{"id": p.id, "name": p.name, "folder": p.folder}
            for p in projects if p.id != WORKSPACE]
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    temp = os.path.join(directory, ".%s.tmp" % os.path.basename(path))
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump({"version": VERSION, "projects": rows}, handle, indent=2,
                      ensure_ascii=False)
            handle.write("\n")
        os.replace(temp, path)
    except OSError as exc:
        raise ProjectsFileError("cannot write %s: %s" % (path, exc))
    return path


def find(projects, project_id):
    for project in projects:
        if project.id == project_id:
            return project
    return None


def _slug(name):
    plain = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore")
    return re.sub(r"[^a-z0-9]+", "-", plain.decode("ascii").lower()).strip("-") \
        or "project"


def problem_with_name(projects, name, keep=""):
    """Why ``name`` cannot be a project's name; "" when it can."""
    name = (name or "").strip()
    if not name:
        return "Give the project a name."
    if any(p.name.casefold() == name.casefold() and p.id != keep for p in projects):
        return "There is a project called %s already." % name
    return ""


def add(name, path=None, root=None):
    """A new project called ``name``, its folder laid out, the list written."""
    projects = load(path)
    problem = problem_with_name(projects, name)
    if problem:
        raise ProjectsFileError(problem)
    root = root or projects_root()
    base = os.path.join(root, _slug(name))
    folder, number = base, 1
    taken = {os.path.abspath(p.folder) for p in projects if p.folder}
    while os.path.abspath(folder) in taken or os.path.exists(folder):
        number += 1
        folder = "%s-%d" % (base, number)
    make_folder(folder)
    project = Project("p-%s" % uuid.uuid4().hex[:8], name.strip(), folder)
    save(projects + [project], path)
    return project


def rename(project_id, name, path=None):
    projects = load(path)
    problem = problem_with_name(projects, name, keep=project_id)
    if problem:
        raise ProjectsFileError(problem)
    if project_id == WORKSPACE:
        raise ProjectsFileError("Workspace keeps its name.")
    save([p._replace(name=name.strip()) if p.id == project_id else p for p in projects],
         path)


def remove(project_id, path=None):
    """Take a project off the list; its folder stays where it is."""
    if project_id == WORKSPACE:
        raise ProjectsFileError("Workspace cannot be taken off the list.")
    save([p for p in load(path) if p.id != project_id], path)


# -- from the projects Launch Sessions used to have -----------------------------
def adopt_launch_projects(launch_projects, path=None, root=None):
    """The first time: launch projects become projects of their own.

    Before projects held everything, Launch Sessions grouped environments in
    projects of its own (launch-projects.json). An empty one becomes a project
    - which is what whoever added it meant. One with environments gives them to
    Workspace's own, where their users and configurations are; they move to
    another project with Export / Import. ``launch_projects`` is changed in
    place; returns the projects added. Does nothing once the list exists.
    """
    from . import launchprojectsfile as lpf
    if os.path.exists(path or default_path()):
        return []
    added = []
    for project in list(launch_projects):
        if project.id == lpf.WORKSPACE:
            continue
        if not project.environments:
            added.append(add(project.name, path, root))
        else:
            lpf.project_row(launch_projects, lpf.WORKSPACE).environments.extend(
                project.environments)
        launch_projects.remove(project)
    if not added:
        save(load(path), path)          # the list exists now: this runs once
    return added
