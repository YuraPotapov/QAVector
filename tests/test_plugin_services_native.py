"""Installed packages call scoped services through real native Runtime workers."""

import json
from pathlib import Path
import threading
import time

import pytest

from cycle import executor, model, runtime, checkpoints
from cycle.context import CancelToken
from execution_runtime.deployments import RuntimeDeployment
from plugin_host.service_policy import NetworkRoute
from test_cycle_managed_plugins import installed, workflow, NODE
from test_plugin_installer_native import manager, native, git_workload_image
from test_execution_runtime_gateway import tls_gateway


def configure(config, services):
    deployments = runtime.DeploymentRegistry()
    previous = deployments.load('plugins')
    raw = previous.to_document()
    raw['spec']['plugin_services'] = {'example.demo.run': services}
    raw['spec']['policy'].update(secrets=list(services.get('credentials', {}).values()),
        tools=list(services.get('processes', {})), network={'mode': 'restricted',
            'endpoints': [NetworkRoute(route).endpoint for route in services.get('network', {}).values()]}
            if services.get('network') else {'mode': 'none'})
    config['revision'] = deployments.save(RuntimeDeployment(raw), previous.digest).digest


def code(checks):
    return NODE.replace('VERSION', '1.1.0').replace("        assert 'QAVECTOR_HOST_SECRET'",
        "        from qavector_sdk.worker import host_call\n        from qavector_sdk import ContractError\n" +
        checks + "        assert 'QAVECTOR_HOST_SECRET'")


def secret_cycle(config, **settings):
    doc = model.to_document(workflow(config, **settings))
    doc['variables'] = {'TOKEN': {'secret': True}}
    return model.parse_cycle(doc, 'managed')


def pin(manager):
    file = next(one for one in manager.runtime.image.files if one.target == '/bin/busybox')
    return {'tool': {'path': file.target, 'sha256': file.sha256}}


def https(raw):
    route = {key: raw[key] for key in ('endpoint', 'addresses', 'allow_private_addresses', 'ca_pem')}
    route.update(credential='account', models=['test-model'], timeout_seconds=2)
    return dict(network={'api': route}, credentials={'account': 'TOKEN'}, processes={})


def test_cycle_https_credentials_and_process_are_scoped(installed, tls_gateway, tmp_path):
    manager, config, install = installed
    raw, state, _ = tls_gateway
    services = https(raw)
    services['processes']['tool'] = dict(argv=['/bin/busybox', 'sh', '-c',
        'cat "$1"; if echo forbidden > "$1"; then exit 9; fi; printf tool-ok', 'tool', '{credential:account}'],
        credentials=['account'])
    configure(config, services)
    checks = '''        import base64, socket, pathlib
        assert 'TOKEN' not in os.environ
        assert not list(pathlib.Path('/run/qavector-secrets').glob('*'))
        try:
            socket.socket()
        except OSError:
            pass
        else:
            raise AssertionError('worker obtained a socket')
        description = await host_call(context['tools_handle'], 'services.describe', {})
        assert description == {'network': ['api'], 'processes': ['tool']}
        credentials = await host_call(context['credentials_handle'], 'credentials.describe', {})
        assert credentials == {'aliases': ['account'], 'delivery': 'scoped_service_only'}
        for handle, operation, args in [
            (context['credentials_handle'], 'credentials.read', {'alias': 'account'}),
            (context['runtime_handle'], 'network.request', {'route': 'api'}),
            (context['tools_handle'], 'process.run', {'tool': 'unapproved'}),
            (context['tools_handle'], 'network.request', {'route': 'api', 'body': {'model': 'other'}})]:
            try:
                await host_call(handle, operation, args)
            except ContractError:
                pass
            else:
                raise AssertionError('ungranted call succeeded')
        response = await host_call(context['tools_handle'], 'network.request', {'route': 'api', 'body': {'model': 'test-model'}})
        assert response['status'] == 200 and base64.b64decode(response['body_base64']) == b'{"result":"ok"}'
        tool = await host_call(context['tools_handle'], 'process.run', {'tool': 'tool'})
        assert tool['exit_code'] == 0, tool
        assert 'tool-ok' in tool['stdout'] and '[redacted]' in tool['stdout'], tool
'''
    install('1.1.0', code=code(checks), permissions=dict(workspace='read', network='policy_controlled',
        process=['tool'], credentials=['account']), prerequisites=pin(manager))
    run = executor.run_cycle(secret_cycle(config), str(tmp_path / 'run'), secrets={'TOKEN': 'synthetic-token'})
    assert run.status == 'success', run.steps['count'].message
    assert len(state['calls']) == 1 and state['calls'][0][1]['Authorization'] == 'Bearer synthetic-token'
    assert run.steps['count'].runtime['cleanup_confirmed']
    for path in (tmp_path / 'run').rglob('*'):
        if path.is_file():
            assert b'synthetic-token' not in path.read_bytes(), path


