"""Bounded Host API services. No host process execution or raw secret RPC."""

import base64
import http.client
import json
import re
import threading
import time
from contextlib import nullcontext
from urllib.parse import urlsplit

from execution_runtime import ExecutionRuntime, PolicyLayer, PolicyPatch, PolicyResolver
from execution_runtime.backends.base import ExecutionRequest
from execution_runtime.gateway import _PinnedHTTPS
from execution_runtime.secrets import SecretValues
from qavector_sdk import ContractError

from .gateway import HostGateway
from .service_policy import NetworkRoute


def rejected(message):
    raise ContractError('POLICY_UNSATISFIED', message)


class ServiceSession:
    """One attempt's opaque capabilities, call budgets and cleanup ownership."""

    def __init__(self, *, attempt, config, backend, authority, native_policy, values,
                 cwd, cancel, deadline, workspace_ref=None, workspace_access='none', model=None, pause=None):
        self.config, self.backend, self.authority, self.native_policy = config, backend, authority, native_policy
        self.values = SecretValues(values)
        self.cwd, self.cancel, self.deadline, self.model = cwd, cancel, deadline, model
        self.pause = pause
        self.now = pause.clock.now if pause else time.monotonic
        self.attempt, self.calls, self.network_started, self.network_completed = attempt, {}, 0, 0
        self.closed, self.cleanup_unconfirmed = False, False
        self.connection = None
        self._connection_lock = threading.Lock()
        grants = {}
        if workspace_ref:
            def workspace(args):
                if args != {'workspace_ref': workspace_ref}:
                    rejected('Workspace reference is not granted.')
                return {'path': cwd, 'access': workspace_access}
            grants['runtime:' + attempt] = {'workspace.resolve': workspace}
        if config['network'] or config['processes']:
            grants['services:' + attempt] = {'services.describe': self.describe,
                'network.request': self.network, 'process.run': self.process}
        if config['credentials']:
            grants['credentials:' + attempt] = {'credentials.describe': self.credentials}
        self.gateway = HostGateway(attempt, attempt, grants, lambda _op, _args: self.available())

    @property
    def uncertain(self):
        return self.cleanup_unconfirmed or self.network_started != self.network_completed

    def available(self):
        return not self.closed and not self.cancel.is_set() and self.now() < self.deadline

    def check(self):
        if not self.available():
            rejected('Service capability was cancelled, expired or closed.')

    def call(self, packet):
        return self.gateway.call(packet)

    def close(self):
        self.closed = True
        self.gateway.close()
        with self._connection_lock:
            if self.connection is not None:
                self.connection.abort()
        self.values = SecretValues()

    def describe(self, args):
        if args:
            rejected('Service discovery takes no arguments.')
        return {'network': list(self.config['network']), 'processes': list(self.config['processes'])}

    def credentials(self, args):
        if args:
            rejected('Credential discovery takes no arguments.')
        return {'aliases': list(self.config['credentials']), 'delivery': 'scoped_service_only'}

    def budget(self, kind, ref, maximum):
        if self.pause:
            self.pause.wait()
        self.check()
        if self.uncertain:
            rejected('An earlier service outcome needs reconciliation before further calls.')
        key = (kind, ref)
        count = self.calls.get(key, 0)
        if count >= maximum or sum(self.calls.values()) >= 32:
            rejected('Attempt service call budget is exhausted.')
        self.calls[key] = count + 1
        return count + 1

    def network(self, args):
        if set(args) - {'route', 'body'} or not isinstance(args.get('route'), str):
            rejected('Network requests select a route and an optional JSON body.')
        config = self.config['network'].get(args['route'])
        if config is None:
            rejected('HTTPS route is not granted.')
        route = NetworkRoute(config)
        if route.endpoint not in self.authority.policy.to_document()['network']['endpoints']:
            rejected('HTTPS endpoint is outside effective policy.')
        body = args.get('body')
        if 'models' in config or self.model is not None:
            if not isinstance(body, dict) or body.get('model') not in config.get('models', [self.model]):
                rejected('Request model is not approved for this route.')
            if self.model is not None and body['model'] != self.model:
                rejected('Request model differs from the selected agent profile.')
        try:
            encoded = json.dumps(body, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode() if body is not None else b''
        except (ValueError, TypeError):
            rejected('Expected a JSON request body.')
        if len(encoded) > config.get('request_bytes', 65536) or config.get('method', 'POST') == 'GET' and encoded:
            rejected('Request body exceeds the approved route contract.')
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
                   'Accept-Encoding': 'identity', 'Connection': 'close'}
        secrets = dict(self.values.items())
        if config.get('credential'):
            value = secrets[self.config['credentials'][config['credential']]]
            if any(ord(c) < 33 or ord(c) > 126 for c in value):
                rejected('Credential cannot be delivered as an HTTP header.')
            auth = config.get('authentication', 'bearer')
            headers['Authorization' if auth == 'bearer' else auth] = 'Bearer ' + value if auth == 'bearer' else value
        self.budget('network', args['route'], config.get('max_requests', 4))
        # An HTTP request already sent cannot be paused remotely. Its transport
        # deadline remains wall time; the response waits at the worker RPC gate.
        until = time.monotonic() + min(self.deadline - self.now(), config.get('timeout_seconds', 30))
        connection = _PinnedHTTPS(route, max(0.001, until - time.monotonic()))
        done = threading.Event()
        def watch():
            while not done.wait(0.02):
                if self.cancel.is_set() or self.closed or time.monotonic() >= until:
                    connection.abort()
                    return
        watcher = threading.Thread(target=watch, daemon=True)
        with self._connection_lock:
            self.connection = connection
        watcher.start()
        try:
            # TLS/connect failure is known not to have sent an HTTP request.
            connection.connect()
            self.check()
            with self.pause.io() if self.pause else nullcontext():
                self.check()
                if time.monotonic() >= until:
                    rejected('HTTPS route expired before sending.')
                self.network_started += 1
            # The request is now committed to sending. Do not hold the Pause
            # gate across socket I/O: an in-flight remote request keeps running.
            connection.request(config.get('method', 'POST'), urlsplit(config['endpoint']).path, body=encoded, headers=headers)
            response = connection.getresponse()
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                rejected('Compressed responses are outside the approved route contract.')
            raw = response.read(config.get('response_bytes', 65536) + 1)
            if len(raw) > config.get('response_bytes', 65536):
                rejected('HTTP response exceeded its approved bound.')
            # read(amt) may return a short body instead of IncompleteRead after
            # abort(). Never acknowledge such a response as a completed call.
            self.check()
            if time.monotonic() >= until or response.length not in (None, 0):
                raise ContractError('EXECUTION_STATE_UNKNOWN', 'HTTPS response was incomplete or expired')
            # Redact once so a short secret cannot recursively expand the
            # replacement text. Preserve the RPC bound after substitution.
            patterns = {pattern for value in secrets.values()
                        for pattern in (value.encode(), json.dumps(value)[1:-1].encode())}
            scrub = re.compile(b'|'.join(re.escape(one) for one in sorted(patterns, key=len, reverse=True))) if patterns else None
            content_type = response.getheader('Content-Type', '').encode('utf-8')
            if scrub is not None:
                raw = scrub.sub(b'[redacted]', raw)
                content_type = scrub.sub(b'[redacted]', content_type)
            if len(raw) > config.get('response_bytes', 65536):
                rejected('Redacted HTTP response exceeded its approved bound.')
            result = {'status': response.status, 'body_base64': base64.b64encode(raw).decode(),
                      'content_type': content_type[:256].decode('utf-8', errors='ignore')}
            self.network_completed += 1
            return result
        except (OSError, http.client.HTTPException):
            raise ContractError('EXECUTION_STATE_UNKNOWN' if self.uncertain else 'POLICY_UNSATISFIED',
                                'HTTPS request did not complete') from None
        finally:
            done.set()
            connection.abort()
            connection.close()
            watcher.join(timeout=1)
            with self._connection_lock:
                self.connection = None
            if watcher.is_alive():
                self.cleanup_unconfirmed = True

    def process(self, args):
        if set(args) - {'tool', 'arguments'} or not isinstance(args.get('tool'), str):
            rejected('Process calls select an approved tool and argument list.')
        config = self.config['processes'].get(args['tool'])
        arguments = args.get('arguments', [])
        if (config is None or not isinstance(arguments, list) or len(arguments) > 64
                or any(not isinstance(arg, str) or '\0' in arg or len(arg) > 4096 for arg in arguments)
                or sum(map(len, arguments)) > 32768):
            rejected('Process tool or arguments are outside the approved contract.')
        count = self.budget('process', args['tool'], config.get('max_calls', 4))
        aliases = config.get('credentials', [])
        references = sorted({self.config['credentials'][alias] for alias in aliases})
        values = {name: value for name, value in self.values.items() if name in references}
        native = self.native_policy
        policy = PolicyResolver().resolve([
            PolicyLayer('platform', PolicyPatch(self.authority.policy.to_document())),
            PolicyLayer('node', PolicyPatch(dict(filesystem=native.policy.to_document()['filesystem'],
                network={'mode': 'none'}, secrets=references, tools=[], integrations=[])))],
            [one.to_document() for one in native.bindings], platform=native.platform, provider=native.provider,
            profile_revision=native.profile_revision, runtime_root_identity=native.runtime_root_identity)
        locations = self.backend.secret_file_locations(references)
        substitutions = {'{credential:' + alias + '}': locations[self.config['credentials'][alias]] for alias in aliases}
        prefix = [substitutions.get(value, value) for value in config['argv']]
        timeout = min(config.get('timeout_seconds', 30), self.deadline - self.now())
        self.check()
        request = ExecutionRequest(policy, prefix + arguments, self.cwd, timeout_seconds=timeout, secret_values=SecretValues(values))
        runtime = ExecutionRuntime(self.backend, audit=True)
        handle, result, clean = None, None, False
        scope = self.attempt + '\0' + args['tool'] + '\0' + str(count)
        with self.backend.execution_scope('plugin-tool', scope) as owner:
            try:
                handle = runtime.prepare(request)
                owner.bind(handle)
                self.check()
                with self.pause.io() if self.pause else nullcontext():
                    self.check()
                    handle = runtime.start(handle)
                    if self.pause:
                        self.pause.add(runtime, handle)
                while True:
                    result = runtime.inspect(handle)
                    if result['state'] in ('exited', 'failed', 'cleaned'):
                        break
                    if not self.available():
                        runtime.stop(handle, 0)
                        break
                    self.cancel.wait(0.02)
                result = runtime.wait(handle)
            finally:
                if handle is not None:
                    try:
                        state = runtime.inspect(handle)
                        if state['state'] not in ('prepared', 'cleaned', 'exited', 'failed'):
                            runtime.stop(handle, 0)
                            state = runtime.wait(handle)
                        if state.get('reason') == 'termination_unconfirmed':
                            rejected('Tool process termination is unconfirmed.')
                        runtime.cleanup(handle)
                        if self.pause:
                            self.pause.remove(handle)
                        owner.clear()
                        clean = True
                    finally:
                        if not clean:
                            self.cleanup_unconfirmed = True
        bound = config.get('output_bytes', 32768)
        def tail(value):
            return value.encode('utf-8')[-bound:].decode('utf-8', errors='ignore')
        return {'exit_code': result.get('exit_code'), 'reason': result.get('reason'),
                'stdout': tail(result.get('stdout_tail', '')), 'stderr': tail(result.get('stderr_tail', ''))}
