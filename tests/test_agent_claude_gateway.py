"""Gateway contract validation before any provider IO."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cycle.runtime_agent import AgentReviewWorker
from execution_runtime.errors import RuntimePolicyError
from execution_runtime.workers import claude_artifact, claude_resource_identities, validate_agent_worker
from qavector_agent_claude import gateway
from qavector_agent_claude.backend import BACKEND_ID, VERSION
from qavector_sdk import ContractError
from qavector_sdk.event_loop import run


@pytest.fixture
def configuration(tmp_path):
    root = Path(__file__).parents[1] / 'examples/plugin_contracts'
    profile = json.loads((root / 'agent-profile.json').read_text())
    profile.update(backend_ref=BACKEND_ID, backend_version='==' + VERSION,
                   credentials_ref=None, model='review-model', instructions='', limits={},
                   backend_options={'binary': '/approved/claude', 'tools': ['Read', 'Grep', 'Glob']})
    request = json.loads((root / 'agent-request.json').read_text())
    request.update(task='Review {{QAVECTOR_NATIVE_EVIDENCE}}', instructions='',
                   workspace_ref='gateway.workspace', context_refs=[], artifact_refs=[], limits={},
                   session_ref=None, output_schema={'type': 'object'})
    context = dict(schema_version='1.0.0', execution_id=request['attempt_id'],
                   runtime_handle='gateway.runtime', tools_handle=None,
                   credentials_handle=None, cancellation_handle='gateway.cancel')
    return dict(contract=gateway.CONTRACT, claude='/approved/claude', cwd=str(tmp_path),
                files=[], capability='sk-ant-oat01-runtime-test', max_requests=2,
                request_bytes=524288, profile=profile, request=request, context=context)


def test_gateway_calls_shared_backend_and_keeps_unreported_usage_unknown(configuration, monkeypatch):
    monkeypatch.setattr(gateway.os.path, 'isfile', lambda _: True)
    monkeypatch.setattr(gateway.os, 'access', lambda *_: True)
    calls = []
    async def execute(transport, prompt, on_line):
        calls.append(prompt)
        await on_line(json.dumps({'type': 'result', 'result': '{"risk":"low"}'}))
        return {'ok': True, 'started': True}
    monkeypatch.setattr(gateway.GatewayTransport, '_run', execute)
    result = run(gateway.execute(configuration))
    assert calls == ['Review ']
    assert result['provider_result_received']
    assert result['agent_result']['status'] == 'completed'
    assert result['agent_result']['usage'] is None
    assert result['agent_result']['output'] == {'risk': 'low'}


@pytest.mark.parametrize('change', ['tools', 'credentials', 'limits', 'options'])
def test_gateway_rejects_widened_profile_before_launch(configuration, monkeypatch, change):
    profile = configuration['profile']
    if change == 'tools':
        profile['backend_options']['tools'].append('Bash')
    elif change == 'credentials':
        profile['credentials_ref'] = 'host.secret'
    elif change == 'limits':
        profile['limits']['turns'] = 2
    else:
        profile['backend_options']['permission_mode'] = 'bypassPermissions'
    monkeypatch.setattr(gateway.subprocess, 'Popen', lambda *_a, **_kw: pytest.fail('No subprocess'))
    result = run(gateway.execute(configuration))
    assert result['agent_result']['status'] == 'failed'
    assert not result['provider_result_received']


@pytest.mark.parametrize('change', ['backend', 'attempt', 'version', 'receipt', 'artifact', 'code'])
def test_host_rejects_result_from_another_attempt_or_backend(configuration, change):
    worker = object.__new__(AgentReviewWorker)
    worker.agent_attempt = configuration['request']['attempt_id']
    worker.agent_identity = BACKEND_ID, VERSION
    value = dict(contract=gateway.CONTRACT, code='', provider_result_received=True,
        agent_result=dict(schema_version='1.0.0', attempt_id=worker.agent_attempt,
            backend_ref=BACKEND_ID, plugin_version=VERSION, status='completed',
            summary='', output={}, artifact_refs=[], session_ref=None, usage=None, error=None))
    if change == 'code':
        value['code'] = 0
    elif change == 'receipt':
        value['provider_result_received'] = False
    else:
        key, replacement = {'backend': ('backend_ref', 'other.backend'),
            'attempt': ('attempt_id', 'other-attempt'), 'version': ('plugin_version', '0.0.1'),
            'artifact': ('artifact_refs', ['host.file'])}[change]
        value['agent_result'][key] = replacement
    with pytest.raises((ValueError, ContractError)):
        worker._agent_response(value)
    assert not hasattr(worker, 'agent_result')


@pytest.mark.parametrize('change', ['missing', 'modified'])
def test_readiness_rejects_backend_source_or_schema_drift(change):
    identities = claude_resource_identities()
    assert '/qavector/site/qavector_agent_claude/gateway.py' in identities
    assert '/qavector/site/qavector_sdk/schemas/agent-result-v1.schema.json' in identities
    target = '/qavector/site/qavector_agent_claude/backend.py'
    if change == 'missing':
        del identities[target]
    else:
        identities[target] = '0' * 64
    identities.update({'/worker.py': claude_artifact('linux')[1], '/python': '1' * 64, '/claude': '2' * 64})
    def identity(path):
        if path not in identities:
            raise RuntimePolicyError('worker_file_not_pinned', 'Missing file')
        return identities[path]
    deployment = SimpleNamespace(to_document=lambda: {'spec': {'workers': {'agent.review': {
        'transport': 'claude_cli', 'entrypoint': '/worker.py', 'argv': ['/python'], 'claude': '/claude'}}}})
    backend = SimpleNamespace(platform='linux', runtime_file_identity=identity)
    with pytest.raises(RuntimePolicyError):
        validate_agent_worker(deployment, backend)


def test_unknown_result_keeps_operation_uncertain(configuration):
    from qavector_sdk.ipc import failure
    worker = object.__new__(AgentReviewWorker)
    worker.agent_attempt = configuration['request']['attempt_id']
    worker.agent_identity = BACKEND_ID, VERSION
    result = dict(schema_version='1.0.0', attempt_id=worker.agent_attempt,
        backend_ref=BACKEND_ID, plugin_version=VERSION, status='unknown', summary='',
        output=None, artifact_refs=[], session_ref=None, usage=None,
        error=failure('EXECUTION_STATE_UNKNOWN', 'Gateway stopped'))
    worker._agent_response(dict(contract=gateway.CONTRACT, code='',
                               provider_result_received=True, agent_result=result))
    assert worker.agent_result['status'] == 'unknown'
    assert not worker.provider_result_received
