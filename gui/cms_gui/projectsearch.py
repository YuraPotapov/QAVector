"""Search (Ctrl+K) in the projects that are not picked.

The pages answer for the project on screen. For every other project this reads
its folder directly - the JSON files and the names of its scenario and cycle
files - so a search finds a user, an environment or a cycle of any project
without a core being started for it. Each hit says its project; opening one
picks that project first, and the page shows the row once its data is loaded
(see ``MainWindow.open_hit``).

Hits carry the same targets the pages' own ``search_rows`` give, so the page's
``show_found`` finds them. Read once per project and kept while the files are
unchanged: Search asks every second while it is open.
"""

import json
import os
import re

from . import launchprojectsfile as lpf, projectsfile, search, usersfile

_NAME = re.compile(r"^name:[ \t]*[\"']?(.+?)[\"']?[ \t]*$", re.M)
_PROJECT = re.compile(r"^project:[ \t]*[\"']?(.+?)[\"']?[ \t]*$", re.M)

#: {folder: (signature, hits)} - read again when a file in it changes.
_cache = {}


def _json(path, default):
    try:
        with open(path, encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _yaml_files(folder):
    try:
        return sorted(name for name in os.listdir(folder)
                      if name.endswith((".yaml", ".yml")))
    except OSError:
        return []


def _head(path, size=4096):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read(size)
    except OSError:
        return ""


def _signature(paths):
    found = []
    for path in (paths.users, paths.launch_projects, paths.launch_presets,
                 paths.services, paths.log_sources,
                 os.path.join(paths.history, "history.json"),
                 os.path.join(paths.flows, "scenarios"), paths.cycles):
        try:
            found.append(os.stat(path).st_mtime_ns)
        except OSError:
            found.append(0)
    return tuple(found)


def _hits(paths):
    """Every row of one project's folder, as page hits (sections filled in)."""
    found = []
    try:
        rows = usersfile.load(paths.users)
    except usersfile.UsersFileError:
        rows = []
    try:
        projects = lpf.load(paths.launch_projects)
    except lpf.LaunchProjectsError:
        projects = []

    class Unclaimed:
        envs = [{"value": value, "alias": value, "origin": "", "count": 0}
                for value in dict.fromkeys(row.env for row in rows if row.env)]
    catalog = lpf.catalog(projects, Unclaimed)
    names = {env.account_key: env.name for env in catalog}
    for env in catalog:
        found.append(search.hit(env.name, env.address, target=env.id,
                                section="environments"))
    for person in usersfile.credentials(rows):
        if person.login:
            places = ", ".join(names.get(env, env) for env in person.envs(rows))
            found.append(search.hit(person.login, " · ".join(
                part for part in (person.role, places) if part),
                target=(person.login, person.role), section="credentials"))
    presets = _json(paths.launch_presets, {})
    for preset in (presets.get("presets") if isinstance(presets, dict) else None) or []:
        if isinstance(preset, dict) and preset.get("id"):
            found.append(search.hit(preset.get("name") or preset["id"], "configuration",
                                    target=preset["id"], section="launch"))
    services = _json(paths.services, {})
    for project in (services.get("projects") if isinstance(services, dict) else None) \
            or []:
        if not isinstance(project, dict):
            continue
        for runner in project.get("runners") or []:
            if isinstance(runner, dict) and runner.get("name"):
                found.append(search.hit(
                    runner["name"], project.get("name", ""),
                    target=("runner", project.get("name", ""), runner["name"]),
                    section="logsources"))
    scenarios = os.path.join(paths.flows, "scenarios")
    for name in _yaml_files(scenarios):
        flow_id = os.path.splitext(name)[0]
        title = _NAME.search(_head(os.path.join(scenarios, name)))
        found.append(search.hit(title.group(1) if title else flow_id, flow_id,
                                target=flow_id, section="scenarios"))
    for name in _yaml_files(paths.cycles):
        cycle_id = os.path.splitext(name)[0]
        text = _head(os.path.join(paths.cycles, name))
        title, group = _NAME.search(text), _PROJECT.search(text)
        found.append(search.hit(
            title.group(1) if title else cycle_id,
            " · ".join(part for part in (group.group(1) if group else "", cycle_id)
                       if part),
            target=("cycle", cycle_id), section="cycles"))
    history = _json(os.path.join(paths.history, "history.json"), [])
    for entry in history if isinstance(history, list) else []:
        if isinstance(entry, dict) and entry.get("id"):
            found.append(search.hit(entry.get("summary") or entry.get("kind") or "run",
                                    entry.get("started_at") or "",
                                    target=entry["id"], section="history"))
    return found


def other_projects(current_id, workspace_paths=None):
    """Hits from every project but ``current_id``, each marked with its project.

    ``workspace_paths`` is Workspace's files (its settings), when Workspace is
    not the one on screen.
    """
    try:
        projects = projectsfile.load()
    except projectsfile.ProjectsFileError:
        return []
    found = []
    for project in projects:
        if project.id == current_id:
            continue
        if project.folder:
            paths = projectsfile.paths(project.folder)
        elif workspace_paths is not None:
            paths = workspace_paths
        else:
            continue
        key = paths.history + "|" + paths.users
        signature = _signature(paths)
        cached = _cache.get(key)
        if cached is None or cached[0] != signature:
            cached = (signature, _hits(paths))
            _cache[key] = cached
        found.extend(one._replace(project=project.id,
                                  detail=" · ".join(part for part in (one.detail,
                                                                      project.name)
                                                    if part))
                     for one in cached[1])
    return found
