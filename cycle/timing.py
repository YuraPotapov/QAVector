"""Bounded execution-time receipts, owned by the scheduler, never a UI tick.

GUI confirmations are diagnostic supervision evidence, not the scheduler lease
planned separately. Missing confirmations freeze accounting; they grant no
permission to start work. CLI runs are accounted while the scheduler is awake.
Large clock gaps split segments instead of adding a suspend/offline interval.
"""

import copy
import threading
import time

TTL = 5.0
RECEIPT_INTERVAL = 5.0
STEP_FIELDS = ("active_elapsed_ms", "execution_generation", "execution_segments",
               "last_activity_at", "progress", "timeout_remaining_ms")


def step_snapshot(step):
    return {name: copy.deepcopy(getattr(step, name)) for name in STEP_FIELDS}


class Confirmations:
    def __init__(self, monotonic=time.monotonic, wall=time.time):
        self.monotonic, self.wall = monotonic, wall
        self.lock = threading.Lock()
        self.last = None
        self.epoch = 0

    def confirm(self):
        now, wall = self.monotonic(), self.wall()
        with self.lock:
            if self.last is None or (now - self.last[0] > TTL or
                                      abs(wall - self.last[1] - (now - self.last[0])) > TTL):
                self.epoch += 1
            self.last = (now, wall)

    def state(self, now):
        with self.lock:
            return self.epoch, self.last is not None and 0 <= now - self.last[0] <= TTL


class Activity:
    """Workers report evidence here; only the scheduler changes StepRun."""
    def __init__(self):
        self.lock = threading.Lock()
        self.pending = {}

    def note(self, step, progress=None):
        with self.lock:
            old = self.pending.get(step, {})
            self.pending[step] = dict(old, last_activity_at=time.time())
            if progress is not None:
                self.pending[step]["progress"] = copy.deepcopy(progress)

    def drain(self):
        with self.lock:
            pending, self.pending = self.pending, {}
        return pending


class Clock:
    def __init__(self, run, confirmations=None, monotonic=time.monotonic, wall=time.time):
        self.run, self.confirmations = run, confirmations
        self.monotonic, self.wall = monotonic, wall
        self.last_mono, self.last_wall = monotonic(), wall()
        self.epoch, self.valid = self._state(self.last_mono)
        run.active_elapsed_ms = run.active_elapsed_ms or 0.0
        self._seal(run, "previous execution")
        for step in run.steps.values():
            self._seal(step, "previous execution")

    def _state(self, now):
        return self.confirmations.state(now) if self.confirmations else (0, True)

    @staticmethod
    def _seal(record, reason):
        for segment in record.execution_segments:
            if segment.get("ended_at") is None:
                segment["ended_at"] = segment["confirmed_at"]
                segment["reason"] = reason

    def _advance(self, record, delta, wall, valid):
        if not valid:
            self._seal(record, "supervision gap")
            return
        if not record.execution_segments or record.execution_segments[-1].get("ended_at") is not None:
            record.execution_segments.append({
                "started_at": wall - delta, "confirmed_at": wall,
                "ended_at": None, "elapsed_ms": 0.0,
                "generation": getattr(record, "execution_generation", 1)})
        segment = record.execution_segments[-1]
        segment["confirmed_at"] = wall
        segment["elapsed_ms"] += delta * 1000
        record.active_elapsed_ms = (record.active_elapsed_ms or 0.0) + delta * 1000

    def tick(self):
        now, wall = self.monotonic(), self.wall()
        epoch, valid = self._state(now)
        valid = valid and not self.run.paused and self.run.status not in ("paused", "interrupted")
        delta = max(0.0, now - self.last_mono)
        continuous = (self.valid and valid and epoch == self.epoch and delta <= TTL
                      and abs((wall - self.last_wall) - delta) <= TTL)
        if not continuous:
            # Close at the last confirmed endpoint; never bridge the gap.
            self._seal(self.run, "supervision gap")
        self._advance(self.run, delta if continuous else 0, wall, valid)
        for step in self.run.steps.values():
            if step.status not in ("running", "waiting"):
                continue
            if not continuous:
                self._seal(step, "supervision gap")
            self._advance(step, delta if continuous else 0, wall, valid)
        self.last_mono, self.last_wall = now, wall
        self.epoch, self.valid = epoch, valid

    def start(self, step):
        if step.execution_segments and step.execution_segments[-1].get("generation") != step.execution_generation:
            self._seal(step, "new generation")
        step.active_elapsed_ms = step.active_elapsed_ms or 0.0
        step.last_activity_at = self.wall()

    def finish(self, step, reason="finished"):
        self._seal(step, reason)

    def close(self):
        self.tick()
        self._seal(self.run, "run ended")
        for step in self.run.steps.values():
            self._seal(step, "run ended")

    def snapshot(self):
        return {"active_elapsed_ms": self.run.active_elapsed_ms,
                "execution_segments": copy.deepcopy(self.run.execution_segments),
                "steps": {name: step_snapshot(step) for name, step in self.run.steps.items()
                          if step.active_elapsed_ms is not None},
                "confirmed_at": self.last_wall, "ttl_ms": int(TTL * 1000)}
