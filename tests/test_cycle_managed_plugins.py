"""Real package import -> Cycle catalog -> isolated worker -> exact Resume."""

import json
from pathlib import Path
import sys
import threading
import time

import pytest

from cycle import executor, model, registry, runtime, checkpoints
from cycle.context import CancelToken
from plugin_host import bundled, catalog
from plugin_host.installer import PluginManager
from execution_runtime.backends.linux import LinuxBackend
from execution_runtime.deployments import RuntimeDeployment, DeploymentRegistry
from execution_runtime.registry import EnvironmentRegistry
from plugin_package_fixture import package, package_files
from test_plugin_installer_native import manager, native, git_workload_image

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Managed Linux runtime')

NODE = '''
import os
class Provider:
    async def execute(self, request, context, emit):
        assert 'QAVECTOR_HOST_SECRET' not in os.environ
        try:
            child = os.fork()
        except OSError:
            pass
        else:
            if not child: os._exit(0)
            os.waitpid(child, 0)
            raise AssertionError('unexpected fork')
        return dict(schema_version='1.0.0', attempt_id=request['attempt_id'],
            contribution_ref='example.demo.run', plugin_version='VERSION', status='completed',
            summary='counted', output={'count': len(request['inputs']['values'])}, artifact_refs=[], error=None)
'''


@pytest.fixture
def installed(manager, tmp_path, monkeypatch):
    monkeypatch.setattr(bundled, 'registry_path', lambda: str(manager.root / 'registry.sqlite3'))
    monkeypatch.setattr(catalog, 'manager', lambda: PluginManager(manager.root, manager.runtime))
    project = tmp_path / 'project'
    project.mkdir()
    deployments, profiles = DeploymentRegistry(tmp_path / 'deployments'), EnvironmentRegistry(tmp_path / 'profiles')
    deployment = deployments.save(RuntimeDeployment(dict(api_version='qavector.io/v1', kind='RuntimeDeployment',
        metadata={'name': 'plugins'}, spec=dict(provider='linux-isolated',
            policy={'filesystem': [{'binding': 'project', 'access': 'read_write'}],
                    'resources': {'scratch_bytes': 16 * 1024 ** 2}},
            bindings=[{'name': 'project', 'source': str(project), 'target': '/workspace'}],
            working_directory='/workspace', backend_options={'linux': {}}))))
    monkeypatch.setattr(runtime, 'DeploymentRegistry', lambda: deployments)
    monkeypatch.setattr(runtime, 'EnvironmentRegistry', lambda: profiles)
    def backend(_deployment, protected=()):
        result = manager.runtime.backend_factory(manager.runtime.image, tmp_path / 'worker-state')
        result.protected_paths = tuple(protected)
        return result
    monkeypatch.setattr(RuntimeDeployment, 'backend', backend)
    config = dict(mode='isolated', deployment='plugins', revision=deployment.digest, project_directory=str(project))

    def install(version='1.0.0', code=None, kind='node_provider', permissions=None, prerequisites=None):
        path = tmp_path / 'demo.qavplug'
        files = package_files(version=version, code=code or NODE.replace('VERSION', version), kind=kind, permissions=permissions)
        files['schemas/input.json'] = b'{"type":"object","required":["values"],"properties":{"values":{"type":"array"}}}'
        if prerequisites:
            manifest = json.loads(files['manifest.json'])
            manifest['external_requirements'] = [dict(id=name, kind='executable', default_command=pin['path'])
                                                 for name, pin in prerequisites.items()]
            files['manifest.json'] = json.dumps(manifest).encode()
        path.write_bytes(package(files))
        manager.install(path, manager.inspect(path)['approval_required'], activate=True, prerequisites=prerequisites)
    install()
    return manager, config, install


def workflow(runtime_config, **settings):
    return model.parse_cycle(dict(id='managed', execution={'runtime': runtime_config}, steps=[
        dict(id='count', plugin='example.demo.run', **{'with': dict(inputs={'values': [1, 2, 3]}, **settings)})]), 'managed')


def test_import_catalog_run_update_disable_resume(installed, tmp_path, monkeypatch):
    manager, config, install = installed
    monkeypatch.setenv('QAVECTOR_HOST_SECRET', 'do-not-expose')
    assert 'demo' not in sys.modules
    assert registry.get('example.demo.run').catalog['effective_state'] == 'active'
    assert manager.describe('example.demo', '1.0.0')['schemas']['schemas/input.json']['required'] == ['values']
    cycle = workflow(config)
    document = model.to_document(cycle)
    document['steps'].append({'id': 'after', 'plugin': 'command.shell', 'needs': ['count'],
                             'with': {'command': ['/bin/busybox', 'test', '-f', '/workspace/ready']}})
    cycle = model.parse_cycle(document, 'managed')
    assert not model.problems(cycle)
    run = executor.run_cycle(cycle, str(tmp_path / 'run'))
    assert run.steps['count'].status == 'success', run.steps['count'].message
    assert run.status == 'failed'
    assert run.steps['count'].outputs['output'] == {'count': 3}
    assert 'demo' not in sys.modules
    install('1.1.0')
    manager.disable('example.demo', '1.0.0')
    (Path(config['project_directory']) / 'ready').touch()
    resumed = executor.run_cycle(cycle, str(tmp_path / 'run'), resume=True)
    assert resumed.status == 'success'
    assert resumed.steps['count'].outputs['node_result']['plugin_version'] == '1.0.0'
    new = executor.run_cycle(cycle, str(tmp_path / 'new'))
    assert new.steps['count'].outputs['node_result']['plugin_version'] == '1.1.0'
    manager.registry.set_state('example.demo', '1.0.0', 'quarantined')
    with pytest.raises(checkpoints.CheckpointError, match='cannot resume'):
        executor.run_cycle(cycle, str(tmp_path / 'run'), resume=True)