@pytest.mark.parametrize('incomplete', [False, True])
def test_resume_never_repeats_sent_http_call(installed, tls_gateway, tmp_path, incomplete):
    _, config, install = installed
    raw, state, _ = tls_gateway
    services = https(raw)
    configure(config, services)
    if incomplete:
        state['body_delay'] = 3
        services['network']['api']['timeout_seconds'] = 1
        configure(config, services)
    # Deliberately swallow service errors: the host must still enforce unknown.
    checks = '''        try:
            await host_call(context['tools_handle'], 'network.request', {'route': 'api', 'body': {'model': 'test-model'}})
        except ContractError:
            pass
'''
    install('1.1.0', code=code(checks), permissions=dict(workspace='none', network='policy_controlled',
        process=[], credentials=['account']))
    cycle = secret_cycle(config, output_schema={'type': 'string'})
    directory = str(tmp_path / 'run')
    run = executor.run_cycle(cycle, directory, secrets={'TOKEN': 'synthetic-token'})
    assert run.status == 'failed'
    assert len(state['calls']) == 1
    result = run.steps['count'].outputs['node_result']
    assert result['status'] == ('unknown' if incomplete else 'failed')
    if incomplete:
        with pytest.raises(checkpoints.CheckpointError, match='Uncertain outcome'):
            executor.run_cycle(cycle, directory, secrets={'TOKEN': 'synthetic-token'}, resume=True)
    else:
        resumed = executor.run_cycle(cycle, directory, secrets={'TOKEN': 'synthetic-token'}, resume=True)
        assert resumed.status == 'failed'
    assert len(state['calls']) == 1


def test_stop_reaps_brokered_process(installed, tmp_path, monkeypatch):
    manager, config, install = installed
    configure(config, dict(processes={'tool': {'argv': ['/bin/busybox', 'sleep']}}))
    install('1.1.0', code=code("        await host_call(context['tools_handle'], 'process.run', {'tool': 'tool', 'arguments': ['30']})\n"),
        permissions=dict(workspace='none', network='none', process=['tool'], credentials=[]), prerequisites=pin(manager))
    from plugin_host import services
    original = services.ExecutionRuntime
    cancel, timers, handles, started = CancelToken(), [], [], []
    class ObservedRuntime(original):
        def start(self, handle):
            result = super().start(handle)
            handles.append((self, result))
            started.append(time.monotonic())
            timer = threading.Timer(0.2, lambda: cancel.set('user stopped'))
            timers.append(timer)
            timer.start()
            return result
    monkeypatch.setattr(services, 'ExecutionRuntime', ObservedRuntime)
    try:
        run = executor.run_cycle(workflow(config), str(tmp_path / 'run'), cancel=cancel)
        assert run.status == 'cancelled'
        assert handles and all(rt.inspect(handle)['state'] == 'cleaned' for rt, handle in handles)
        assert time.monotonic() - started[0] < 5, 'Stop must not wait for the 30-second command'
        receipts = [json.loads(path.read_text()) for path in (tmp_path / 'run').rglob('receipt.json')]
        assert receipts and all(row['state'] == 'complete' for row in receipts)
    finally:
        for timer in timers:
            timer.cancel()


