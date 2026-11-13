"""Capabilities carried out by their owner - the GUI - over the host's pipe.

A service, a scenario run, a cycle run: QAVector's own process does the work,
because it owns the supervisor and the launcher. An :class:`OwnerInvoker`
sends a ``dispatch`` message through the host and turns the owner's ``owner``
reports into the execution's progress:

``accepted`` (the owner took it - not success), ``running``, ``progress``, then
``succeeded`` or ``failed``. No acknowledgement within ``ack_seconds`` is
``owner_unavailable``. When the execution ends without the owner finishing it -
cancel, deadline, no answer - the owner is told ``stop_waiting``; what that
means is the resource's (a service is left alone, a run is stopped).

Thread-safe: reports come from the host's reading thread, deadlines from timers.
"""

import threading

#: How long the owner has to say it took a job.
ACK_SECONDS = 15


def _timer(seconds, work):
    timer = threading.Timer(seconds, work)
    timer.daemon = True
    timer.start()
    return timer


class OwnerInvoker:
    def __init__(self, send, *, schedule=_timer, ack_seconds=ACK_SECONDS):
        self._send, self._schedule, self._ack_seconds = send, schedule, ack_seconds
        self._lock = threading.Lock()
        self._jobs = {}            # execution id -> [job, acknowledged, ack timer]
        self.adapter = None

    def wire_op(self, capability):
        """The ``op`` a dispatch names: the capability's last part by default."""
        return capability.capability_id.rsplit(".", 1)[1]

    def requirements(self, capability, request):
        return ()

    def start(self, job):
        with self._lock:
            slot = [job, False, None]
            self._jobs[job.execution_id] = slot
        # The actor goes along: an owner that acts on a person's behalf - an
        # approval - writes down who asked. The host's word, never the request's.
        self._send({"type": "dispatch", "execution_id": job.execution_id, "op": self.wire_op(job.capability),
                    "resource_id": job.resource.resource_id, "actor": job.actor_id, "input": job.request})
        timer = self._schedule(self._ack_seconds, lambda: self._unanswered(job))
        with self._lock:
            if job.execution_id in self._jobs:
                slot[2] = timer
            elif timer is not None:
                timer.cancel()

    def _unanswered(self, job):
        with self._lock:
            slot = self._jobs.get(job.execution_id)
            if slot is None or slot[1]:
                return
        job.fail("owner_unavailable", "The owner did not take the job in time.")
        # A late owner must not act on a job that has already ended.
        self.cancel(job)

    def cancel(self, job):
        """The execution ended without the owner finishing it: tell it to stop waiting."""
        if self._forget(job.execution_id) is not None:
            self._send({"type": "stop_waiting", "execution_id": job.execution_id})

    def _forget(self, execution_id):
        with self._lock:
            slot = self._jobs.pop(execution_id, None)
        if slot is not None and slot[2] is not None:
            slot[2].cancel()
        return slot

    def knows(self, execution_id):
        with self._lock:
            return execution_id in self._jobs

    def report(self, message):
        """One ``owner`` message. Returns whether it changed the execution."""
        execution_id = message.get("execution_id")
        with self._lock:
            slot = self._jobs.get(execution_id)
            if slot is None:
                return False
            if message.get("event") in ("accepted", "running", "succeeded", "failed"):
                slot[1] = True
                if slot[2] is not None:          # answered: the "no answer" timer has nothing to do
                    slot[2].cancel()
                    slot[2] = None
        job, event = slot[0], message.get("event")
        payload = message.get("payload")
        if event == "accepted":
            return job.dispatched(payload)
        if event == "running":
            return job.running(payload)
        if event == "progress":
            kind = message.get("kind")
            return isinstance(kind, str) and job.progress("owner." + kind[:64], payload)
        if event == "succeeded":
            done = job.succeed(message.get("output"))
        elif event == "failed":
            error = message.get("error") if isinstance(message.get("error"), dict) else {}
            code = error.get("code") if isinstance(error.get("code"), str) else "owner_failed"
            text = error.get("message") if isinstance(error.get("message"), str) else None
            done = job.fail(code[:64], (text or code)[:500], message.get("output"))
        else:
            return False
        self._forget(execution_id)
        return done
