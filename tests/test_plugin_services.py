"""Contribution-scoped policy and real TLS service transport (local fixtures)."""

import base64
import copy
import json
import threading
import time
from types import SimpleNamespace

import pytest

from cycle.context import CancelToken
from execution_runtime import PolicyLayer, PolicyPatch, PolicyResolver
from execution_runtime.errors import RuntimePolicyError
from plugin_host.service_policy import NetworkRoute, select_services, validate_services
from plugin_host.services import ServiceSession
from qavector_sdk import ContractError
from test_execution_runtime_gateway import tls_gateway


def route(**changes):
    return dict(endpoint='https://api.example.com/v1/run', addresses=['1.1.1.1'], **changes)


@pytest.mark.parametrize('change', [
    {'endpoint': 'http://api.example.com/v1/run'}, {'endpoint': 'https://user@api.example.com/run'},
    {'endpoint': 'https://api.example.com/run?redirect=x'}, {'endpoint': 'https://api.example.com:0/run'},
    {'addresses': ['169.254.169.254'], 'allow_private_addresses': True}, {'addresses': ['127.0.0.1']},
    {'addresses': ['::ffff:127.0.0.1'], 'allow_private_addresses': True}, {'addresses': ['api.example.com']},
    {'addresses': ['0.0.0.0']}, {'timeout_seconds': True}, {'timeout_seconds': 121},
    {'response_bytes': 65537}, {'method': 'CONNECT'}, {'ca_pem': 'invalid'},
    {'authentication': 'bearer'}, {'credential': '../token'}, {'headers': {'Authorization': 'token'}},
    {'models': ['one', 'one']}, {'models': [{}]}, {'models': ['bad\n']},
])
def test_invalid_routes_fail_before_io(change):
    value = route()
    value.update(change)
    with pytest.raises(RuntimePolicyError):
        NetworkRoute(value)


def policy(raw=None):
    return PolicyResolver().resolve([PolicyLayer('platform', PolicyPatch(raw or {}))],
                                   platform='linux', provider='linux-isolated')


def test_services_intersect_contribution_consent_and_policy():
    endpoint = NetworkRoute(route()).endpoint
    config = dict(network={'api': route(credential='account')}, credentials={'account': 'TOKEN'},
                  processes={'git': {'argv': ['/approved/git']}})
    deployment = SimpleNamespace(to_document=lambda: {'spec': {'plugin_services': {'example.node': config}}})
    worker = dict(binding={'contribution': {'id': 'example.node'}}, grants=dict(
        network='policy_controlled', credentials=['account'], process=['git']),
        image={'files': [dict(target='/approved/git', executable=True, sha256='a' * 64)]},
        external_bindings={'git': {'path': '/approved/git', 'sha256': 'a' * 64}})
    raw = dict(network={'mode': 'restricted', 'endpoints': [endpoint]}, secrets=['TOKEN'], tools=['git'])
    validate_services({'example.node': config})
    assert select_services(deployment, worker, policy(raw)) == config
    for key, value in [('network', {'mode': 'none'}), ('secrets', []), ('tools', [])]:
        with pytest.raises(RuntimePolicyError):
            select_services(deployment, worker, policy(dict(raw, **{key: value})))
    wrong = copy.deepcopy(worker)
    wrong['external_bindings']['git']['sha256'] = 'b' * 64
    with pytest.raises(RuntimePolicyError, match='executable hash'):
        select_services(deployment, wrong, policy(raw))
    wrong = copy.deepcopy(worker)
    wrong['binding']['contribution']['id'] = 'other.node'
    with pytest.raises(RuntimePolicyError):
        select_services(deployment, wrong, policy(raw))


def session(raw, cancel=None):
    configuration = {key: raw[key] for key in ('endpoint', 'addresses', 'allow_private_addresses', 'ca_pem', 'timeout_seconds')}
    configuration.update(credential='account', models=['test-model'], max_requests=2)
    authority = policy(dict(network={'mode': 'restricted', 'endpoints': [NetworkRoute(configuration).endpoint]}, secrets=['TOKEN']))
    return ServiceSession(attempt='one', config=dict(network={'api': configuration}, processes={}, credentials={'account': 'TOKEN'}),
        backend=None, authority=authority, native_policy=policy(), values={'TOKEN': 'synthetic-token'},
        cwd='/', cancel=cancel or CancelToken(), deadline=time.monotonic() + 10, model='test-model')


def packet(operation, arguments, handle='services:one'):
    return dict(execution_id='one', attempt_id='one', handle=handle,
                operation=operation, arguments=arguments)


