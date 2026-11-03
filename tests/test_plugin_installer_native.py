"""Imported wheel code runs only inside the immutable offline Linux boundary."""

import json
from pathlib import Path
import sys

import pytest
from packaging.markers import default_environment
from packaging.tags import sys_tags

from plugin_host.installer import PluginManager
from plugin_host.managed_runtime import ManagedRuntime
from qavector_sdk import ContractError
from plugin_package_fixture import package

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed runtime only")

if sys.platform == 'linux':
    from test_execution_runtime_linux import native, git_workload_image


@pytest.fixture
def manager(native, git_workload_image, tmp_path):
    from execution_runtime.backends.linux import LinuxBackend
    from tests.runtime_plugin_image import plugin_image
    image, git = git_workload_image
    image, _ = plugin_image(tmp_path, image, git['argv'][0])
    descriptor = dict(format_version=1, target='linux-x86_64-cp310', python_version='3.10.12',
        tags=[str(tag) for tag in sys_tags()], markers=default_environment(), sdk_version='1.0.0',
        interpreter=git['argv'][0], image={'identity': image.identity, 'files': [vars(one) for one in image.files]})
    def factory(image, state):
        return LinuxBackend(image, state, launcher=native[0].launcher, bundle_directory=native[0]._bundle.directory)
    runtime = ManagedRuntime(descriptor, tmp_path / 'runtime-state', backend_factory=factory)
    manager = PluginManager(tmp_path / 'plugin store', runtime)
    yield manager
    manager.close()


@pytest.mark.parametrize('kind', ['node_provider', 'agent_backend'])
def test_import_healthcheck_is_offline_and_has_no_host_environment(manager, tmp_path, monkeypatch, kind):
    monkeypatch.setenv('QAVECTOR_HOST_SECRET', 'not-for-plugin')
    code = '''import os, socket
assert 'QAVECTOR_HOST_SECRET' not in os.environ
import resource
assert resource.getrlimit(resource.RLIMIT_AS) == (512 * 1024 ** 2, 512 * 1024 ** 2)
assert resource.getrlimit(resource.RLIMIT_NPROC) == (0, 0)
try:
    child = os.fork()
except OSError:
    pass
else:
    if not child:
        os._exit(0)
    os.waitpid(child, 0)
    raise AssertionError('healthcheck could create a process')
for family, kind in ((socket.AF_INET, socket.SOCK_STREAM), (socket.AF_UNIX, socket.SOCK_STREAM)):
    try:
        sock = socket.socket(family, kind)
    except OSError:
        pass
    else:
        sock.close()
        raise AssertionError('socket escaped policy')
try:
    open('/qavector/plugins/demo.py', 'w')
except OSError:
    pass
else:
    raise AssertionError('installed code writable')
class Provider:
    pass
'''
    path = tmp_path / 'reviewed.qavplug'
    path.write_bytes(package(kind=kind, code=code))
    result = manager.install(path, manager.inspect(path)['approval_required'], activate=True)
    assert result['healthcheck']['status'] == 'passed'
    assert result['healthcheck']['provider_request'] == 'not_sent'
    assert manager.registry.resolve('example.demo.run')['digest'] == result['digest']


def test_malicious_import_cannot_write_host_or_replace_active_version(manager, tmp_path):
    path = tmp_path / 'plugin.qavplug'
    path.write_bytes(package())
    manager.install(path, manager.inspect(path)['approval_required'], activate=True)
    marker = tmp_path / 'host-file'
    path.write_bytes(package(version='1.1.0', code='open(%r, "w").write("attack")\nclass Provider: pass\n' % str(marker)))
    with pytest.raises(ContractError, match='healthcheck'):
        manager.install(path, manager.inspect(path)['approval_required'], activate=True)
    assert not marker.exists()
    assert manager.registry.resolve('example.demo.run')['version'] == '1.0.0'
    assert len(manager.list()) == 1


def test_prepared_python_bundle_is_relocatable(native, tmp_path):
    import shutil
    from examples.plugin_contracts.prepare_runtime import build
    from execution_runtime.backends.linux import LinuxBackend
    descriptor = build(tmp_path / 'original', '/usr/bin/busybox')
    def factory(image, state):
        return LinuxBackend(image, state, launcher=native[0].launcher, bundle_directory=native[0]._bundle.directory)
    first = ManagedRuntime.read(descriptor, tmp_path / 'state', backend_factory=factory)
    shutil.move(str(descriptor.parent), str(tmp_path / 'relocated runtime'))
    runtime = ManagedRuntime.read(tmp_path / 'relocated runtime' / 'plugin-python.json', tmp_path / 'state', backend_factory=factory)
    assert runtime.identity == first.identity
    manager = PluginManager(tmp_path / 'plugins', runtime)
    try:
        path = tmp_path / 'plugin.qavplug'
        path.write_bytes(package(code='from qavector_sdk import NodeProvider\nclass Provider: pass\n'))
        result = manager.install(path, manager.inspect(path)['approval_required'], activate=True)
        spec = manager.worker_spec('example.demo.run')
        assert spec['runtime_id'] == runtime.identity
        assert result['healthcheck']['status'] == 'passed'
        assert '-S' in spec['argv']
    finally:
        manager.close()
