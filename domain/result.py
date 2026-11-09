"""Result models: the outcome of running steps, flows and a whole run.

Kept separate from actions: a StepResult records what
happened for one step, a FlowResult aggregates a scenario against one session,
and a RunResult aggregates everything. All are dataclasses so they serialize to
JSON cleanly via ``dataclasses.asdict``.
"""

from dataclasses import dataclass, field

# Step / flow statuses. FAIL = an assertion or action returned a negative
# result; ERROR = an unexpected exception (selector timeout, compile error).
PASS = "pass"
FAIL = "fail"
ERROR = "error"


@dataclass
class StepResult:
    index: int
    action: str
    status: str                # PASS | FAIL | ERROR
    target: str = None
    value: str = None
    message: str = ""
    duration_ms: float = 0.0
    attempts: int = 1


@dataclass
class FlowResult:
    scenario: str
    session: str
    status: str = PASS
    steps: list = field(default_factory=list)   # list[StepResult]
    duration_ms: float = 0.0
    artifacts_dir: str = None
    error: str = None
    # Each time a person stepped back and had the run go on from there (see
    # engine.stepping): {"after_step", "back_to_step", "reason", "at"}. Those
    # steps ran twice on the same page, so a pass with any of these is a pass
    # somebody helped along, and says so.
    interventions: list = field(default_factory=list)

    @property
    def ok(self):
        return self.status == PASS


@dataclass
class RunResult:
    flows: list = field(default_factory=list)   # list[FlowResult]

    @property
    def ok(self):
        return all(f.ok for f in self.flows)

    @property
    def exit_code(self):
        return 0 if self.ok else 1
