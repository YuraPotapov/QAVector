"""Universal package lifecycle, serialized with registry/run-lock transactions.

Filesystem publication precedes the SQLite commit. A crash can leave only an
unregistered orphan, never an active partial version. Recovery removes orphans
under the same writer lock. Worker payloads are never imported here.
"""

import json
import os
from pathlib import Path
import re
import shutil
import time
from uuid import uuid4

from .archive import Package, json_document, reject, sha256
from .registry import PluginRegistry, _json
from .compatibility import report as compatibility_report, host_versions
from .trust import assess, authorize
from .wheels import environment


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _sync(directory):
    if os.name != 'nt':
        fd = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _remove(path):
    if path.is_symlink():
        reject('Storage symlink detected')
    if path.exists():
        for entry in path.rglob('*'):
            if entry.is_symlink():
                reject('Storage symlink detected')
            entry.chmod(0o700 if entry.is_dir() else 0o600)
        path.chmod(0o700)
        shutil.rmtree(path)


def _seal(path):
    for entry in sorted(path.rglob('*'), reverse=True):
        if entry.is_dir():
            _sync(entry)
        entry.chmod(0o500 if entry.is_dir() else 0o400)
    _sync(path)
    path.chmod(0o500)


class PluginManager:
    def __init__(self, root, runtime=None):
        self.root = Path(root).absolute()
        if self.root.is_symlink():
            reject('Plugin store must not be a symlink')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.runtime = runtime
        for name in ('staging', 'packages', 'environments', 'logs', 'state', 'cache'):
            path = self.root / name
            if path.is_symlink():
                reject('Plugin storage directory must not be a symlink')
            path.mkdir(exist_ok=True, mode=0o700)
        for name in ('registry.sqlite3', 'registry.sqlite3-journal', 'registry.sqlite3-wal', 'registry.sqlite3-shm'):
            if (self.root / name).is_symlink():
                reject('Registry files must not be symlinks')
        self.registry = PluginRegistry(self.root / 'registry.sqlite3')
        self.registry.db.executescript('''
            CREATE TABLE IF NOT EXISTS managed_installations (
                plugin_id TEXT NOT NULL, version TEXT NOT NULL, document TEXT NOT NULL,
                PRIMARY KEY(plugin_id, version),
                FOREIGN KEY(plugin_id,version) REFERENCES packages(plugin_id,version));
            CREATE TABLE IF NOT EXISTS plugin_trust_keys (id TEXT PRIMARY KEY, public_key TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS plugin_references (
                kind TEXT NOT NULL, id TEXT NOT NULL, pins TEXT NOT NULL, PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS plugin_install_log (
                id TEXT PRIMARY KEY, at REAL NOT NULL, digest TEXT NOT NULL, status TEXT NOT NULL, code TEXT NOT NULL);
        ''')

    def close(self):
        self.registry.close()

    def enroll_key(self, key_id, public_key):
        import base64
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        if not isinstance(key_id, str) or not re.fullmatch('[A-Za-z0-9_.-]{1,128}', key_id):
            reject('Invalid trust key ID')
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
        except (ValueError, TypeError):
            reject('Invalid Ed25519 public key')
        with self.registry._transaction():
            prior = self.registry.db.execute('SELECT public_key FROM plugin_trust_keys WHERE id=?', (key_id,)).fetchone()
            if prior and prior[0] != public_key:
                reject('Trust key ID already belongs to another key')
            self.registry.db.execute('INSERT OR IGNORE INTO plugin_trust_keys VALUES (?,?)', (key_id, public_key))

    def _trust(self, package):
        keys = dict(self.registry.db.execute('SELECT id,public_key FROM plugin_trust_keys'))
        return assess(package, keys)

    def inspect(self, source):
        package = Package.read(source, check_compatibility=False)
        compatibility = compatibility_report(package.manifest)
        trust = self._trust(package)
        if self.runtime is not None and compatibility['compatible']:
            environment(package, self.runtime.document)
        prior = [item for item in self.list() if item['plugin_id'] == package.manifest['plugin_id'] and item['state'] == 'active']
        return {'digest': package.digest, 'manifest': package.manifest, 'trust': trust,
                'compatibility': compatibility,
                'target': self.runtime.document['target'] if self.runtime else None,
                'runtime_ready': self.runtime is not None,
                'previous_active': prior[0] if prior else None,
                'approval_required': ({'digest': package.digest, 'publisher_identity': trust['publisher_identity'],
                                      'grants': package.manifest['permissions_requested']} if compatibility['compatible'] else None)}

    def list(self):
        rows = self.registry.db.execute('''SELECT p.plugin_id,p.version,p.digest,p.state,p.manifest,p.publisher_identity,m.document
            FROM packages p LEFT JOIN managed_installations m USING(plugin_id,version) ORDER BY p.plugin_id,p.version''')
        result = []
        for row in rows:
            manifest = json.loads(row[4])
            compatibility = compatibility_report(manifest)
            result.append(dict(plugin_id=row[0], version=row[1], digest=row[2], state=row[3],
                effective_state=row[3] if compatibility['compatible'] else 'incompatible',
                manifest=manifest, publisher_identity=row[5], compatibility=compatibility,
                installation=json.loads(row[6]) if row[6] else None))
        return result

    def check(self):
        packages = [{key: item[key] for key in ('plugin_id', 'version', 'state', 'effective_state', 'compatibility')}
                    for item in self.list()]
        return {'scope': 'qavector_and_public_apis', 'host': host_versions(), 'compatible': all(item['compatibility']['compatible'] for item in packages),
                'packages': packages}

    def describe(self, plugin_id, version):
        record = self._record(plugin_id, version)
        path, _ = self._paths(record)
        package = Package.read(path / 'package.qavplug', check_compatibility=False)
        if package.digest != record['digest']:
            reject('Installed archive digest changed')
        return {'manifest': package.manifest, 'schemas': package.schemas,
                'compatibility': compatibility_report(package.manifest)}

    def _record(self, plugin_id, version):
        row = self.registry.db.execute('SELECT document FROM managed_installations WHERE plugin_id=? AND version=?',
                                       (plugin_id, version)).fetchone()
        if row is None:
            reject('Managed package version is not installed', 'PLUGIN_NOT_AVAILABLE')
        return json.loads(row[0])

    def _paths(self, record):
        return self.root / 'packages' / record['digest'], self.root / 'environments' / record['environment_id']

    def _verify(self, record):
        package_path, env_path = self._paths(record)
        if package_path.is_symlink() or env_path.is_symlink():
            reject('Installed storage symlink detected')
        package = Package.read(package_path / 'package.qavplug')
        if package.digest != record['digest'] or self._trust(package) != record['trust']:
            reject('Installed package integrity or trust changed', 'PLUGIN_NOT_AVAILABLE')
        actual = {}
        for path in env_path.rglob('*'):
            if path.is_symlink():
                reject('Environment contains a symlink')
            if path.is_file():
                actual[path.relative_to(env_path).as_posix()] = sha256(path.read_bytes())
        if actual != record['files']:
            reject('Installed environment is missing or modified', 'PLUGIN_NOT_AVAILABLE')
        if self.runtime is None or self.runtime.identity != record['runtime_id']:
            reject('Exact managed runtime is unavailable', 'PLUGIN_RUNTIME_INCOMPATIBLE')
        self.runtime.execution_image(env_path)
        return package, env_path

    def install(self, source, decision, *, activate=False, cancel=None, prerequisites=None):
        package = Package.read(source)
        if self.runtime is None:
            reject('A bundled or explicitly provisioned managed runtime is required', 'PLUGIN_RUNTIME_INCOMPATIBLE')
        identity, version = package.manifest['plugin_id'], package.manifest['version']
        attempt = uuid4().hex
        try:
            with self.registry._transaction():
                self._recover()
                trust = self._trust(package)
                grants = authorize(package, trust, decision)
                prior = self.registry.db.execute('SELECT digest FROM packages WHERE plugin_id=? AND version=?', (identity, version)).fetchone()
                if prior:
                    if prior[0] != package.digest:
                        reject('Version already has another package digest', 'PACKAGE_VERSION_CONFLICT')
                    record = self._record(identity, version)
                    self._verify(record)
                    if activate:
                        self._activate(identity, version, record, prerequisites)
                    return record
                def check_cancel():
                    if cancel is not None and cancel.is_set():
                        reject('Installation cancelled', 'INSTALL_CANCELLED')
                check_cancel()
                # Ownership/API checks precede any payload execution. This row
                # is invisible to other connections until the final commit.
                self.registry.register(package.manifest, package.digest, publisher_identity=trust['publisher_identity'])
                files = environment(package, self.runtime.document)
                stage = self.root / 'staging' / attempt
                stage.mkdir(mode=0o700)
                package_stage, env_stage = stage / 'package', stage / 'environment'
                _write(package_stage / 'package.qavplug', package.raw)
                for name, data in files.items():
                    check_cancel()
                    _write(env_stage / name, data)
                healthcheck = self.runtime.healthcheck(env_stage, package.manifest['contributions'], cancel)
                check_cancel()
                record = {'digest': package.digest, 'runtime_id': self.runtime.identity,
                          'target': self.runtime.document['target'], 'environment_id': sha256(
                              (package.digest + self.runtime.identity).encode()), 'trust': trust,
                          'decision': grants, 'healthcheck': healthcheck,
                          'files': {name: sha256(data) for name, data in files.items()},
                          'external_requirements': package.manifest['external_requirements']}
                # Register and publish under one DB writer transaction. Commit
                # happens only after files and parent directories are durable.
                self.registry.db.execute('INSERT INTO managed_installations VALUES (?,?,?)', (identity, version, _json(record)))
                package_path, env_path = self._paths(record)
                package_stage.rename(package_path)
                env_stage.rename(env_path)
                _seal(package_path)
                _seal(env_path)
                _sync(package_path.parent)
                _sync(env_path.parent)
                if activate:
                    self._activate(identity, version, record, prerequisites)
                self.registry.db.execute('INSERT INTO plugin_install_log VALUES (?,?,?,?,?)',
                    (attempt, time.time(), package.digest, 'installed', ''))
                _remove(stage)
                return record
        except Exception as exc:
            with self.registry._transaction():
                self._recover()
                self.registry.db.execute('INSERT OR REPLACE INTO plugin_install_log VALUES (?,?,?,?,?)',
                    (attempt, time.time(), package.digest, 'failed', getattr(exc, 'code', 'INSTALL_FAILED')))
            raise

    def _activate(self, plugin_id, version, record, prerequisites):
        required = {one['id'] for one in record['external_requirements']}
        supplied = prerequisites if prerequisites is not None else record.get('external_bindings', {})
        if not isinstance(supplied, dict) or set(supplied) != required:
            reject('Pin every external prerequisite in the managed runtime image before activation', 'AGENT_PREREQUISITE_MISSING')
        if required:
            pinned = {one.target: one for one in self.runtime.image.files}
            for binding in supplied.values():
                if (not isinstance(binding, dict) or set(binding) != {'path', 'sha256'}
                        or binding['path'] not in pinned or not pinned[binding['path']].executable
                        or pinned[binding['path']].sha256 != binding['sha256']):
                    reject('External executable is not pinned in the approved managed runtime', 'AGENT_PREREQUISITE_MISSING')
        record['external_bindings'] = supplied
        self.registry.db.execute('UPDATE managed_installations SET document=? WHERE plugin_id=? AND version=?',
                                 (_json(record), plugin_id, version))
        self.registry.set_state(plugin_id, version, 'active')

    def activate(self, plugin_id, version, prerequisites=None):
        with self.registry._transaction():
            if plugin_id == 'qavector.agent.claude_cli':
                from . import bundled
                binding = bundled.claude_binding()
                if version != binding['version'] or prerequisites:
                    reject('Only the backend bundled in this core build can be activated', 'PLUGIN_NOT_AVAILABLE')
                bundled.register_claude(self.registry)
                record = self.registry._package(plugin_id, version)
                if record['digest'] != binding['digest']:
                    reject('Bundled content differs from its registered pin', 'PLUGIN_NOT_AVAILABLE')
                self.registry.set_state(plugin_id, version, 'active')
                return {'delivery': 'bundled', 'binding': binding}
            record = self._record(plugin_id, version)
            self._verify(record)
            self._activate(plugin_id, version, record, prerequisites)
            return record

    def disable(self, plugin_id, version):
        with self.registry._transaction():
            self._record(plugin_id, version)
            self.registry.set_state(plugin_id, version, 'disabled')

    def retain(self, kind, reference, pins):
        if kind not in ('project', 'history') or not isinstance(reference, str) or not reference or not isinstance(pins, list):
            reject('Invalid retention reference')
        with self.registry._transaction():
            for pin in pins:
                if set(pin) != {'plugin_id', 'version', 'digest'} or self.registry._package(
                        pin['plugin_id'], pin['version'])['digest'] != pin['digest']:
                    reject('Invalid retained package pin')
            self.registry.db.execute('INSERT OR REPLACE INTO plugin_references VALUES (?,?,?)', (kind, reference, _json(pins)))

    def blockers(self, plugin_id, version):
        result = []
        def matches(pin):
            return pin['plugin_id'] == plugin_id and pin['version'] == version
        for run_id, document in self.registry.db.execute('SELECT run_id,document FROM run_locks'):
            if any(matches(pin) for pin in json.loads(document)['bindings'].values()):
                result.append({'kind': 'run', 'id': run_id})
        for kind, reference, pins in self.registry.db.execute('SELECT kind,id,pins FROM plugin_references'):
            if any(matches(pin) for pin in json.loads(pins)):
                result.append({'kind': kind, 'id': reference})
        if self.registry._package(plugin_id, version)['state'] == 'active':
            result.append({'kind': 'active', 'id': plugin_id})
        return result

    def remove(self, plugin_id, version):
        with self.registry._transaction():
            self._record(plugin_id, version)
            blockers = self.blockers(plugin_id, version)
            if blockers:
                reject('Removal blocked by references: ' + _json(blockers), 'PLUGIN_IN_USE')
            self.registry.db.execute('DELETE FROM managed_installations WHERE plugin_id=? AND version=?', (plugin_id, version))
            self.registry.db.execute('DELETE FROM packages WHERE plugin_id=? AND version=?', (plugin_id, version))
        # If interrupted here, the next recovery removes these unregistered files.
        self.recover()

    def _recover(self):
        records = [json.loads(row[0]) for row in self.registry.db.execute('SELECT document FROM managed_installations')]
        retained = {'packages': {one['digest'] for one in records},
                    'environments': {one['environment_id'] for one in records}, 'staging': set()}
        for directory, names in retained.items():
            for path in (self.root / directory).iterdir():
                if re.fullmatch('[0-9a-f]{32}|[0-9a-f]{64}', path.name) and path.name not in names:
                    _remove(path)

    def recover(self):
        with self.registry._transaction():
            self._recover()

    def worker_spec(self, reference, constraint=">=1.0.0,<2.0.0", *, pin=None):
        """Verified inputs for the Runtime proxy, never a host import or process.

        The caller must intersect grants with Project/Cycle/Node policy and use
        run locks before executing. This API does not authorize a workload.
        """
        with self.registry._transaction():
            binding = self.registry.resolve(reference, constraint, pin=pin)
            return self._worker_spec(binding)

    def locked_worker_spec(self, lock, reference):
        """Existing locks may use disabled versions; quarantine still blocks them."""
        with self.registry._transaction():
            actual = self.registry.lock_run(lock['run_id'], lock['requirements'], require_existing=True)
            if actual != lock or reference not in actual['bindings']:
                reject('Original run lock changed', 'PLUGIN_NOT_AVAILABLE')
            return self._worker_spec(actual['bindings'][reference])

    def _worker_spec(self, binding):
        record = self._record(binding['plugin_id'], binding['version'])
        package, directory = self._verify(record)
        contribution = binding['contribution']
        schemas = {name: package.schemas[contribution[name + '_schema']]
                   if name + '_schema' in contribution else True for name in ('config', 'input', 'output')}
        image = self.runtime.execution_image(directory)
        return {'binding': binding, 'runtime_id': record['runtime_id'],
                'base_image_identity': self.runtime.image.identity,
                'image': {'identity': image.identity, 'files': [vars(one) for one in image.files]},
                'argv': self.runtime.worker_argv(), 'schemas': schemas,
                'grants': record['decision']['grants'], 'external_bindings': record.get('external_bindings', {})}

    def release_reference(self, kind, reference):
        if kind not in ('project', 'history'):
            reject('Invalid retention reference kind')
        with self.registry._transaction():
            self.registry.db.execute('DELETE FROM plugin_references WHERE kind=? AND id=?', (kind, reference))

    def diagnostics(self):
        return [dict(attempt=row[0], at=row[1], digest=row[2], status=row[3], code=row[4]) for row in
                self.registry.db.execute('SELECT * FROM plugin_install_log ORDER BY at DESC LIMIT 100')]
