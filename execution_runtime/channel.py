"""Bounded stdio transport; process ownership remains with RuntimeBackend.

Protocol bytes never become ordinary runtime stdout logs. This channel does
not parse messages, dispatch host calls or start/terminate processes.
"""

import os
import math
import queue
import select
import threading
import time

from .errors import RuntimePolicyError


class StdioChannel:
    def __init__(self, max_frame_bytes=256 * 1024, max_pending=64):
        if type(max_frame_bytes) is not int or not 1024 <= max_frame_bytes <= 1024 * 1024:
            raise RuntimePolicyError("invalid_channel", "Invalid protocol frame limit.")
        if type(max_pending) is not int or not 1 <= max_pending <= 64:
            raise RuntimePolicyError("invalid_channel", "Invalid pending protocol limit.")
        self.max_frame_bytes = max_frame_bytes
        self._frames = queue.Queue(maxsize=max_pending)
        self._write_lock = threading.Lock()
        self._stdin = None
        self._stdout = None
        self._attached = False
        self._eof = False
        self._problem = ""

    def attach(self, stdin, stdout):
        if self._attached:
            raise RuntimePolicyError("channel_reused", "Protocol channel belongs to one process only.")
        self._attached = True
        self._stdin, self._stdout = stdin, stdout
        os.set_blocking(stdin.fileno(), False)
        return self

    def send(self, raw, timeout=1.0):
        if (not isinstance(raw, bytes) or not raw.endswith(b"\n")
                or len(raw) > self.max_frame_bytes):
            raise RuntimePolicyError("protocol_frame_invalid", "Expected a bounded line frame.")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise RuntimePolicyError("invalid_channel", "Protocol write timeout must be bounded.")
        if self._stdin is None or self._eof or self._problem:
            raise RuntimePolicyError("protocol_closed", "Worker protocol channel is unavailable.")
        deadline = time.monotonic() + timeout
        with self._write_lock:
            try:
                fd = self._stdin.fileno()
                pending = raw
                while pending:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                        raise RuntimePolicyError("protocol_write_timeout", "Worker did not consume protocol input.")
                    try:
                        pending = pending[os.write(fd, pending):]
                    except BlockingIOError:
                        continue
            except (OSError, ValueError):
                raise RuntimePolicyError("protocol_closed", "Worker input stream closed.") from None

    def receive(self, timeout=0):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 60:
            raise RuntimePolicyError("invalid_channel", "Protocol read timeout must be bounded.")
        if self._problem:
            raise RuntimePolicyError(self._problem, "Worker protocol stream failed.")
        try:
            return self._frames.get(timeout=timeout)
        except queue.Empty:
            if self._eof:
                raise RuntimePolicyError("protocol_closed", "Worker output stream closed.")
            return None

    def read(self, size=-1):
        """Consumed by LogCapture: handle protocol internally, log no payload."""
        try:
            while True:
                raw = self._stdout.readline(self.max_frame_bytes + 1)
                if not raw:
                    break
                if self._problem:
                    continue  # Drain until Runtime terminates the process.
                if len(raw) > self.max_frame_bytes or not raw.endswith(b"\n"):
                    self._problem = "protocol_frame_invalid"
                    continue
                try:
                    self._frames.put_nowait(raw)
                except queue.Full:
                    self._problem = "protocol_backpressure"
        except OSError:
            self._problem = "protocol_read_failed"
        finally:
            self._eof = True
        return b""

    def close(self):
        if self._stdout is not None:
            self._stdout.close()

    def close_input(self):
        with self._write_lock:
            if self._stdin is not None:
                self._stdin.close()

    @property
    def problem(self):
        return self._problem
