"""Read-only contribution discovery. Never imports installed Python packages."""

import json
from pathlib import Path
import sqlite3

from . import bundled
from .compatibility import report


def contributions(path=None):
    path = Path(path or bundled.registry_path())
    if not path.is_file() or path.is_symlink():
        return []
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        rows = db.execute('SELECT plugin_id,version,digest,manifest,state FROM packages').fetchall()
    finally:
        db.close()
    result = []
    for plugin_id, version, digest, raw, state in rows:
        manifest = json.loads(raw)
        compatibility = report(manifest)
        for contribution in manifest['contributions']:
            result.append(dict(plugin_id=plugin_id, version=version, digest=digest,
                contribution=contribution, state=state,
                effective_state=state if compatibility['compatible'] else 'incompatible',
                compatibility=compatibility, permissions=manifest['permissions_requested']))
    return sorted(result, key=lambda one: (one['contribution']['id'],
        one['state'] == 'active', tuple(map(int, one['version'].split('.')))), reverse=True)


def selected(kind):
    result = {}
    for item in contributions():
        if item['contribution']['kind'] == kind:
            result.setdefault(item['contribution']['id'], item)
    return result


def inventory(host):
    """Unified display catalog; core nodes are not invented qavplug packages."""
    from cycle.registry import builtin_descriptions
    import runtime_paths
    rows = host.list()
    current = bundled.claude_binding()
    if not any(row['plugin_id'] == current['plugin_id'] and row['version'] == current['version'] for row in rows):
        manifest = bundled.claude_manifest()
        compatibility = report(manifest)
        rows.append(dict(plugin_id=current['plugin_id'], version=current['version'], digest=current['digest'],
            state='bundled_available', effective_state='bundled_available' if compatibility['compatible'] else 'incompatible',
            manifest=manifest, installation=None, publisher_identity='qavector:bundled', compatibility=compatibility))
    for row in rows:
        row['delivery'] = 'installed' if row['installation'] is not None else 'bundled'
    for node in builtin_descriptions():
        rows.append(dict(plugin_id=node['id'], version=node['version'], state='available',
            effective_state='available', delivery='core_node', node=node,
            qavector_version=runtime_paths.application_version()))
    return sorted(rows, key=lambda row: (row['delivery'], row['plugin_id'], row['version']))


def manager():
    """The same host-owned store and bundled interpreter used by the CLI."""
    import runtime_paths
    from .installer import PluginManager
    from .managed_runtime import ManagedRuntime
    root = Path(bundled.registry_path()).parent
    descriptor = Path(runtime_paths.app_root()) / 'runtime' / 'plugin-python.json'
    runtime = ManagedRuntime.read(descriptor, root / 'runtime-state') if descriptor.is_file() else None
    return PluginManager(root, runtime)