@pytest.mark.parametrize('unconfirmed', [False, True])
def test_tool_deadline_and_unconfirmed_cleanup(installed, tmp_path, monkeypatch, unconfirmed):
    manager, config, install = installed
    configure(config, dict(processes={'tool': {'argv': ['/bin/busybox', 'sleep'], 'timeout_seconds': 1}}))
    checks = '''        try:
            result = await host_call(context['tools_handle'], 'process.run', {'tool': 'tool', 'arguments': ['5']})
            assert result['exit_code'] != 0
        except ContractError:
            pass
'''
    install('1.1.0', code=code(checks), permissions=dict(workspace='none', network='none',
        process=['tool'], credentials=[]), prerequisites=pin(manager))
    from plugin_host import services
    from execution_runtime.errors import RuntimePolicyError
    original = services.ExecutionRuntime
    handles = []
    class ObservedRuntime(original):
        def start(self, handle):
            result = super().start(handle)
            handles.append((self, result))
            return result
        def cleanup(self, handle):
            super().cleanup(handle)
            if unconfirmed:
                raise RuntimePolicyError('termination_unconfirmed', 'Synthetic missing cleanup acknowledgement')
    monkeypatch.setattr(services, 'ExecutionRuntime', ObservedRuntime)
    cycle, directory = workflow(config), str(tmp_path / 'run')
    run = executor.run_cycle(cycle, directory)
    assert handles and all(rt.inspect(handle)['state'] == 'cleaned' for rt, handle in handles)
    assert run.steps['count'].outputs['node_result']['status'] == ('unknown' if unconfirmed else 'completed')
    if unconfirmed:
        with pytest.raises(checkpoints.CheckpointError, match='Uncertain outcome'):
            executor.run_cycle(cycle, directory, resume=True)
        assert len(handles) == 1


def test_external_agent_uses_profile_model_and_credential_alias(installed, tls_gateway, tmp_path):
    _, config, install = installed
    raw, state, _ = tls_gateway
    configure(config, https(raw))
    source = Path('tests/fixtures/plugin_providers.py').read_text().split('class FakeAgent:')[1]
    source = 'from qavector_sdk.worker import host_call\nclass Provider:' + source
    source = source.replace('example.agent.review', 'example.demo.run').replace('"plugin_version": "1.0.0"', '"plugin_version": "1.1.0"')
    source = source.replace('    async def execute(self, request, context, emit):\n',
        "    async def execute(self, request, context, emit):\n"
        "        await host_call(context['tools_handle'], 'network.request', {'route': 'api', 'body': {'model': 'test-model'}})\n")
    install('1.1.0', kind='agent_backend', code=source,
        permissions=dict(workspace='none', network='policy_controlled', process=[], credentials=['account']))
    cycle = model.parse_cycle(dict(id='managed', variables={'TOKEN': {'secret': True}}, execution={'runtime': config},
        agent_profiles={'agent.tester': {'backend_ref': 'example.demo.run', 'model': 'test-model', 'credentials_ref': 'account'}},
        steps=[{'id': 'review', 'plugin': 'agent', 'with': {'agent_profile': 'agent.tester', 'task': 'Review'}}]), 'managed')
    missing = executor.run_cycle(cycle, str(tmp_path / 'missing'))
    assert missing.status == 'failed' and not state['calls']
    assert not list((tmp_path / 'missing').rglob('attempts.sqlite3'))
    run = executor.run_cycle(cycle, str(tmp_path / 'run'), secrets={'TOKEN': 'synthetic-token'})
    assert run.status == 'success', run.steps['review'].message
    assert len(state['calls']) == 1 and state['calls'][0][1]['Authorization'] == 'Bearer synthetic-token'


def test_service_can_exceed_legacy_thirty_second_callback_limit(installed, tmp_path):
    manager, config, install = installed
    configure(config, dict(processes={'tool': {'argv': ['/bin/busybox', 'sleep'], 'timeout_seconds': 40}}))
    checks = "        result = await host_call(context['tools_handle'], 'process.run', {'tool': 'tool', 'arguments': ['31']})\n        assert result['exit_code'] == 0\n"
    install('1.1.0', code=code(checks), permissions=dict(workspace='none', network='none',
        process=['tool'], credentials=[]), prerequisites=pin(manager))
    cycle = workflow(config)
    cycle.steps[0].timeout = 50
    run = executor.run_cycle(cycle, str(tmp_path / 'run'))
    assert run.status == 'success', run.steps['count'].message
