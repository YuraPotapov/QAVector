"""Real namespace pause plus Cycle/Host API pause and cancellation semantics."""

from contextlib import contextmanager
import json
from pathlib import Path
import shlex
import threading
import time

import pytest

from cycle import checkpoints, executor
from cycle.context import CancelToken
from execution_runtime import ExecutionRuntime
from execution_runtime.backends.base import ExecutionRequest
from test_execution_runtime_linux import native, policy, git_workload_image
from test_plugin_installer_native import manager
from test_cycle_managed_plugins import installed, workflow
from test_plugin_services_native import code, configure, pin, https, secret_cycle
from test_execution_runtime_gateway import tls_gateway
from test_cycle_phases import Events


def until(predicate, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(.02)
    return bool(predicate())


def count(path):
    try:
        return int(path.read_text() or 0)
    except (OSError, ValueError):
        return 0


@pytest.mark.parametrize('stop', [False, True])
def test_native_pause_freezes_descendants_and_deadline(native, tmp_path, stop):
    backend, _ = native
    runtime = ExecutionRuntime(backend)
    script = 'i=0; while ! test -f /workspace/finish; do i=$((i+1)); echo "$i" > /workspace/count; /bin/busybox sleep 0.03; done'
    handle = runtime.start(runtime.prepare(ExecutionRequest(policy(backend, tmp_path, 'read_write'),
        ['/bin/busybox', 'sh', '-c', '/bin/busybox sh -c ' + shlex.quote(script) + ' & wait'],
        '/workspace', timeout_seconds=2)))
    try:
        assert until(lambda: count(tmp_path / 'count') > 2)
        runtime.pause(handle)
        runtime.pause(handle)  # idempotent
        before = count(tmp_path / 'count')
        time.sleep(2.2)
        assert runtime.inspect(handle)['paused']
        assert runtime.inspect(handle)['state'] == 'running'
        assert count(tmp_path / 'count') == before
        if stop:
            started = time.monotonic()
            runtime.stop(handle, 0)
            assert time.monotonic() - started < 3
            assert runtime.wait(handle)['reason'] == 'cancelled'
        else:
            (tmp_path / 'finish').touch()
            runtime.resume(handle)
            runtime.resume(handle)
            assert runtime.wait(handle)['exit_code'] == 0
        assert not runtime.inspect(handle)['paused']
    finally:
        runtime.stop(handle, 0)
        runtime.cleanup(handle)


@contextmanager
def live(cycle, directory, secrets=None):
    events, cancel, result = Events(), CancelToken(), {}
    identifier = directory.name
    def run():
        try:
            result['run'] = executor.run_cycle(cycle, str(directory), observer=events, cancel=cancel,
                controlled=True, run_id=identifier, secrets=secrets or {})
        except Exception as exc:
            result['error'] = exc
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield events, result, identifier
    finally:
        cancel.set('test cleanup')
        thread.join(15)
        assert not thread.is_alive(), 'Paused job did not stop'
        assert 'error' not in result, result


def pause(identifier, events):
    assert not executor.control_run(identifier, 'pause')
    assert until(lambda: any(e['kind'] == 'cycle.run.state' and e.get('action') == 'paused' and
                            e.get('held_steps') == ['count'] for e in events.events))


@pytest.mark.parametrize('tool,stop', [(False, False), (True, False), (True, True)])
def test_cycle_pause_holds_worker_and_tool_with_same_attempt(installed, tmp_path, tool, stop):
    manager, config, install = installed
    project = Path(config['project_directory'])
    if tool:
        command = 'i=0; while ! test -f /workspace/finish; do i=$((i+1)); echo "$i" > /workspace/count; /bin/busybox sleep 0.03; done'
        configure(config, {'processes': {'tool': {'argv': ['/bin/busybox', 'sh', '-c', command], 'timeout_seconds': 2}}})
        checks = "        result = await host_call(context['tools_handle'], 'process.run', {'tool': 'tool'})\n        assert result['exit_code'] == 0\n"
    else:
        checks = '''        import pathlib, time
        marker = pathlib.Path('/workspace/count')
        i = 0
        while not pathlib.Path('/workspace/finish').exists():
            i += 1
            marker.write_text(str(i))
            time.sleep(.03)
'''
    install('1.1.0', code=code(checks), permissions=dict(workspace='write', network='none',
        credentials=[], process=['tool'] if tool else []), prerequisites=pin(manager) if tool else None)
    cycle = workflow(config)
    cycle.steps[0].timeout = 12
    with live(cycle, tmp_path / 'paused-run') as (events, result, identifier):
        assert until(lambda: count(project / 'count') > 2), result
        pause(identifier, events)
        saved = count(project / 'count')
        time.sleep(12.2)
        assert count(project / 'count') == saved and 'run' not in result
        if not stop:
            (project / 'finish').touch()
            assert not executor.control_run(identifier, 'continue')
            assert until(lambda: 'run' in result), result
    run = result['run']
    assert run.status == ('cancelled' if stop else 'success'), run.steps['count'].message
    attempts = list((tmp_path / 'paused-run').rglob('attempts.sqlite3'))
    assert len(attempts) == 1
    receipts = [json.loads(path.read_text()) for path in (tmp_path / 'paused-run').rglob('receipt.json')]
    assert all(row['state'] == 'complete' for row in receipts)


@pytest.mark.parametrize('outcome', ['completed', 'incomplete', 'stop'])
def test_https_finishes_while_paused_and_is_not_repeated(installed, tls_gateway, tmp_path, outcome):
    _, config, install = installed
    raw, state, received = tls_gateway
    services = https(raw)
    services['network']['api']['timeout_seconds'] = 1 if outcome == 'incomplete' else 10
    configure(config, services)
    state['body_delay'] = 1 if outcome == 'completed' else 3
    checks = "        result = await host_call(context['tools_handle'], 'network.request', {'route': 'api', 'body': {'model': 'test-model'}})\n        assert result['status'] == 200\n"
    install('1.1.0', code=code(checks), permissions=dict(workspace='none', network='policy_controlled', process=[], credentials=['account']))
    cycle = secret_cycle(config)
    cycle.steps[0].timeout = 12
    with live(cycle, tmp_path / 'http-paused', {'TOKEN': 'synthetic-token'}) as (events, result, identifier):
        assert received.wait(20), result
        pause(identifier, events)
        if outcome != 'stop':
            time.sleep(12.2)
            assert 'run' not in result and len(state['calls']) == 1
            assert not executor.control_run(identifier, 'continue')
            assert until(lambda: 'run' in result), result
    run = result['run']
    assert run.status == {'completed': 'success', 'incomplete': 'failed', 'stop': 'cancelled'}[outcome], run.steps['count'].message
    if outcome != 'completed':
        assert run.steps['count'].outputs['node_result']['status'] == 'unknown'
        with pytest.raises(checkpoints.CheckpointError, match='Uncertain outcome'):
            executor.run_cycle(cycle, str(tmp_path / 'http-paused'), secrets={'TOKEN': 'synthetic-token'}, resume=True)
    assert len(state['calls']) == 1


@pytest.mark.parametrize('stop', [False, True])
def test_pause_before_launch_never_starts_work_until_continue(installed, tmp_path, monkeypatch, stop):
    _, config, _ = installed
    from cycle import runtime_plugin
    original = runtime_plugin.ExecutionRuntime
    ready, proceed, starts = threading.Event(), threading.Event(), []
    class HeldPreparation(original):
        def prepare(self, request):
            handle = super().prepare(request)
            ready.set()
            assert proceed.wait(20)
            return handle
        def start(self, handle):
            starts.append(handle.instance_id)
            return super().start(handle)
    monkeypatch.setattr(runtime_plugin, 'ExecutionRuntime', HeldPreparation)
    try:
        with live(workflow(config), tmp_path / 'late-start') as (events, result, identifier):
            assert ready.wait(20), result
            pause(identifier, events)
            proceed.set()
            time.sleep(.3)
            assert not starts and 'run' not in result
            if not stop:
                assert not executor.control_run(identifier, 'continue')
                assert until(lambda: 'run' in result), result
    finally:
        proceed.set()
    assert result['run'].status == ('cancelled' if stop else 'success')
    assert len(starts) == (0 if stop else 1)
    receipts = [json.loads(path.read_text()) for path in (tmp_path / 'late-start').rglob('receipt.json')]
    assert receipts and all(row['state'] == 'complete' for row in receipts)
