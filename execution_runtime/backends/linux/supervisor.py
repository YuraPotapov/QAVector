"""Bounded output capture and private native instance state."""

import threading
import time
import json
from array import array
from pathlib import Path


class LogCapture(threading.Thread):
    def __init__(self, stream, path, redactions=(), limit=1024 * 1024):
        super().__init__(daemon=True)
        self.stream, self.path = stream, Path(path)
        variants = {raw.encode("utf-8") for one in redactions if one for raw in
                    (one, json.dumps(one, ensure_ascii=True)[1:-1], json.dumps(one, ensure_ascii=False)[1:-1])}
        self.redactions = tuple(sorted(variants, key=len, reverse=True))
        self.prefixes = []
        for value in self.redactions:
            prefix = array("I", [0]) * len(value)
            length = 0
            for index in range(1, len(value)):
                while length and value[index] != value[length]:
                    length = prefix[length - 1]
                if value[index] == value[length]:
                    length += 1
                prefix[index] = length
            self.prefixes.append((value, prefix))
        self.limit, self.written = limit, 0
        self.tail = b""
        self.truncated = False
        self.error = False

    def _emit(self, output, block):
        self.tail = (self.tail + block)[-8192:]
        available = max(0, self.limit - self.written)
        output.write(block[:available])
        self.written += min(available, len(block))
        self.truncated |= len(block) > available

    def run(self):
        pending = b""
        read = getattr(self.stream, "read1", self.stream.read)
        try:
            with open(self.path, "xb") as output:
                while True:
                    block = read(65536)
                    if not block:
                        break
                    pending += block
                    pending = self._consume(output, pending)
                    output.flush()
                self._consume(output, pending, final=True)
        except OSError:
            self.error = True
            try:
                while read(65536):
                    pass
            except OSError:
                pass
        finally:
            self.redactions = ()
            self.prefixes = []
            self.stream.close()

    def _consume(self, output, pending, final=False):
        held = 0
        if not final:
            for value, prefix in self.prefixes:
                if len(value) == 1:
                    continue
                length = 0
                for byte in pending[-(len(value) - 1):]:
                    while length and byte != value[length]:
                        length = prefix[length - 1]
                    if byte == value[length]:
                        length += 1
                    if length == len(value):
                        length = prefix[length - 1]
                held = max(held, length)
        cutoff = len(pending) if final else len(pending) - held
        cursor, chunks = 0, []
        while cursor < cutoff:
            matches = [(pending.find(value, cursor), value) for value in self.redactions]
            matches = [(start, value) for start, value in matches if 0 <= start < cutoff]
            if not matches:
                chunks.append(pending[cursor:cutoff])
                cursor = cutoff
                break
            start, value = min(matches, key=lambda one: (one[0], -len(one[1])))
            chunks.extend((pending[cursor:start], b"[redacted]"))
            cursor = start + len(value)
        self._emit(output, b"".join(chunks))
        return pending[cursor:]


class Instance:
    def __init__(self, token, request, directory, fds, seccomp, group):
        import os
        from .recovery import process_identity
        self.token, self.request, self.directory = token, request, Path(directory)
        self.fds, self.seccomp, self.group = fds, seccomp, group
        self.process = None
        self.lease_fd = None
        self.owner_identity = process_identity(os.getpid())
        self.init_identity = None
        self.init_pid = None
        self.init_pidfd = None
        self.captures = []
        self.status_thread = None
        self.done = threading.Event()
        self.stopping = threading.RLock()
        self.reason = ""
        self.exit_code = None
        self.state = "prepared"
        self.created_at = time.time()
        self.started_at = None
        self.ended_at = None
        self.samples = []
        self.child_exit_reported = False
        self.secret_mounts = []
        self.secret_locations = []
        self.secret_redactions = ()
        self.environment_fd = None
        self.environment_names = []
        self.gateway = None

    def close_preparation(self):
        import os
        for fd in self.fds:
            os.close(fd)
        self.fds = []
        if self.seccomp is not None:
            self.seccomp.close()
            self.seccomp = None

    def sample(self):
        if self.group is not None:
            self.samples.append({"at": time.time(), "usage": self.group.metrics()})
            self.samples = self.samples[-256:]
