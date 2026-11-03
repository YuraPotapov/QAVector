import base64
import io
import json
from pathlib import Path
import threading
import zipfile

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from plugin_host.archive import Package, zip_files, ArchiveLimits
from plugin_host.installer import PluginManager
from plugin_host.trust import PREFIX, assess
from plugin_host.wheels import environment
from qavector_sdk import ContractError
from plugin_package_fixture import package, package_files, wheel, zipped, runtime_document


class Runtime:
    document = runtime_document()
    identity = 'b' * 64
    def __init__(self):
        self.fail = False
        self.calls = 0
    def healthcheck(self, directory, contributions, cancel):
        self.calls += 1
        assert (directory / 'demo.py').is_file()
        if self.fail:
            raise ContractError('PLUGIN_HEALTHCHECK_FAILED', 'fixture failure')
        return {'status': 'passed'}
    def execution_image(self, directory):
        pass


@pytest.fixture
def manager(tmp_path):
    manager = PluginManager(tmp_path / 'Área plugins', Runtime())
    yield manager
    manager.close()


def source(tmp_path, raw):
    path = tmp_path / 'plugin.qavplug'
    path.write_bytes(raw)
    return path


def install(manager, tmp_path, **kwargs):
    path = source(tmp_path, package(**kwargs))
    return manager.install(path, manager.inspect(path)['approval_required'])


@pytest.mark.parametrize('kind', ['node_provider', 'agent_backend'])
def test_both_kinds_install_without_host_import(manager, tmp_path, kind):
    marker = tmp_path / 'must-not-exist'
    install(manager, tmp_path, kind=kind, code='from pathlib import Path\nPath(%r).touch()\nclass Provider: pass\n' % str(marker))
    assert not marker.exists()
    assert manager.list()[0]['state'] == 'installed_inactive'
    manager.activate('example.demo', '1.0.0')
    assert manager.registry.resolve('example.demo.run')['version'] == '1.0.0'


def test_update_rollback_run_lock_and_removal_guard(manager, tmp_path):
    first = install(manager, tmp_path)
    manager.activate('example.demo', '1.0.0')
    requirements = {'example.demo.run': {'constraint': '>=1.0.0'}}
    pinned = manager.registry.lock_run('run-1', requirements)
    install(manager, tmp_path, version='1.1.0')
    manager.activate('example.demo', '1.1.0')
    assert manager.registry.lock_run('run-1', requirements) == pinned
    assert manager.registry.resolve('example.demo.run')['version'] == '1.1.0'
    manager.activate('example.demo', '1.0.0')
    manager.disable('example.demo', '1.0.0')
    assert manager.registry.lock_run('run-1', requirements) == pinned
    with pytest.raises(ContractError, match='run-1'):
        manager.remove('example.demo', '1.0.0')
    manager.remove('example.demo', '1.1.0')
    assert len(manager.list()) == 1
    assert manager._paths(first)[0].is_dir()


def test_idempotence_conflict_and_failed_update_preserve_active(manager, tmp_path):
    install(manager, tmp_path)
    manager.activate('example.demo', '1.0.0')
    install(manager, tmp_path)
    assert manager.runtime.calls == 1
    with pytest.raises(ContractError, match='digest'):
        install(manager, tmp_path, code='class Provider: changed=True\n')
    manager.runtime.fail = True
    with pytest.raises(ContractError):
        install(manager, tmp_path, version='1.1.0')
    assert manager.registry.resolve('example.demo.run')['version'] == '1.0.0'
    assert len(manager.list()) == 1
    assert not list((manager.root / 'staging').iterdir())
    assert manager.diagnostics()[0]['status'] == 'failed'


def test_cancelled_install_and_orphan_recovery(manager, tmp_path):
    path = source(tmp_path, package())
    cancel = threading.Event(); cancel.set()
    with pytest.raises(ContractError, match='cancelled'):
        manager.install(path, manager.inspect(path)['approval_required'], cancel=cancel)
    orphan = manager.root / 'packages' / ('a' * 64)
    orphan.mkdir(); (orphan / 'incomplete').write_text('interrupted')
    manager.recover()
    assert not orphan.exists() and not manager.list()


