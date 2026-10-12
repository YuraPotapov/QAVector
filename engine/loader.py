"""Load declarative flows/blocks/scenarios from the ``flows/`` tree.

Flow ids map to files by a simple convention:
  - dotted ids are paths:  ``auth.login`` -> ``flows/auth/login.yaml``
  - bare ids are scenarios: ``my_scenario`` -> ``flows/scenarios/my_scenario.yaml``
    (falling back to ``flows/my_scenario.yaml``)

There can be more than one tree. An installed build keeps its flows inside the
application bundle, where nothing can be written, so anything the user creates
lives in a tree of their own that is searched *first* - see
``runtime_paths.flows_search_path``. A scenario there shadows a bundled one with
the same id, and can still ``use:`` the blocks and named selectors the app ships.

One rule keeps that predictable: ``--flows-dir=DIR`` means exactly that directory
and nothing else. Only the default is layered. So a caller passing a directory
gets what it asked for, and a source checkout - where both trees are the same
directory - behaves as it always has.

``pyyaml`` is imported lazily so importing this module (e.g. for the domain unit
tests) does not require it.
"""

import os

import runtime_paths

from domain.flow import Flow

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_FLOWS_DIR = os.path.join(PROJECT_ROOT, "flows")


def search_path(flows_dir=None):
    """The trees to look in, nearest first.

    ``None`` means the layered default. A single directory is taken literally -
    that is what ``--flows-dir`` means. A list is taken literally too, which is
    how a caller stages a candidate tree in front of the real ones (see
    ``flowfile._validate_text``, which compiles an unsaved scenario against
    everything it will be able to see once it is written).
    """
    if isinstance(flows_dir, (list, tuple)):
        return [str(entry) for entry in flows_dir] or [DEFAULT_FLOWS_DIR]
    if flows_dir:
        return [flows_dir]
    return runtime_paths.flows_search_path() or [DEFAULT_FLOWS_DIR]

# Scenarios carrying any of these tags are skipped by --run-tests=all (still runnable
# by id): `template` needs real selectors; `manual` has side effects (writes data);
# `blocked` is parked (a known unmet dependency). `blocked` scenarios are also NOT tagged
# `manual`, so they stay out of `tag:manual` runs; find them via `tag:blocked`.
_SKIP_TAGS = {"template", "manual", "blocked"}


class FlowError(Exception):
    """A flow file is malformed."""


class FlowNotFound(FlowError):
    """No file exists for a requested flow id."""


def _load_yaml(path):
    from engine import yamlload  # lazy: only needed when actually reading flows
    with open(path, encoding="utf-8") as fh:
        return yamlload.safe_load(fh) or {}


def candidate_paths(flow_id, flows_dir=None):
    """Every path ``flow_id`` could name, best first, across every tree."""
    for root in search_path(flows_dir):
        if "." in flow_id:
            yield os.path.join(root, *flow_id.split(".")) + ".yaml"
        else:
            yield os.path.join(root, "scenarios", flow_id + ".yaml")
            yield os.path.join(root, flow_id + ".yaml")


def flow_path(flow_id, flows_dir=None):
    """Resolve a flow id to a file path (may not exist).

    The first tree that has the file wins. When nothing does, the canonical path
    in the *nearest* tree is returned, because that is where the file would go and
    it makes the "no flow file for X" message point somewhere useful.
    """
    for candidate in candidate_paths(flow_id, flows_dir):
        if os.path.exists(candidate):
            return candidate
    return canonical_path(flow_id, flows_dir)


def canonical_path(flow_id, flows_dir=None):
    """Where ``flow_id`` belongs when it is written, whether or not it exists."""
    root = search_path(flows_dir)[0]
    if "." in flow_id:
        return os.path.join(root, *flow_id.split(".")) + ".yaml"
    return os.path.join(root, "scenarios", flow_id + ".yaml")


