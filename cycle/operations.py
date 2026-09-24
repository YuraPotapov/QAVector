"""Durable receipts for billable calls inside a node.

Plugins save the provider's answer here before parsing it or writing reports.
A completed successful call can then be replayed locally. A call recorded as
started without a receipt is ambiguous and must not be charged again silently.
"""

import os
from dataclasses import asdict

from cycle import checkpoints
from cycle.registry import PluginResult
from domain.cycle import Artifact


def result_document(result):
    return asdict(result)


def result_from_document(raw):
    raw = dict(raw)
    raw["artifacts"] = [Artifact(**item) for item in raw.get("artifacts", [])]
    return PluginResult(**raw)


class Operation:
    def __init__(self, context, step, name, request):
        self.context = context
        self.step = step
        self.directory = context.step_dir(step.id, "operations",
                                          checkpoints.digest([name, request]))
        self.path = os.path.join(self.directory, "receipt.json")
        self.epoch = getattr(context.run, "resume_count", 0)
        self.record = checkpoints._read(self.path) if os.path.exists(self.path) else None
        self.store = checkpoints.Store(context.workspace)

    def cached(self):
        if self.record and self.record.get("state") == "complete":
            if self.record.get("ok") or self.record.get("epoch") == self.epoch:
                self.context.stage(self.step.id, {
                    "kind": "phase", "status": "done", "title": "Saved service response",
                    "detail": "Reused the recorded response; no service request was made."})
                return self.record["answer"]
        return None

    @property
    def uncertain(self):
        return bool(self.record and self.record.get("state") == "started")

    def begin(self):
        if self.uncertain:
            raise checkpoints.CheckpointError(
                "The service call in %s has an uncertain outcome. Inspect %s "
                "before explicitly retrying; no new request was sent."
                % (self.step.id, self.directory))
        if self.record:
            self.store.write(os.path.join(self.directory, "history",
                                           "%04d.json" % self.record.get("epoch", 0)),
                             self.record)
        self.store.write(self.path, {"state": "started", "epoch": self.epoch})

    def complete(self, answer, ok):
        self.store.write(self.path, {"state": "complete", "epoch": self.epoch,
                                     "ok": ok, "answer": answer})

    def process_context(self):
        return _ProcessContext(self.context, self.directory)


class _ProcessContext:
    def __init__(self, context, directory):
        self.context, self.directory = context, directory

    def __getattr__(self, name):
        return getattr(self.context, name)

    def step_dir(self, step_id, *parts):
        directory = os.path.join(self.directory, *parts)
        os.makedirs(directory, exist_ok=True)
        return directory
