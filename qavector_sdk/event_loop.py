"""POSIX asyncio wakeups over pipes, compatible with network-denied workers.

CPython's selector loop normally creates a socketpair even for a plugin that
never uses networking. Keep the same loop scheduling and use anonymous pipes
for its private wakeup channel, without relaxing Runtime socket restrictions.
The small private-hook adapter is covered by the supported Python CI matrix.
"""

import asyncio
import os


class _PipeEnd:
    def __init__(self, fd):
        self.fd = fd

    def fileno(self):
        return self.fd

    def recv(self, size):
        return os.read(self.fd, size)

    def send(self, data):
        return os.write(self.fd, data)

    def close(self):
        os.close(self.fd)
        self.fd = -1


class PipeEventLoop(asyncio.SelectorEventLoop):
    def _make_self_pipe(self):
        reader, writer = os.pipe()
        os.set_blocking(reader, False)
        os.set_blocking(writer, False)
        self._ssock, self._csock = _PipeEnd(reader), _PipeEnd(writer)
        self._internal_fds += 1
        self._add_reader(reader, self._read_from_self)


def run(coroutine):
    loop = PipeEventLoop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coroutine)
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
        asyncio.set_event_loop(None)
        loop.close()