def test_grants_are_bound_to_digest_and_never_inherited(manager, tmp_path):
    path = source(tmp_path, package())
    approval = manager.inspect(path)['approval_required']
    path.write_bytes(package(version='1.1.0', permissions=dict(workspace='write', process=['shell'], network='none', credentials=[])))
    with pytest.raises(ContractError):
        manager.install(path, approval)
    assert manager.runtime.calls == 0


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'C:/drive', 'a\\b', 'CON.txt', 'x/../y', 'x.', 'x//y'])
def test_unsafe_zip_paths(name):
    with pytest.raises(ContractError):
        zip_files(zipped({name: b'payload'}))


def test_zip_case_collision_symlinks_and_ratio():
    with pytest.raises(ContractError):
        zip_files(zipped({'a': b'x', 'A': b'y'}))
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        entry = zipfile.ZipInfo('link'); entry.external_attr = 0o120777 << 16
        archive.writestr(entry, 'outside')
    with pytest.raises(ContractError):
        zip_files(output.getvalue())
    with pytest.raises(ContractError):
        zip_files(zipped({'bomb': b'0' * 10000}), ArchiveLimits(ratio=2))


def test_checksums_and_duplicate_json_are_rejected():
    raw = package()
    files = zip_files(raw)
    files['manifest.json'] += b' '
    with pytest.raises(ContractError):
        Package(zipped(files))
    files = package_files(); files['manifest.json'] = b'{"format_version":1,"format_version":1}'
    with pytest.raises(ContractError):
        Package(package(files))