def test_invalid_config_never_calls_payload(installed, tmp_path):
    _, config, _ = installed
    run = executor.run_cycle(workflow(config, config={'not_allowed': True}), str(tmp_path / 'invalid'))
    assert run.status == 'failed'
    assert not list((tmp_path / 'invalid').rglob('attempts.sqlite3'))


def test_profile_selects_installed_backend(installed, tmp_path):
    _, config, install = installed
    code = Path('tests/fixtures/plugin_providers.py').read_text().split('class FakeAgent:')[1]
    code = 'class Provider:' + code.replace('example.agent.review', 'example.demo.run')
    # Replace the original package's contribution kind in a distinct version.
    install('1.1.0', code=code.replace('"1.0.0",\n                "status"', '"1.1.0",\n                "status"'), kind='agent_backend')
    cycle = model.parse_cycle(dict(id='agent', execution={'runtime': config}, agent_profiles={
        'agent.tester': {'backend_ref': 'example.demo.run', 'instructions': 'Check the result'}}, steps=[
        {'id': 'review', 'plugin': 'agent', 'with': {'agent_profile': 'agent.tester', 'task': 'Review'}}]), 'agent')
    run = executor.run_cycle(cycle, str(tmp_path / 'agent'))
    assert run.status == 'success', run.steps['review'].message
    assert run.steps['review'].outputs['agent_result']['backend_ref'] == 'example.demo.run'


def test_package_grant_narrows_writable_deployment_to_read_only(installed, tmp_path):
    _, config, install = installed
    project = Path(config['project_directory'])
    (project / 'allowed.txt').write_text('readable')
    checks = '''        from qavector_sdk.worker import host_call
        from qavector_sdk import ContractError
        import pathlib, socket
        resolved = await host_call(context['runtime_handle'], 'workspace.resolve', {'workspace_ref': request['workspace_ref']})
        assert resolved == {'path': '/workspace', 'access': 'read'}
        assert pathlib.Path('/workspace/allowed.txt').read_text() == 'readable'
        try:
            pathlib.Path('/workspace/forbidden.txt').write_text('no')
        except OSError:
            pass
        else:
            raise AssertionError('write escaped read grant')
        try:
            socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except OSError:
            pass
        else:
            raise AssertionError('socket escaped offline policy')
        try:
            await host_call(context['runtime_handle'], 'workspace.resolve', {'workspace_ref': 'ungranted'})
        except ContractError:
            pass
        else:
            raise AssertionError('ungranted workspace resolved')
'''
    code = NODE.replace('VERSION', '1.1.0').replace("        assert 'QAVECTOR_HOST_SECRET'", checks + "        assert 'QAVECTOR_HOST_SECRET'")
    install('1.1.0', code=code, permissions={'workspace': 'read', 'process': [], 'network': 'none', 'credentials': []})
    run = executor.run_cycle(workflow(config), str(tmp_path / 'read-only'))
    assert run.status == 'success', run.steps['count'].message
    assert not (project / 'forbidden.txt').exists()
    assert run.steps['count'].runtime['cleanup_confirmed']


def test_cancel_and_deadline_reap_worker(installed, tmp_path, monkeypatch):
    _, config, install = installed
    install('1.1.0', code=NODE.replace('VERSION', '1.1.0').replace(
        "        assert 'QAVECTOR_HOST_SECRET'", "        import asyncio\n        await asyncio.sleep(30)\n        assert 'QAVECTOR_HOST_SECRET'"))
    cycle = workflow(config)
    cancel = CancelToken()
    from cycle import runtime_plugin
    original = runtime_plugin.run_plugin
    timers = []
    def cancelling(runtime, *args, **kwargs):
        start = runtime.start
        def started(handle):
            result = start(handle)
            timer = threading.Timer(2, lambda: cancel.set('user cancelled'))
            timers.append(timer)
            timer.start()
            return result
        runtime.start = started
        return original(runtime, *args, **kwargs)
    monkeypatch.setattr(runtime_plugin, 'run_plugin', cancelling)
    try:
        before = time.monotonic()
        run = executor.run_cycle(cycle, str(tmp_path / 'cancel'), cancel=cancel)
        assert time.monotonic() - before < 25
        assert run.status == 'cancelled'
    finally:
        for timer in timers:
            timer.cancel()
        monkeypatch.setattr(runtime_plugin, 'run_plugin', original)
    documents = [json.loads(path.read_text()) for path in (tmp_path / 'cancel').rglob('receipt.json')]
    assert documents and all(doc['state'] == 'complete' for doc in documents)
    assert documents[0]['answer']['outputs']['node_result']['status'] == 'cancelled'
    cycle.steps[0].timeout = 1
    run = executor.run_cycle(cycle, str(tmp_path / 'timeout'))
    assert run.steps['count'].status == 'timeout'