def load_flow(flow_id, flows_dir=None):
    """Load one flow/block/scenario as a :class:`Flow` (raw, uncompiled steps)."""
    path = flow_path(flow_id, flows_dir)
    if not os.path.exists(path):
        raise FlowNotFound("no flow file for %r (looked at %s)" % (flow_id, path))
    data = _load_yaml(path)
    if not isinstance(data, dict):
        raise FlowError("%s must be a YAML mapping" % path)
    steps = data.get("steps") or []
    if not isinstance(steps, list):
        raise FlowError("%s: 'steps' must be a list" % path)
    return Flow(
        id=data.get("id", flow_id),
        steps=steps,
        name=data.get("name"),
        description=data.get("description"),
        tags=data.get("tags") or [],
        source=path,
    )


def load_selectors(flows_dir=None):
    """Load the named-target -> CSS map from ``flows/selectors.yaml`` ({} if absent).

    Merged across trees, furthest first, so a name defined in the user's own
    selectors.yaml overrides the bundled one and everything else still resolves.
    That is what lets someone re-point a single selector after a UI change without
    taking a copy of the whole file.
    """
    merged = {}
    for root in reversed(search_path(flows_dir)):
        path = os.path.join(root, "selectors.yaml")
        if not os.path.exists(path):
            continue
        data = _load_yaml(path)
        if isinstance(data, dict):
            merged.update(data)
    return merged


def scenario_files(flows_dir=None):
    """``{id: path}`` for every scenario in every tree, nearest tree winning.

    One id names one file even when two trees offer it, so a user scenario
    shadows a bundled one rather than the pair of them both turning up in the
    list and disagreeing about what --run-tests should run.
    """
    found = {}
    for root in search_path(flows_dir):
        scenarios_dir = os.path.join(root, "scenarios")
        if not os.path.isdir(scenarios_dir):
            continue
        for name in sorted(os.listdir(scenarios_dir)):
            if name.endswith(".yaml"):
                found.setdefault(name[:-5], os.path.join(scenarios_dir, name))
    return found


def block_files(flows_dir=None):
    """``{dotted id: path}`` for every reusable block, nearest tree winning.

    Everything that is not a scenario and not selectors.yaml: the files a
    scenario reaches through ``use:``. They are not runnable on their own, so
    discover_scenarios deliberately ignores them - but an editor has to be able
    to show you the one a step names, or ``use: access.open_app`` is a dead end.
    """
    found = {}
    for root in search_path(flows_dir):
        if not os.path.isdir(root):
            continue
        for folder, _dirs, files in os.walk(root):
            relative = os.path.relpath(folder, root)
            if relative == "." or relative.split(os.sep)[0] == "scenarios":
                continue
            for name in sorted(files):
                if not name.endswith(".yaml"):
                    continue
                parts = relative.split(os.sep) + [name[:-5]]
                found.setdefault(".".join(parts), os.path.join(folder, name))
    return found


def discover_scenarios(flows_dir=None, include_templates=False):
    """Return the ids of runnable scenarios under ``flows/scenarios`` (sorted).

    Scenarios tagged ``template`` (need real selectors) or ``manual`` (have side
    effects, e.g. one that creates a record) are skipped by default, so
    ``--run-tests=all`` only runs safe, configured scenarios. Pass
    ``include_templates=True`` to list them too; they are always runnable by id.
    """
    ids = []
    for flow_id, path in sorted(scenario_files(flows_dir).items()):
        if not include_templates:
            data = _load_yaml(path)
            if set((data or {}).get("tags") or []) & _SKIP_TAGS:
                continue
        ids.append(flow_id)
    return ids


def scenarios_with_tag(tag, flows_dir=None):
    """Return the ids of every scenario carrying ``tag`` (sorted).

    Unlike :func:`discover_scenarios`, this does NOT skip template/manual - asking for a
    tag explicitly includes them (e.g. ``tag:manual`` returns the write scenarios).
    """
    ids = []
    for flow_id, path in sorted(scenario_files(flows_dir).items()):
        data = _load_yaml(path)
        if tag in ((data or {}).get("tags") or []):
            ids.append(flow_id)
    return ids