def test_signature_is_over_exact_inventory_and_host_key(manager, tmp_path):
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    public = base64.b64encode(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    files = zip_files(package())
    files['signature.json'] = json.dumps({'algorithm': 'Ed25519', 'key_id': 'publisher',
        'signature': base64.b64encode(private.sign(PREFIX + files['checksums.json'])).decode()}).encode()
    path = source(tmp_path, zipped(files))
    with pytest.raises(ContractError):
        manager.inspect(path)
    manager.enroll_key('publisher', public)
    report = manager.inspect(path)
    assert report['trust']['status'] == 'verified_signature'
    manager.install(path, report['approval_required'])
    files['checksums.json'] += b' '
    path.write_bytes(zipped(files))
    with pytest.raises(ContractError, match='signature'):
        manager.inspect(path)


@pytest.mark.parametrize('extra', [{'../outside': b'x'}, {'bad.pth': b'import os'}, {'qavector_sdk/__init__.py': b'bad'}])
def test_nested_wheel_rejects_escape_startup_hooks_and_sdk_override(extra):
    files = package_files(wheels={'wheels/demo-1.0.0-py3-none-any.whl': wheel(extra=extra)})
    with pytest.raises(ContractError):
        environment(Package(package(files)), runtime_document())


def test_missing_dependency_and_version_mismatch():
    files = package_files(wheels={'wheels/demo-1.0.0-py3-none-any.whl': wheel(requires=['missing==1.0'])})
    with pytest.raises(ContractError, match='dependency'):
        environment(Package(package(files)), runtime_document())


def test_mutated_environment_cannot_activate(manager, tmp_path):
    record = install(manager, tmp_path)
    target = manager._paths(record)[1] / 'demo.py'
    target.chmod(0o600); target.write_text('modified')
    with pytest.raises(ContractError, match='modified'):
        manager.activate('example.demo', '1.0.0')


def test_zip_embedded_nul_is_rejected():
    raw = zipped({'x_bad': b'payload'}).replace(b'x_bad', b'x\x00bad')
    with pytest.raises(ContractError):
        zip_files(raw)


def test_publisher_ownership_survives_removal(manager, tmp_path):
    install(manager, tmp_path)
    manager.remove('example.demo', '1.0.0')
    from plugin_package_fixture import package_files
    manifest = json.loads(package_files()['manifest.json'])
    with pytest.raises(ContractError, match='identity'):
        manager.registry.register(manifest, 'c' * 64, publisher_identity='another-key')


def test_missing_runtime_never_uses_host_python(tmp_path):
    manager = PluginManager(tmp_path / 'plugins')
    try:
        path = source(tmp_path, package())
        report = manager.inspect(path)
        assert not report['runtime_ready']
        with pytest.raises(ContractError, match='managed runtime'):
            manager.install(path, report['approval_required'])
    finally:
        manager.close()


def test_cross_process_imports_are_serialized_and_idempotent(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = source(tmp_path, package())
    root = tmp_path / 'plugins'
    def one(_):
        manager = PluginManager(root, Runtime())
        try:
            return manager.install(path, manager.inspect(path)['approval_required'])['digest']
        finally:
            manager.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert len(set(pool.map(one, range(2)))) == 1


def test_nested_wheel_checksum_and_unlisted_files_fail():
    files = package_files()
    filename = next(path for path in files if path.endswith('.whl'))
    nested = zip_files(files[filename]); nested['demo.py'] += b'# tampered'
    files[filename] = zipped(nested)
    lock = json.loads(files['requirements.lock'])
    import hashlib
    lock['targets']['linux-x86_64-cp310'][0]['sha256'] = hashlib.sha256(files[filename]).hexdigest()
    files['requirements.lock'] = json.dumps(lock).encode()
    with pytest.raises(ContractError, match='RECORD'):
        environment(Package(package(files)), runtime_document())


def test_failure_after_filesystem_publish_recovers_without_activation(manager, tmp_path, monkeypatch):
    install(manager, tmp_path)
    manager.activate('example.demo', '1.0.0')
    original = Path.rename
    def interrupted(path, target):
        result = original(path, target)
        if path.name == 'environment':
            raise OSError('Simulated failure after rename')
        return result
    monkeypatch.setattr(Path, 'rename', interrupted)
    with pytest.raises(OSError):
        install(manager, tmp_path, version='1.1.0')
    assert manager.registry.resolve('example.demo.run')['version'] == '1.0.0'
    assert len(list((manager.root / 'packages').iterdir())) == 1
    assert len(list((manager.root / 'environments').iterdir())) == 1


def test_reference_retention_and_release(manager, tmp_path):
    record = install(manager, tmp_path)
    manager.retain('project', 'project-1', [{'plugin_id': 'example.demo', 'version': '1.0.0', 'digest': record['digest']}])
    with pytest.raises(ContractError, match='project-1'):
        manager.remove('example.demo', '1.0.0')
    manager.release_reference('project', 'project-1')
    manager.remove('example.demo', '1.0.0')
    assert not manager.list()


def test_external_executable_requires_explicit_image_pin(manager, tmp_path):
    from types import SimpleNamespace
    files = package_files()
    manifest = json.loads(files['manifest.json'])
    manifest['external_requirements'] = [{'id': 'tool', 'kind': 'executable', 'default_command': 'tool'}]
    files['manifest.json'] = json.dumps(manifest).encode()
    path = source(tmp_path, package(files))
    manager.install(path, manager.inspect(path)['approval_required'])
    with pytest.raises(ContractError, match='prerequisite'):
        manager.activate('example.demo', '1.0.0')
    manager.runtime.image = SimpleNamespace(files=[SimpleNamespace(target='/approved/tool', executable=True, sha256='c' * 64)])
    manager.activate('example.demo', '1.0.0', {'tool': {'path': '/approved/tool', 'sha256': 'c' * 64}})
    assert manager.registry.resolve('example.demo.run')['version'] == '1.0.0'


def test_cli_uses_json_approval_and_errors_without_runtime(tmp_path, capsys):
    from plugin_host.cli import main
    root = str(tmp_path / 'cli-store')
    path = source(tmp_path, package())
    assert main(['--root', root, 'inspect', str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    approval = tmp_path / 'approval.json'
    approval.write_text(json.dumps(report['result']['approval_required']))
    assert main(['--root', root, 'import', str(path), '--approval', str(approval)]) == 2
    assert json.loads(capsys.readouterr().out)['code'] == 'PLUGIN_RUNTIME_INCOMPATIBLE'


def test_official_namespace_is_rejected_before_healthcheck(manager, tmp_path):
    files = package_files()
    manifest = json.loads(files['manifest.json']); manifest['plugin_id'] = 'qavector.forged'
    files['manifest.json'] = json.dumps(manifest).encode()
    path = source(tmp_path, package(files))
    with pytest.raises(ContractError, match='Official'):
        manager.install(path, manager.inspect(path)['approval_required'])
    assert manager.runtime.calls == 0


def test_zip_implicit_directory_case_collision():
    with pytest.raises(ContractError):
        zip_files(zipped({'DIR/a': b'x', 'dir/b': b'y'}))
