"""Trusted lowering of installed contributions into the managed Runtime image."""

import copy
from pathlib import Path
import time

from execution_runtime import ExecutionRuntime, PolicyLayer, PolicyPatch, PolicyResolver
from execution_runtime.pause import PauseGroup
from execution_runtime.backends.base import ExecutionRequest
from execution_runtime.backends.linux import LinuxBackend, BundledBubblewrapBackend, SystemBubblewrapBackend
from execution_runtime.backends.linux.filesystem import RuntimeFile, RuntimeImage
from execution_runtime.errors import RuntimePolicyError
from plugin_host.attempts import AttemptStore
from plugin_host.services import ServiceSession
from plugin_host.service_policy import select_services
from plugin_host.runner import run_plugin
from qavector_sdk import ContractError, validate_output, validate_contract

from . import checkpoints
from .operations import Operation, result_document, result_from_document
from .registry import PluginResult
from domain.cycle import SUCCESS, FAILED


class ManagedPluginWorker:
    contract = 'qavector.managed-plugin/v1'

    def __init__(self, plan, step, metadata):
        self.plan, self.step, self.metadata = plan, step, metadata
        self.spec = copy.deepcopy(plan.plugin_plan.specs[step.id])
        self.profile = None
        if self.spec['binding']['contribution']['kind'] == 'agent_backend':
            from .agent_profiles import selected
            self.profile = selected(plan.cycle, step.settings.get('agent_profile'))
        self.deployment, self.backend, self.policy = self.resolve()
        if not set(self.services['credentials'].values()) <= set(plan.cycle.secrets):
            raise checkpoints.CheckpointError('Plugin service credentials must reference declared Cycle secrets.')
        if self.profile and self.profile['credentials_ref'] is not None and self.profile['credentials_ref'] not in self.services['credentials']:
            raise checkpoints.CheckpointError('Agent credential alias is not granted by its deployment and package.')
        self.binding = self.make_binding(self.deployment, self.policy)

    def make_binding(self, deployment, policy):
        binding = dict(self.plan.binding(deployment, policy, self.contract),
            plugin=self.spec['binding'], managed_runtime=self.spec['runtime_id'])
        if any(self.services.values()):
            binding.update(services=self.services, service_policy=self.authority.to_document())
        return binding

    def resolve(self):
        deployment, backend, effective = self.plan.resolve(self.step)
        if (type(backend) not in (LinuxBackend, BundledBubblewrapBackend, SystemBubblewrapBackend)
                or backend.runtime_root_identity() != self.spec['base_image_identity']):
            raise checkpoints.CheckpointError('Plugin deployment must approve the exact managed Python base image.')
        grants = self.spec['grants']
        self.authority = effective
        self.services = select_services(deployment, self.spec, effective)
        image = RuntimeImage(tuple(RuntimeFile(**one) for one in self.spec['image']['files']))
        if image.identity != self.spec['image']['identity']:
            raise checkpoints.CheckpointError('Managed plugin image identity changed.')
        backend = type(backend)(image, backend.state_directory, cgroup_parent=backend.cgroup_parent,
            protected_paths=backend.protected_paths, launcher=backend.launcher, bundle_directory=backend._bundle.directory)
        raw = effective.policy.to_document()
        filesystem = [dict(grant, access='read_only' if grants['workspace'] == 'read' else grant['access'])
                      for grant in raw['filesystem'] if grant['binding'] == 'project' and grants['workspace'] != 'none']
        # This layer only narrows the approved deployment; no manifest can grant access.
        policy = PolicyResolver().resolve([
            PolicyLayer('platform', PolicyPatch(raw)),
            PolicyLayer('node', PolicyPatch(dict(filesystem=filesystem, network={'mode': 'none'},
                                                secrets=[], tools=[], integrations=[])))],
            [one.to_document() for one in effective.bindings], platform=effective.platform,
            provider=effective.provider, profile_revision=effective.profile_revision,
            runtime_root_identity=image.identity)
        return deployment, backend, policy

    def prepare(self, context, step):
        context.cancel.raise_if_set()

    def execute(self, context, step):
        from . import freeze
        pause = freeze.track_controller(PauseGroup(context.cancel))
        deadline = getattr(context, 'runtime_deadline', None)
        active_deadline = None if deadline is None else pause.clock.now() + deadline - time.monotonic()
        try:
            pause.wait()
            context.cancel.raise_if_set()
            return self._execute(context, step, pause, active_deadline)
        finally:
            pause.close()

    def _execute(self, context, step, pause, active_deadline):
        self.plan.plugin_plan.verify(step.id)
        deployment, backend, policy = self.resolve()
        binding = self.make_binding(deployment, policy)
        if binding != self.binding:
            raise checkpoints.CheckpointError('Managed runtime policy changed after approval.')
        operation = Operation(context, step, 'managed-plugin',
                              {'binding': binding, 'settings': step.settings, 'profile': self.profile})
        cached = operation.cached()
        if cached:
            return result_from_document(cached)
        limits = copy.deepcopy(self.profile['limits']) if self.profile else {}
        timeout = min(limits.get('wall_time_seconds', 300), 300)
        if active_deadline is not None:
            timeout = min(timeout, active_deadline - pause.clock.now())
        context.cancel.raise_if_set()
        if timeout <= 0:
            raise checkpoints.CheckpointError('Plugin deadline expired before execution.')
        limits['wall_time_seconds'] = timeout
        mounts = policy.policy.to_document()['filesystem']
        cwd = deployment.to_document()['spec']['working_directory'] if mounts else '/'
        workspace_ref = 'workspace:' + step.id if mounts else None
        access = 'write' if any(one['access'] == 'read_write' for one in mounts) else 'read' if mounts else 'none'
        attempt = checkpoints.digest([context.run.id, step.id, operation.directory, operation.epoch])
        request = dict(schema_version='1.0.0', run_id=context.run.id, node_id=step.id, attempt_id=attempt,
            workspace_ref=workspace_ref, artifact_refs=[], output_schema=step.settings.get('output_schema'), limits=limits)
        if self.profile:
            if step.settings.get('inputs') or step.settings.get('directory'):
                raise checkpoints.CheckpointError('Managed agents use task/instructions; workspace paths come from the deployment.')
            request.update(task=step.settings.get('task', ''), instructions=self.profile['instructions'],
                           context_refs=[], session_ref=None)
            config = self.profile['backend_options']
        else:
            config = step.settings.get('config', {})
            request.update(config=config, inputs=step.settings.get('inputs', {}))
            validate_output(request['inputs'], self.spec['schemas']['input'], code='CONFIG_INVALID')
        validate_output(config, self.spec['schemas']['config'], code='CONFIG_INVALID')
        params = dict(request=request, context=dict(schema_version='1.0.0', execution_id=attempt,
            runtime_handle='runtime:' + attempt,
            tools_handle='services:' + attempt if self.services['network'] or self.services['processes'] else None,
            credentials_handle='credentials:' + attempt if self.services['credentials'] else None,
            cancellation_handle='cancel:' + attempt))
        if self.profile:
            params.update(profile=self.profile, requirements=dict(schema_version='1.0.0',
                workspace_access=access, hard_limits=list(limits), confirmed_cancellation=True))
        initialization = dict(protocol_version='1.0.0', identity=self.spec['binding'],
                              schemas=self.spec['schemas'], execution_scope='local_process')
        validate_contract('plugin-initialize', initialization)
        validate_contract('plugin-execute', params)
        values = {}
        for reference in set(self.services['credentials'].values()):
            value = context.secrets.get(reference)
            if not isinstance(value, str) or not value:
                raise checkpoints.CheckpointError('Plugin credential is unavailable: ' + reference)
            values[reference] = value
        gateway = ServiceSession(attempt=attempt, config=self.services, backend=backend,
            authority=self.authority, native_policy=policy, values=values, cwd=cwd, cancel=context.cancel,
            deadline=pause.clock.now() + timeout, workspace_ref=workspace_ref, workspace_access=access,
            model=self.profile['model'] if self.profile else None, pause=pause)
        argv = self.spec['argv'][:-1] + ['cycle-worker']
        execution = ExecutionRequest(policy, argv, cwd, timeout_seconds=timeout)
        runtime = ExecutionRuntime(backend, audit=True)
        info = dict(binding, enforcement='not_evaluated', cleanup_confirmed=True)
        operation.begin()
        store = AttemptStore(Path(operation.directory) / 'attempts.sqlite3')
        try:
            with backend.execution_scope('cycle', self.plan.workspace + '\0' + step.id) as owner:
                # Bind native ownership before any payload starts; crashes leave a
                # durable guard against replacement until Runtime reconciliation.
                class OwnedRuntime:
                    def prepare(self, req):
                        handle = runtime.prepare(req)
                        owner.bind(handle)
                        info.update(instance_id=handle.instance_id, cleanup_confirmed=False)
                        return handle
                    def start(self, handle):
                        with pause.io():
                            if context.cancel.is_set():
                                raise ContractError('EXECUTION_CANCELLED', 'Stopped before worker launch')
                            handle = runtime.start(handle)
                            pause.add(runtime, handle)
                        info['enforcement'] = 'verified'
                        context.stage(step.id, {'kind': 'runtime', 'title': 'Plugin worker',
                            'status': 'running', 'body': {'runtime': copy.deepcopy(info)}})
                        return handle
                    def cleanup(self, handle):
                        runtime.cleanup(handle)
                        pause.remove(handle)
                        owner.clear()
                        info['cleanup_confirmed'] = True
                    def __getattr__(self, name):
                        return getattr(runtime, name)
                result = run_plugin(OwnedRuntime(), execution, initialization, params, store,
                                    cancel=context.cancel, gateway=gateway, pause=pause)
        finally:
            gateway.close()
            store.close()
        name = 'agent_result' if self.profile else 'node_result'
        answer = PluginResult(SUCCESS if result['status'] == 'completed' else FAILED,
            outputs={'output': result['output'], 'summary': result['summary'], name: result},
            message=result['summary'], runtime=dict(info, outcome=result['status']))
        if result['status'] != 'unknown':
            # Preserve received provider responses even if subsequent output
            # validation fails; Resume must not repeat a possibly billable call.
            operation.complete(result_document(answer), answer.ok or gateway.network_completed > 0)
        if result['status'] == 'timed_out':
            context.cancel.set('timed out in managed plugin')
        return answer

    def cleanup(self, context, step):
        pass  # run_plugin owns stop, wait and cleanup on every path.
