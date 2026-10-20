"""Shared lifecycle entry point: native details remain behind RuntimeBackend."""

import threading
import time
import math
import re
import uuid

from .errors import RuntimePolicyError


class ExecutionRuntime:
    def __init__(self, backend, *, audit=False, event_sink=None):
        self.backend = backend
        self.audit = bool(audit)
        self.event_sink = event_sink
        self._restart_lock = threading.Lock()

    def _event(self, kind, policy=None, handle=None, code="", result=None, operation=""):
        if not self.audit and self.event_sink is None:
            return
        record = {"schema_version": 1, "event_id": uuid.uuid4().hex, "kind": kind,
                  "at": time.time(), "provider": self.backend.provider}
        if policy is not None:
            record.update(policy_digest=policy.digest, effective_policy=policy.to_document())
        if handle is not None:
            for name, pattern in (("instance_id", r"[0-9a-f]{32}"), ("policy_digest", r"[0-9a-f]{64}")):
                value = getattr(handle, name, None)
                if isinstance(value, str) and re.fullmatch(pattern, value):
                    record[name] = value
        if code:
            record["code"] = code if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code) else "runtime_error"
        if operation:
            record["operation"] = operation
        if result is not None:
            record["exit_code"] = result.get("exit_code")
            record["state"] = result.get("state", "")
            reason = result.get("reason", "")
            if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", reason):
                record["reason"] = reason
        if self.audit:
            self.backend.record_event(record)
        if self.event_sink is not None:
            try:
                self.event_sink(kind, **{key: value for key, value in record.items() if key != "kind"})
            except Exception:
                pass  # The durable record, rather than a connected UI, gates execution.

    def _call(self, operation, function, *, policy=None, handle=None):
        try:
            return function()
        except RuntimePolicyError as exc:
            self._event("runtime.rejected", policy, handle, code=exc.code, operation=operation)
            raise
        except OSError:
            self._event("runtime.rejected", policy, handle, code="runtime_io_error", operation=operation)
            raise RuntimePolicyError("runtime_io_error", "A native runtime operation failed.") from None

    def preflight(self, policy):
        return self.backend.preflight(policy)

    def prepare(self, request):
        self._event("runtime.prepare", policy=request.policy)
        prepared = self._call("prepare", lambda: self.backend.prepare(request), policy=request.policy)
        try:
            self._event("runtime.prepared", policy=request.policy, handle=prepared)
        except RuntimePolicyError:
            self.backend.cleanup(prepared)
            raise
        return prepared

    def start(self, prepared):
        self._event("runtime.start", handle=prepared)
        handle = self._call("start", lambda: self.backend.start(prepared), handle=prepared)
        try:
            self._event("runtime.started", handle=handle)
        except RuntimePolicyError:
            self.backend.stop(handle, time.monotonic())
            self.backend.wait(handle)
            self.backend.cleanup(handle)
            raise
        return handle

    def stop(self, handle, grace_seconds=2.0):
        if (type(grace_seconds) not in (int, float) or not math.isfinite(grace_seconds)
                or not 0 <= grace_seconds <= 60):
            raise RuntimePolicyError("invalid_deadline", "Stop grace must be a finite duration between 0 and 60 seconds.")
        result = self._call("stop", lambda: self.backend.stop(handle, time.monotonic() + grace_seconds), handle=handle)
        self._event("runtime.stopped", handle=handle)
        return result

    def wait(self, handle):
        result = self._call("wait", lambda: self.backend.wait(handle), handle=handle)
        self._event("runtime.exited", handle=handle, result=result)
        return result

    def inspect(self, handle):
        return self.backend.inspect(handle)

    def cleanup(self, handle):
        result = self._call("cleanup", lambda: self.backend.cleanup(handle), handle=handle)
        self._event("runtime.cleaned", handle=handle)
        return result

    def recover(self):
        result = self._call("recover", self.backend.recover)
        self._event("runtime.recovered")
        return result

    def shutdown(self, grace_seconds=2.0):
        if (type(grace_seconds) not in (int, float) or not math.isfinite(grace_seconds)
                or not 0 <= grace_seconds <= 60):
            raise RuntimePolicyError("invalid_deadline", "Shutdown grace must be a finite duration between 0 and 60 seconds.")
        result = self._call("shutdown", lambda: self.backend.shutdown(time.monotonic() + grace_seconds))
        self._event("runtime.shutdown")
        return result

    def restart(self, handle, request, grace_seconds=2.0):
        with self._restart_lock:
            self.stop(handle, grace_seconds)
            self.wait(handle)
            self.cleanup(handle)
            return self.start(self.prepare(request))