def test_tls_exact_route_authentication_redaction_and_no_redirect(tls_gateway):
    raw, state, _ = tls_gateway
    service = session(raw)
    state.update(status=302, body=b'{"echo":"synthetic-token"}')
    try:
        result = service.call(packet('network.request', {'route': 'api', 'body': {'model': 'test-model'}}))
        assert result['status'] == 302 and len(state['calls']) == 1
        assert state['calls'][0][1]['Authorization'] == 'Bearer synthetic-token'
        assert base64.b64decode(result['body_base64']) == b'{"echo":"[redacted]"}'
        assert not service.uncertain and service.network_completed == 1
        credentials = service.call(packet('credentials.describe', {}, 'credentials:one'))
        assert credentials == {'aliases': ['account'], 'delivery': 'scoped_service_only'}
        assert 'synthetic-token' not in json.dumps(credentials)
        with pytest.raises(ContractError):
            service.call(packet('network.request', {'route': 'api', 'body': {'model': 'expensive-other'}}))
        with pytest.raises(ContractError):
            service.call(packet('network.request', {'route': 'api', 'url': 'https://elsewhere', 'body': {}}))
        with pytest.raises(ContractError):
            service.call(packet('network.request', {'route': 'api'}, 'runtime:one'))
        assert len(state['calls']) == 1
    finally:
        service.close()


def test_request_budget_and_closed_handle(tls_gateway):
    raw, state, _ = tls_gateway
    service = session(raw)
    args = {'route': 'api', 'body': {'model': 'test-model'}}
    try:
        service.network(args)
        service.network(args)
        with pytest.raises(ContractError, match='budget'):
            service.network(args)
        assert len(state['calls']) == 2
    finally:
        service.close()
    with pytest.raises(ContractError):
        service.call(packet('services.describe', {}))


def test_cancel_after_send_is_unknown_and_aborts_socket(tls_gateway):
    raw, state, received = tls_gateway
    state['body_delay'] = 3
    cancel = CancelToken()
    service = session(raw, cancel)
    def stop():
        received.wait(3)
        cancel.set('stop')
    watcher = threading.Thread(target=stop)
    watcher.start()
    before = time.monotonic()
    try:
        with pytest.raises(ContractError):
            service.network({'route': 'api', 'body': {'model': 'test-model'}})
        assert time.monotonic() - before < 2
        assert service.uncertain and len(state['calls']) == 1
    finally:
        service.close()
        watcher.join(4)


def test_cancel_before_send_does_not_open_connection(tls_gateway, monkeypatch):
    raw, state, _ = tls_gateway
    service = session(raw)
    service.cancel.set('stop')
    monkeypatch.setattr('plugin_host.services._PinnedHTTPS', lambda *_: pytest.fail('No connection after cancellation'))
    with pytest.raises(ContractError):
        service.network({'route': 'api', 'body': {'model': 'test-model'}})
    assert not service.uncertain and not state['calls']
    service.close()


@pytest.mark.parametrize('change', [
    {'encoding': 'gzip'}, {'body': b'x' * 65537},
    {'body_delay': 2},
])
def test_incomplete_or_unreadable_response_blocks_further_calls(tls_gateway, change):
    raw, state, _ = tls_gateway
    service = session(raw)
    service.config['network']['api']['timeout_seconds'] = 1
    state.update(change)
    try:
        with pytest.raises(ContractError):
            service.network({'route': 'api', 'body': {'model': 'test-model'}})
        assert service.uncertain
        with pytest.raises(ContractError, match='reconciliation'):
            service.network({'route': 'api', 'body': {'model': 'test-model'}})
        assert len(state['calls']) == 1
    finally:
        service.close()


@pytest.mark.parametrize('command', [
    {'argv': ['git']}, {'argv': ['/bin/../git']}, {'argv': ['/bin/git'], 'timeout_seconds': True},
    {'argv': ['/bin/git'], 'credentials': ['undeclared']},
    {'argv': ['/bin/git', '--token={credential:account}'], 'credentials': ['account']},
    {'argv': ['/bin/git', '{credential:account}']},
])
def test_invalid_process_configuration(command):
    with pytest.raises(RuntimePolicyError):
        validate_services({'example.node': {'credentials': {'account': 'TOKEN'}, 'processes': {'tool': command}}})


def test_redaction_cannot_overflow_response_budget(tls_gateway):
    raw, state, _ = tls_gateway
    service = session(raw)
    service.config['network']['api']['response_bytes'] = 64
    from execution_runtime.secrets import SecretValues
    service.values = SecretValues({'TOKEN': 'x'})
    state['body'] = b'x' * 64
    try:
        with pytest.raises(ContractError, match='Redacted'):
            service.network({'route': 'api', 'body': {'model': 'test-model'}})
        assert service.uncertain
    finally:
        service.close()


def test_deployment_revision_covers_service_configuration(tmp_path):
    from execution_runtime.deployments import RuntimeDeployment, DeploymentRegistry
    raw = dict(api_version='qavector.io/v1', kind='RuntimeDeployment', metadata={'name': 'test'},
        spec=dict(provider='linux-isolated', policy={}, bindings=[], working_directory='/',
                  backend_options={}, plugin_services={'example.node': {'network': {'api': route()}}}))
    registry = DeploymentRegistry(tmp_path / 'deployments')
    first = registry.save(RuntimeDeployment(raw))
    raw['spec']['plugin_services']['example.node']['network']['api']['endpoint'] = 'https://api.example.com/v2/run'
    second = registry.save(RuntimeDeployment(raw), first.digest)
    assert second.digest != first.digest
    with pytest.raises(RuntimePolicyError, match='changed'):
        registry.load('test', first.digest)
