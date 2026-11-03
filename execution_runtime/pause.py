"""Active-time accounting and opt-in ownership of pausable Runtime jobs."""

from contextlib import contextmanager
import threading
import time


class ActiveClock:
    def __init__(self):
        self.lock = threading.RLock()
        self.since, self.total = None, 0.0

    def now(self):
        with self.lock:
            return (time.monotonic() if self.since is None else self.since) - self.total

    def pause(self):
        with self.lock:
            if self.since is None:
                self.since = time.monotonic()

    def resume(self):
        with self.lock:
            if self.since is not None:
                self.total += time.monotonic() - self.since
                self.since = None


class PauseGroup:
    """One attempt's gate, active clock and explicitly registered native jobs."""
    def __init__(self, cancel):
        self.cancel, self.clock = cancel, ActiveClock()
        self.lock = threading.RLock()
        self.jobs = {}
        self.paused, self.closed = False, False

    def wait(self):
        while self.paused and not self.cancel.is_set():
            self.cancel.wait(0.02)

    @contextmanager
    def io(self):
        # A bounded protocol write or launch completes before a freeze. Never
        # freeze the consumer halfway through a frame larger than its pipe.
        while True:
            self.wait()
            self.lock.acquire()
            if not self.paused or self.cancel.is_set():
                break
            self.lock.release()
        try:
            yield
        finally:
            self.lock.release()

    def add(self, runtime, handle):
        with self.lock:
            self.jobs[handle.instance_id] = (runtime, handle)

    def remove(self, handle):
        with self.lock:
            self.jobs.pop(handle.instance_id, None)

    def pause(self):
        with self.lock:
            if self.closed or self.paused or self.cancel.is_set():
                return
            self.clock.pause()
            try:
                for runtime, handle in self.jobs.values():
                    runtime.pause(handle)
                self.paused = True
            except Exception:
                # Stop owns reconciliation; no successful paused state is
                # reported if even one job could not be held.
                self.cancel.set('Native Runtime pause could not be confirmed')
                self.clock.resume()

    def release(self):
        with self.lock:
            if not self.paused:
                return
            if not self.cancel.is_set():
                try:
                    for runtime, handle in self.jobs.values():
                        runtime.resume(handle)
                except Exception:
                    self.cancel.set('Native Runtime continuation could not be confirmed')
            self.clock.resume()
            self.paused = False

    def close(self):
        with self.lock:
            self.closed = True
            self.clock.resume()
            self.paused = False
            self.jobs.clear()
