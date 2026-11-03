"""An explicitly provisioned, pinned Python image; never use core sys.executable."""

from importlib import resources
import copy
import re
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

from .archive import json_document, reject, sha256


class ManagedRuntime:
    def __init__(self, descriptor, state_directory, *, backend_factory=None):
        required = {'format_version', 'target', 'python_version', 'tags', 'markers',
                    'sdk_version', 'interpreter', 'image'}
        if not isinstance(descriptor, dict) or set(descriptor) != required or descriptor['format_version'] != 1:
            reject('Invalid managed runtime descriptor', 'PLUGIN_RUNTIME_INCOMPATIBLE')
        if sys.platform != 'linux' or not isinstance(descriptor['target'], str) or not re.fullmatch(r'linux-x86_64-cp[0-9]+', descriptor['target']):
            reject('Managed isolation is currently supported on Linux x86_64 only', 'PLUGIN_RUNTIME_INCOMPATIBLE')
        from execution_runtime.backends.linux.filesystem import RuntimeImage, RuntimeFile
        self.image = RuntimeImage(tuple(RuntimeFile(**one) for one in descriptor['image']['files']))
        if self.image.identity != descriptor['image']['identity']:
            reject('Managed runtime image identity mismatch')
        identities = {one.target: one.sha256 for one in self.image.files}
        from .bundled import _payload
        for name, data in _payload(resources.files('qavector_sdk'), 'qavector_sdk'):
            if identities.get('/qavector/site/' + name) != sha256(data):
                reject('Managed runtime SDK differs from this core build', 'PLUGIN_RUNTIME_INCOMPATIBLE')
        if descriptor['interpreter'] not in identities:
            reject('Managed interpreter must be pinned into the runtime image')
        if (descriptor['sdk_version'] != '1.0.0' or not isinstance(descriptor['tags'], list)
                or not descriptor['tags'] or not isinstance(descriptor['markers'], dict)):
            reject('Invalid managed Python metadata')
        from packaging.markers import default_environment
        from packaging.tags import parse_tag
        from packaging.version import Version
        try:
            release = Version(descriptor['python_version'])
            if (descriptor['target'] != 'linux-x86_64-cp%d%d' % release.release[:2]
                    or set(descriptor['markers']) != set(default_environment())
                    or descriptor['markers']['python_version'] != '%d.%d' % release.release[:2]
                    or descriptor['markers']['sys_platform'] != 'linux'
                    or any(not isinstance(value, str) for value in descriptor['markers'].values())):
                reject('Managed runtime metadata does not match its target')
            for tag in descriptor['tags']:
                parse_tag(tag)
        except (ValueError, TypeError):
            reject('Invalid managed Python metadata')
        self.document = copy.deepcopy(descriptor)
        self.bootstrap_digest = sha256(Path(__file__).with_name('managed_bootstrap.py').read_bytes())
        identity_document = dict(descriptor, image={'identity': self.image.identity}, bootstrap=self.bootstrap_digest)
        self.identity = sha256(json.dumps(identity_document, sort_keys=True, separators=(',', ':')).encode())
        self.state_directory = Path(state_directory)
        self.backend_factory = backend_factory

    @classmethod
    def read(cls, path, state_directory, **kwargs):
        path = Path(path).absolute()
        document = json_document(path.read_bytes())
        from .archive import safe_path
        for item in document['image']['files']:
            if not Path(item['source']).is_absolute():
                item['source'] = str(path.parent / safe_path(item['source']))
        return cls(document, state_directory, **kwargs)

    def execution_image(self, directory):
        from execution_runtime.backends.linux.filesystem import RuntimeImage
        targets = {one.target: (one.source, one.target, one.executable) for one in self.image.files}
        for source in sorted(Path(directory).rglob('*')):
            if source.is_symlink():
                reject('Managed environment contains a symlink')
            if source.is_file():
                target = '/qavector/plugins/' + source.relative_to(directory).as_posix()
                if target in targets:
                    reject('Managed image overlaps plugin environment')
                targets[target] = (str(source), target, False)
        bootstrap = Path(__file__).with_name('managed_bootstrap.py')
        target = '/qavector/managed_bootstrap.py'
        if sha256(bootstrap.read_bytes()) != self.bootstrap_digest:
            reject('Managed bootstrap changed after runtime selection')
        if target in targets:
            reject('Managed bootstrap target collision')
        targets[target] = (str(bootstrap), target, False)
        image = RuntimeImage.from_files(list(targets.values()))
        # from_files hashes sources again: never silently accept changed base files.
        actual = {one.target: one.sha256 for one in image.files}
        if any(actual[one.target] != one.sha256 for one in self.image.files):
            reject('Managed runtime source changed after approval')
        return image

    def worker_argv(self):
        return [self.document['interpreter'], '-I', '-S', '-B', '/qavector/managed_bootstrap.py', 'worker']

    def healthcheck(self, directory, contributions, cancel=None):
        from execution_runtime import ExecutionRuntime, PolicyResolver, PolicyLayer, PolicyPatch
        from execution_runtime.backends.base import ExecutionRequest
        from execution_runtime.backends.linux import LinuxBackend
        image = self.execution_image(directory)
        backend = (self.backend_factory(image, self.state_directory) if self.backend_factory else
                   LinuxBackend(image, self.state_directory))
        runtime = ExecutionRuntime(backend)
        policy = PolicyResolver().resolve([PolicyLayer('platform', PolicyPatch({
            'resources': {'scratch_bytes': 16 * 1024 ** 2}}))], platform='linux',
            provider='linux-isolated', runtime_root_identity=image.identity)
        nonce = uuid4().hex
        request = ExecutionRequest(policy, self.worker_argv()[:-1] + [
            'healthcheck', json.dumps(contributions), nonce], '/', timeout_seconds=15)
        report = runtime.preflight(policy)
        if not report.ready:
            reasons = sorted({item.reason for item in report.checks if item.status != 'verified'})
            reject('Managed runtime isolation is unavailable: ' + '; '.join(reasons), 'PLUGIN_RUNTIME_INCOMPATIBLE')
        handle = None
        try:
            handle = runtime.prepare(request)
            handle = runtime.start(handle)
            while runtime.inspect(handle)['state'] not in ('exited', 'cleaned'):
                if cancel is not None and cancel.is_set():
                    runtime.stop(handle, grace_seconds=0)
                    reject('Installation cancelled', 'INSTALL_CANCELLED')
                time.sleep(0.02)
            result = runtime.wait(handle)
            expected = json.dumps({'healthcheck': nonce})
            if result['exit_code'] != 0 or result['stdout_tail'].strip() != expected:
                reject('Isolated import healthcheck failed', 'PLUGIN_HEALTHCHECK_FAILED')
            return {'status': 'passed', 'runtime_id': self.identity, 'image_identity': image.identity,
                    'scope': 'offline_import', 'provider_request': 'not_sent'}
        finally:
            if handle is not None:
                runtime.cleanup(handle)
