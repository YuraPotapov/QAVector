"""Headless plugin manager. All import approvals are explicit JSON documents."""

import argparse
import json
from pathlib import Path
import sqlite3

from qavector_sdk import ContractError
from execution_runtime.errors import RuntimePolicyError
from .archive import json_document
from .installer import PluginManager
from .managed_runtime import ManagedRuntime


def main(argv=None):
    import runtime_paths
    parser = argparse.ArgumentParser(prog='qavector --plugins')
    parser.add_argument('--root', default=str(Path(runtime_paths.user_data_root()) / 'plugins'))
    parser.add_argument('--runtime', help='Approved managed Python descriptor; defaults to bundled runtime')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list')
    sub.add_parser('catalog')
    sub.add_parser('check')
    sub.add_parser('diagnostics')
    sub.add_parser('recover')
    describe = sub.add_parser('show')
    describe.add_argument('plugin_id')
    describe.add_argument('version')
    inspect = sub.add_parser('inspect')
    inspect.add_argument('archive')
    install = sub.add_parser('import')
    install.add_argument('archive')
    install.add_argument('--approval', required=True, help='Reviewed digest/publisher_identity/grants JSON')
    install.add_argument('--activate', action='store_true')
    install.add_argument('--prerequisites', help='JSON mapping of external IDs to pinned path/sha256')
    for name in ('activate', 'rollback', 'disable', 'remove'):
        action = sub.add_parser(name)
        action.add_argument('plugin_id')
        action.add_argument('version')
        if name in ('activate', 'rollback'):
            action.add_argument('--prerequisites')
    key = sub.add_parser('trust-key')
    key.add_argument('key_id')
    key.add_argument('public_key', help='Ed25519 public key in base64; enrolling this key is an explicit trust decision')
    args = parser.parse_args(argv)
    manager = None
    try:
        runtime_path = Path(args.runtime) if args.runtime else Path(runtime_paths.app_root()) / 'runtime' / 'plugin-python.json'
        metadata_only = args.command in ('list', 'catalog', 'check', 'show', 'diagnostics', 'recover', 'disable', 'remove', 'trust-key')
        runtime = ManagedRuntime.read(runtime_path, Path(args.root) / 'runtime-state') if not metadata_only and runtime_path.is_file() else None
        if args.runtime and runtime is None and not metadata_only:
            raise ContractError('PLUGIN_RUNTIME_INCOMPATIBLE', 'Requested managed runtime descriptor is missing')
        manager = PluginManager(args.root, runtime)
        if args.command == 'list':
            result = manager.list()
        elif args.command == 'catalog':
            from .catalog import inventory
            result = inventory(manager)
        elif args.command == 'show':
            result = manager.describe(args.plugin_id, args.version)
        elif args.command == 'inspect':
            result = manager.inspect(args.archive)
        elif args.command == 'import':
            result = manager.install(args.archive, json_document(Path(args.approval).read_bytes()), activate=args.activate,
                prerequisites=json_document(Path(args.prerequisites).read_bytes()) if args.prerequisites else None)
        elif args.command in ('activate', 'rollback'):
            result = manager.activate(args.plugin_id, args.version,
                prerequisites=json_document(Path(args.prerequisites).read_bytes()) if args.prerequisites else None)
        elif args.command in ('disable', 'remove'):
            result = getattr(manager, args.command)(args.plugin_id, args.version)
        elif args.command == 'trust-key':
            result = manager.enroll_key(args.key_id, args.public_key)
        else:
            result = getattr(manager, args.command)()
        compatible = result['compatible'] if args.command == 'check' else (result['compatibility']['compatible'] if args.command == 'inspect' else True)
        print(json.dumps({'ok': compatible, 'result': result}, ensure_ascii=True))
        return 0 if compatible else 2
    except (ContractError, RuntimePolicyError, OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({'ok': False, 'code': getattr(exc, 'code', 'PLUGIN_MANAGER_ERROR'),
                          'message': str(exc) if isinstance(exc, (ContractError, RuntimePolicyError)) else 'Plugin manager operation failed'}))
        return 2
    finally:
        if manager is not None:
            manager.close()


if __name__ == '__main__':
    raise SystemExit(main())
