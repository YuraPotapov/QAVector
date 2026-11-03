"""Host release ranges and plugin version locks are independent constraints."""

import json

import pytest

import runtime_paths
from plugin_host.compatibility import report
from plugin_host.installer import PluginManager
from plugin_host.registry import PluginRegistry
from qavector_sdk import ContractError, validate_contract
from test_plugin_registry import manifest
from test_plugin_installer import Runtime
from plugin_package_fixture import package, package_files


@pytest.mark.parametrize('current,compatible', [
    ('0.16.47', False), ('0.16.48', True), ('0.16.99', True), ('0.17.0', False),
    ('dev (not installed)', False), ('', False), ('0.16.48rc1', False),
])
def test_host_range_boundaries_and_unknown_release(monkeypatch, current, compatible):
    monkeypatch.setattr(runtime_paths, 'application_version', lambda: current)
    result = report(manifest())
    assert result['compatible'] is compatible
    if not compatible:
        assert result['issues'][0]['component'] == 'qavector'
        assert result['issues'][0]['code'] == 'QAVECTOR_VERSION_INCOMPATIBLE'


def test_api_and_application_ranges_are_both_required(monkeypatch):
    monkeypatch.setattr(runtime_paths, 'application_version', lambda: '0.16.48')
    value = manifest()
    value['compatibility']['plugin_api'] = '>=2.0.0'
    result = report(value)
    assert not result['compatible']
    assert result['issues'][0]['component'] == 'plugin_api'
    assert result['issues'][0]['actual'] == '1.0.0'
    del value['compatibility']['qavector']
    assert {one['component'] for one in report(value)['issues']} == {'plugin_api', 'qavector'}


@pytest.mark.parametrize('constraint', ['^0.16.48', '0.16.48', '>=0.16', '*', '>=0.16.48, <0.17.0'])
def test_invalid_application_constraint_is_rejected_by_schema(constraint):
    value = manifest(); value['compatibility']['qavector'] = constraint
    with pytest.raises(ContractError):
        validate_contract('qavplug-manifest', value)


def test_upgrade_downgrade_and_resume_never_substitute_a_version(tmp_path, monkeypatch):
    current = ['0.16.48']
    monkeypatch.setattr(runtime_paths, 'application_version', lambda: current[0])
    catalog = PluginRegistry(tmp_path / 'catalog.db')
    try:
        old = manifest()
        reference = old['contributions'][0]['id']
        catalog.register(old, 'a' * 64, publisher_identity='test')
        catalog.set_state(old['plugin_id'], old['version'], 'active')
        requirements = {reference: {'constraint': '>=1.0.0,<2.0.0'}}
        lock = catalog.lock_run('original-run', requirements)
        current[0] = '0.17.0'
        for operation in (lambda: catalog.resolve(reference),
                          lambda: catalog.set_state(old['plugin_id'], old['version'], 'active'),
                          lambda: catalog.lock_run('original-run', requirements)):
            with pytest.raises(ContractError) as caught:
                operation()
            assert caught.value.code == 'QAVECTOR_VERSION_INCOMPATIBLE'
        new = manifest('1.1.0')
        new['compatibility']['qavector'] = '>=0.17.0,<0.18.0'
        catalog.register(new, 'b' * 64, publisher_identity='test')
        catalog.set_state(new['plugin_id'], new['version'], 'active')
        assert catalog.resolve(reference)['version'] == '1.1.0'
        with pytest.raises(ContractError):
            catalog.lock_run('original-run', requirements)
        persisted = json.loads(catalog.db.execute('SELECT document FROM run_locks').fetchone()[0])
        assert persisted == lock
        current[0] = '0.16.48'
        with pytest.raises(ContractError):
            catalog.resolve(reference)  # no automatic fallback to available 1.0.0
        catalog.set_state(old['plugin_id'], old['version'], 'active')
        assert catalog.lock_run('original-run', requirements) == lock
    finally:
        catalog.close()


