"""Opt-in frozen core import/run with no Python or pip available through PATH."""

import json
from pathlib import Path
import subprocess

import pytest

from plugin_host.managed_runtime import ManagedRuntime
from plugin_package_fixture import package
from execution_runtime.deployments import DeploymentRegistry, RuntimeDeployment


def test_frozen_import_catalog_and_cycle_without_system_python(request, tmp_path):
    executable = request.config.getoption('--runtime-frozen-core')
    if not executable:
        pytest.skip('Pass --runtime-frozen-core with bundled managed Python and native Runtime')
    core = Path(executable).absolute()
    home, project, cycles = tmp_path / 'data', tmp_path / 'project', tmp_path / 'cycles'
    for path in (home, project, cycles):
        path.mkdir()
    env = {'CMS_HOME': str(home), 'PATH': '/no-system-python-or-tools', 'LANG': 'C.UTF-8'}
    def call(*args):
        process = subprocess.run([str(core), *args], cwd=tmp_path, env=env,
            capture_output=True, text=True, timeout=60)
        assert process.returncode == 0, (process.stdout, process.stderr)
        return json.loads(process.stdout)
    payload = tmp_path / 'frozen.qavplug'
    payload.write_bytes(package(code='''
class Provider:
    async def execute(self, request, context, emit):
        return dict(schema_version='1.0.0', contribution_ref='example.demo.run', plugin_version='1.0.0',
            attempt_id=request['attempt_id'], status='completed', summary='frozen plugin',
            output={'count': len(request['inputs']['values'])}, artifact_refs=[], error=None)
'''))
    inspection = call('--plugins', 'inspect', str(payload))['result']
    assert inspection['runtime_ready']
    approval = tmp_path / 'approval.json'
    approval.write_text(json.dumps(inspection['approval_required']))
    call('--plugins', 'import', str(payload), '--approval', str(approval))
    call('--plugins', 'activate', 'example.demo', '1.0.0')
    assert call('--plugins', 'list')['result'][0]['state'] == 'active'
    managed = ManagedRuntime.read(core.parent / '_internal/runtime/plugin-python.json', tmp_path / 'state')
    deployment = RuntimeDeployment(dict(api_version='qavector.io/v1', kind='RuntimeDeployment',
        metadata={'name': 'plugins'}, spec=dict(provider='linux-isolated',
            policy={'resources': {'scratch_bytes': 16 * 1024 ** 2}},
            bindings=[{'name': 'project', 'source': str(project), 'target': '/workspace'}],
            working_directory='/', backend_options={'linux': {'state_directory': str(tmp_path / 'worker-state'),
                'image': {'identity': managed.image.identity, 'files': [vars(one) for one in managed.image.files]}}})))
    DeploymentRegistry(home / 'execution-deployments').save(deployment)
    document = dict(id='frozen', execution={'runtime': dict(mode='isolated', deployment='plugins',
        revision=deployment.digest, project_directory=str(project))}, steps=[{'id': 'count',
            'plugin': 'example.demo.run', 'with': {'inputs': {'values': [1, 2, 3]}}}])
    (cycles / 'frozen.yaml').write_text(json.dumps(document))
    inventory = call('--describe', '--cycles-dir=' + str(cycles))
    assert any(item['id'] == 'example.demo.run' for item in inventory['cycle_plugins'])
    process = subprocess.run([str(core), '--cycle-run=frozen', '--cycles-dir=' + str(cycles), '--events=-'],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert process.returncode == 0, (process.stdout, process.stderr)
    events = [json.loads(line) for line in process.stdout.splitlines()]
    result = next(item for item in events if item['kind'] == 'cycle.step.end')
    assert result['outputs']['output'] == {'count': 3}
