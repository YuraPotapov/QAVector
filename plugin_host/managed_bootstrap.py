"""Pinned entrypoint inside the managed image, never run in the core process."""

import importlib
import json
import sys


def main():
    # -I -S: no cwd/user/system site discovery and no .pth execution.
    sys.path[:0] = ['/qavector/plugins', '/qavector/site']
    if len(sys.argv) == 4 and sys.argv[1] == 'healthcheck':
        # Startup imports receive no process/thread budget. Hard process-local
        # ceilings cannot be raised by unprivileged payload code. Runtime still
        # owns wall time, scratch capacity and full descendant cleanup.
        import resource
        for kind, limit in ((resource.RLIMIT_AS, 512 * 1024 ** 2),
                            (resource.RLIMIT_CPU, 5), (resource.RLIMIT_NPROC, 0),
                            (resource.RLIMIT_NOFILE, 64), (resource.RLIMIT_FSIZE, 1024 ** 2)):
            resource.setrlimit(kind, (limit, limit))
        contributions = json.loads(sys.argv[2])
        for item in contributions:
            module, attribute = item['entrypoint'].split(':')
            factory = importlib.import_module(module)
            for part in attribute.split('.'):
                factory = getattr(factory, part)
            if not callable(factory):
                raise ValueError('Entrypoint is not callable')
        print(json.dumps({'healthcheck': sys.argv[3]}))
        return 0
    if sys.argv[1:] == ['cycle-worker']:
        # The initial Cycle transport negotiates no process creation. This hard
        # ceiling is inherited by payload code and cannot be raised by it.
        import resource
        resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    if sys.argv[1:] in (['worker'], ['cycle-worker']):
        from qavector_sdk.worker import main as worker
        return worker()
    raise ValueError('Unsupported managed worker invocation')


if __name__ == '__main__':
    raise SystemExit(main())