def test_inspection_diagnoses_incompatibility_before_runtime_or_trust_approval(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_paths, 'application_version', lambda: '0.17.0')
    manager = PluginManager(tmp_path / 'store', Runtime())
    try:
        path = tmp_path / 'package.qavplug'; path.write_bytes(package())
        info = manager.inspect(path)
        assert not info['compatibility']['compatible']
        assert info['approval_required'] is None
        with pytest.raises(ContractError, match='qavector requires'):
            manager.install(path, {})
        assert manager.runtime.calls == 0 and manager.list() == []
    finally:
        manager.close()


def test_legacy_missing_range_is_visible_but_not_executable(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_paths, 'application_version', lambda: '0.16.48')
    manager = PluginManager(tmp_path / 'store')
    try:
        files = package_files(); value = json.loads(files['manifest.json'])
        del value['compatibility']['qavector']; files['manifest.json'] = json.dumps(value).encode()
        path = tmp_path / 'legacy.qavplug'; path.write_bytes(package(files))
        info = manager.inspect(path)
        assert info['compatibility']['issues'][0]['required'] is None
        assert info['approval_required'] is None
        # Historic registry rows remain readable after an application update.
        manager.registry.db.execute('INSERT INTO packages VALUES (?,?,?,?,?,?)',
            (value['plugin_id'], value['version'], 'a' * 64, json.dumps(value), 'disabled', 'local:unsigned'))
        item = manager.list()[0]
        assert item['state'] == 'disabled' and item['effective_state'] == 'incompatible'
        assert not manager.check()['compatible']
    finally:
        manager.close()


def test_cli_check_works_without_a_worker_image_and_reports_upgrade(tmp_path, monkeypatch, capsys):
    from plugin_host.cli import main
    root = tmp_path / 'store'
    monkeypatch.setattr(runtime_paths, 'application_version', lambda: '0.16.48')
    manager = PluginManager(root, Runtime())
    path = tmp_path / 'package.qavplug'; path.write_bytes(package())
    manager.install(path, manager.inspect(path)['approval_required'], activate=True)
    manager.close()
    assert main(['--root', str(root), 'check']) == 0
    assert json.loads(capsys.readouterr().out)['result']['compatible']
    monkeypatch.setattr(runtime_paths, 'application_version', lambda: '0.17.0')
    assert main(['--root', str(root), '--runtime', str(tmp_path / 'absent'), 'check']) == 2
    payload = json.loads(capsys.readouterr().out)
    item = payload['result']['packages'][0]
    assert not payload['ok'] and item['effective_state'] == 'incompatible'
    assert item['state'] == 'active'  # persisted selection is never rewritten by listing
    assert item['compatibility']['host']['qavector'] == '0.17.0'
    assert main(['--root', str(root), 'inspect', str(path)]) == 2
    assert json.loads(capsys.readouterr().out)['result']['approval_required'] is None


def test_release_identity_uses_checkout_then_distribution(monkeypatch, tmp_path):
    import importlib.metadata
    monkeypatch.setattr(runtime_paths, 'FROZEN', False)
    monkeypatch.setattr(runtime_paths, '__file__', str(tmp_path / 'runtime_paths.py'))
    monkeypatch.setattr(importlib.metadata, 'version', lambda _: '9.9.9')
    project = tmp_path / 'pyproject.toml'
    project.write_text('[project]\nversion = "0.16.48"\n[other]\nversion = "7.0.0"\n')
    assert runtime_paths.application_version() == '0.16.48'
    project.unlink()
    assert runtime_paths.application_version() == '9.9.9'


def test_frozen_release_never_uses_host_distribution(monkeypatch):
    import importlib.metadata
    monkeypatch.setattr(runtime_paths, 'FROZEN', True)
    monkeypatch.setattr(importlib.metadata, 'version', lambda _: pytest.fail('No host metadata in frozen build'))
    monkeypatch.setattr(runtime_paths, 'bundled_version', lambda: '0.16.48')
    assert runtime_paths.application_version() == '0.16.48'
    monkeypatch.setattr(runtime_paths, 'bundled_version', lambda: '')
    assert report(manifest())['host']['qavector'] is None


def test_version_error_uses_public_rpc_error_contract():
    from qavector_sdk.ipc import decode, encode, error_response
    message = error_response('check-version', 'QAVECTOR_VERSION_INCOMPATIBLE',
                             'qavector requires >=0.17.0; running 0.16.48')
    assert decode(encode(message)) == message
