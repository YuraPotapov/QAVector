"""Discovery and lock handoff remain data-only, including unavailable versions."""

import json

import pytest

from plugin_host import bundled, catalog
from plugin_host.installer import PluginManager
from qavector_sdk import ContractError
from execution_runtime.backends.linux.filesystem import RuntimeImage
from cycle import registry, model, checkpoints
from cycle.plugin_plan import CyclePluginPlan
from plugin_package_fixture import package
from test_plugin_installer import Runtime


@pytest.fixture
def manager(tmp_path, monkeypatch):
    runtime = Runtime()
    base = tmp_path / 'base.py'
    base.write_text('reviewed base')
    runtime.image = RuntimeImage.from_files([(base, '/base.py', False)])
    runtime.worker_argv = lambda: ['/python', 'worker']
    runtime.execution_image = lambda directory: RuntimeImage.from_files(
        [(base, '/base.py', False)] + [(path, '/plugins/' + path.name, False) for path in directory.rglob('*') if path.is_file()])
    host = PluginManager(tmp_path / 'store', runtime)
    monkeypatch.setattr(bundled, 'registry_path', lambda: str(host.root / 'registry.sqlite3'))
    monkeypatch.setattr(catalog, 'manager', lambda: PluginManager(host.root, runtime))
    yield host
    host.close()


def install(manager, tmp_path, version='1.0.0'):
    path = tmp_path / 'sample.qavplug'
    path.write_bytes(package(version=version, code='raise RuntimeError("must never import on host")\nclass Provider: pass'))
    manager.install(path, manager.inspect(path)['approval_required'], activate=True)


def test_empty_discovery_does_not_create_store(tmp_path):
    path = tmp_path / 'missing' / 'registry.sqlite3'
    assert catalog.contributions(path) == []
    assert not path.parent.exists()


def test_unified_catalog_shows_core_without_registering_fake_packages(manager):
    rows = catalog.inventory(manager)
    command = next(row for row in rows if row['plugin_id'] == 'command.shell')
    assert command['delivery'] == 'core_node'
    assert command['node']['inputs']
    assert 'manifest' not in command
    claude = next(row for row in rows if row['plugin_id'] == 'qavector.agent.claude_cli')
    assert claude['delivery'] == 'bundled' and claude['state'] == 'bundled_available'
    assert manager.list() == []  # Display does not change active pointers or import code.


def test_current_bundled_backend_can_be_selected_after_core_upgrade(manager):
    old = bundled.claude_manifest()
    old['version'] = '1.0.1'
    manager.registry.register(old, 'a' * 64, publisher_identity='qavector:bundled', bundled=True,
        activate_initial=True, aliases={'claude_cli': old['contributions'][0]['id']})
    binding = bundled.register_claude(manager.registry)
    assert manager.registry.resolve('claude_cli')['version'] == '1.0.1'
    manager.activate(binding['plugin_id'], binding['version'])
    assert manager.registry.resolve('claude_cli') == binding
    with pytest.raises(ContractError, match='bundled in this core'):
        manager.activate(binding['plugin_id'], '1.0.1')


def test_catalog_refresh_and_disabled_visibility(manager, tmp_path):
    install(manager, tmp_path)
    assert registry.get('example.demo.run').catalog['version'] == '1.0.0'
    install(manager, tmp_path, '1.1.0')
    assert registry.get('example.demo.run').catalog['version'] == '1.1.0'
    manager.disable('example.demo', '1.1.0')
    assert registry.get('example.demo.run').catalog['effective_state'] == 'disabled'
    assert manager.describe('example.demo', '1.0.0')['schemas']['schemas/config.json']['type'] == 'object'


def test_locked_handoff_preserves_disabled_version_and_quarantine_revokes(manager, tmp_path):
    install(manager, tmp_path)
    requirements = {'example.demo.run': {'constraint': '>=1.0.0,<2.0.0'}}
    lock = manager.registry.lock_run('original-run', requirements)
    original = manager.locked_worker_spec(lock, 'example.demo.run')
    install(manager, tmp_path, '1.1.0')
    manager.disable('example.demo', '1.0.0')
    assert manager.locked_worker_spec(lock, 'example.demo.run') == original
    assert manager.worker_spec('example.demo.run')['binding']['version'] == '1.1.0'
    manager.registry.set_state('example.demo', '1.0.0', 'quarantined')
    with pytest.raises(ContractError, match='cannot resume'):
        manager.locked_worker_spec(lock, 'example.demo.run')


def test_no_host_fallback_and_no_replacement_for_missing_lock(manager, tmp_path):
    install(manager, tmp_path)
    document = {'id': 'demo', 'steps': [{'id': 'one', 'plugin': 'example.demo.run'}]}
    cycle = model.parse_cycle(document, 'demo')
    store = checkpoints.Store(str(tmp_path / 'run'))
    with pytest.raises(checkpoints.CheckpointError, match='require execution.runtime'):
        CyclePluginPlan(cycle, registry, store)
    document['execution'] = {'runtime': {'mode': 'isolated', 'deployment': 'test',
        'revision': 'a' * 64, 'project_directory': '/approved'}}
    cycle = model.parse_cycle(document, 'demo')
    plan = CyclePluginPlan(cycle, registry, store)
    store.write(store.directory + '/manifest.json', {'plugins': plan.bindings})
    manager.registry.db.execute('DELETE FROM run_locks')
    with pytest.raises(checkpoints.CheckpointError, match='lock is missing'):
        CyclePluginPlan(cycle, registry, store, resume=True)
