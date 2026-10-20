"""Cycle models: a saved automation graph, and the record of running one.

A **Cycle** is a graph of steps that orchestrates the things this application
can already do - start a service, run a scenario, run a command, write a report
- and says in what order, what may happen at the same time, and what depends on
what. A **CycleRun** is one execution of that graph.

This is deliberately *not* the same thing as a Flow. A Flow (``domain/flow.py``)
is a scenario or a reusable block: a flat list of browser actions driven against
one page. A Cycle sits a level above and may run a whole Flow as one of its
steps. The two never share a word - the package, the files, the CLI flags and
the events all say "cycle" - because the core has used "flow" for a scenario
since the beginning and one of the two names had to stay put.

Like the rest of ``domain/``, everything here is a plain dataclass with no I/O
and no third-party imports, so a cycle can be built and asserted about without
pyyaml, without Playwright, and without Qt. Everything that *does* something -
parsing, validation, ring detection, layout - lives in ``cycle/model.py``, the
same way ``engine/compiler.py`` holds what ``domain/flow.py`` does not.
"""

from dataclasses import dataclass, field

# Step and run statuses. The first five are terminal for scheduling purposes;
# RUNNING and WAITING are not.
#
# SKIPPED and CANCELLED say different things and both are worth having: a step
# is SKIPPED because of something about the graph (a dependency failed, its
# `if:` was false, it is disabled), and CANCELLED because the run as a whole
# stopped (the user asked, or another step failed under `on_failure: stop`).
# Reading a finished run, that difference is the difference between "this never
# applied" and "this never got its turn".
PENDING = "pending"
RUNNING = "running"
WAITING = "waiting"
SUCCESS = "success"
FAILED = "failed"
TIMEOUT = "timeout"
SKIPPED = "skipped"
CANCELLED = "cancelled"

#: Statuses a step can end in. Once a step wears one of these the scheduler
#: never looks at it again, and its dependents can be resolved.
TERMINAL = (SUCCESS, FAILED, TIMEOUT, SKIPPED, CANCELLED)

#: What a step's dependents need to see before they may start. Only SUCCESS -
#: a step that timed out or was skipped did not produce the outputs the next
#: step was written against.
PASSING = (SUCCESS,)

#: `on_failure:` - what a failing step does to the rest of the run.
STOP = "stop"
CONTINUE = "continue"
ON_FAILURE = (STOP, CONTINUE)

#: `retry.backoff:` - how the wait between tries grows. FIXED waits
#: `retry.delay` every time; EXPONENTIAL doubles it before each further try.
FIXED = "fixed"
EXPONENTIAL = "exponential"
BACKOFF = (FIXED, EXPONENTIAL)

#: How a run was started. Only MANUAL exists today; the field is here because a
#: run's account of itself is worth nothing later if it cannot say what started
#: it, and adding the field afterwards means every old run answers "unknown".
MANUAL = "manual"

#: Core policies governing implicit approval immediately before node submit.
EXECUTION_MODES = ("auto", "important", "approve")


@dataclass
class CycleStep:
    """One node of a cycle: which plugin to run, and under what conditions.

    ``settings`` is the step's ``with:`` mapping - whatever that plugin's
    metadata declares it takes. It is named ``settings`` rather than ``with``
    because ``with`` is a Python keyword, and rather than ``with_`` because a
    trailing underscore in a dataclass field reads as an accident every time
    somebody meets it. The wire name stays ``with``.

    ``needs`` is what makes this a graph rather than a list. Steps that do not
    name each other, directly or transitively, may run at the same time.
    """

    id: str
    plugin: str
    label: str = ""                    # human name; falls back to id when blank
    needs: tuple = ()                  # ids of steps that must succeed first
    settings: dict = field(default_factory=dict)    # the `with:` mapping
    condition: str = ""                # `if:`; blank means always
    timeout: float = None              # seconds; None -> no deadline
    retry_attempts: int = 1            # total tries, not extra tries
    retry_delay: float = 0.0           # seconds between tries
    on_failure: str = STOP
    disabled: bool = False
    source_index: int = 0              # position in the file; decides layout ties
    #: How the wait between tries grows - see :data:`BACKOFF`.
    retry_backoff: str = FIXED
    #: Which phase of the cycle this step belongs to, for a reader: the canvas
    #: frames a group's steps together and can fold them into one box. It
    #: changes nothing about when or whether the step runs.
    group: str = ""
    phase: str = ""                    # explicit execution boundary; group stays visual
    important: bool = False
    importance_reason: str = ""
    runtime: dict = field(default_factory=dict)  # node restrictions; inherits execution.runtime
    #: Narrows the cycle's ``restrictions`` for this step only - see cycle/restrict.py.
    restrictions: dict = field(default_factory=dict)
    #: Runs without the cycle's restrictions, and the canvas says so.
    system: bool = False

    @property
    def title(self):
        """What to call this step in a UI or a log line."""
        return self.label or self.id

    def retry_wait(self, attempt):
        """Seconds to wait before try ``attempt`` (the second try is 2).

        Exponential doubles the delay each time: 5, 10, 20... The step's own
        ``timeout:`` still bounds the whole of it, waits included, so a long
        run of doublings ends where the deadline says rather than where the
        arithmetic would.
        """
        if attempt < 2:
            return 0.0
        if self.retry_backoff == EXPONENTIAL:
            return self.retry_delay * (2 ** (attempt - 2))
        return self.retry_delay


@dataclass
class Subject:
    """What a cycle works on, as the cycle file declares it.

    A run of a development cycle is about one task; a run of a demo is about
    nothing but itself. Without saying which, every run looks like the last one
    from the outside, and "is this still QA-934 or the next task?" is a
    question only somebody reading a step's outputs could answer.

    Every field but ``kind`` and ``pin`` is a ``${...}`` expression, resolved
    during the run once the steps it reads have finished - the subject of a run
    that has to *find* its task is not known when it starts.
    """

    kind: str = ""          # a word for the reader: task, branch, release
    key: str = ""           # the identity; runs with the same key are one session
    title: str = ""         # what to call it
    memory: str = ""        # the memory record holding its state across runs
    #: A variable that fixes the subject when set, so "run it again on this
    #: task" can be said on the command line rather than hoped for.
    pin: str = ""


#: How often a listener asks, in seconds, when a trigger does not say.
WATCH_EVERY = 300
#: The least it may ask: a Jira instance polled every few seconds by every
#: open copy of the application is somebody else's outage.
WATCH_EVERY_MIN = 60


@dataclass
class Trigger:
    """Something that offers to start the cycle on its own.

    One kind so far: ``watch`` names a ``jira.issues`` step, and while the
    application is open the query that step runs is repeated every ``every``
    seconds. An issue it has not seen before is offered to a person - "start
    work on it?" - and, if they agree, the cycle runs with ``pin`` set to that
    issue's key. Nothing starts without the question being answered.
    """

    id: str
    watch: str = ""         # the jira.issues step whose query is repeated
    every: int = WATCH_EVERY
    pin: str = ""           # the variable an offered key is run with
    #: Off keeps the trigger - its step and its schedule - without listening,
    #: so turning it back on does not mean writing it again.
    enabled: bool = True


@dataclass
class CyclePhase:
    id: str
    title: str = ""
    purpose: str = ""
    policy: str = "automatic"
    approval: str = ""
    outcome: str = ""
    deliverables: tuple = ()           # step ids whose artifacts form the receipt
    effects: tuple = ()               # declarations, not verified effects


@dataclass
class Cycle:
    """A parsed cycle: its id, its steps, and its metadata.

    ``variables`` are the defaults a run starts with; a run may override any of
    them, which is what makes one cycle definition reusable across branches,
    environments and hosts without being copied.
    """

    id: str
    name: str = ""
    description: str = ""
    #: Which project this belongs to, by name. The Cycles section keeps its own
    #: list of projects - deliberately not the ones on the Services & Logs page,
    #: because a project can have cycles and no services at all, and one list
    #: doing both jobs would make that look like a mistake. A name nothing
    #: recognises is not an error: the cycle simply shows as unassigned.
    project: str = ""
    version: int = 1
    variables: dict = field(default_factory=dict)
    #: Names of variables declared ``{secret: true}``. Their values are NOT in
    #: ``variables`` and never in the cycle file - see ``cycle/secrets.py``.
    #: A cycle with secrets can be committed and handed to somebody else; they
    #: supply their own values.
    secrets: tuple = ()
    #: Names declared ``{kind: path}``. Their values *are* in ``variables``
    #: like any other - a path is ordinary text at run time, and
    #: ``${vars.project_dir}`` reads the same whatever it was declared as.
    #: The declaration is for whoever is *editing* the cycle: a form can offer
    #: to find a folder rather than leaving somebody to type one correctly.
    paths: tuple = ()
    #: What the cycle works on, or None for a cycle whose every run stands on
    #: its own.
    subject: Subject = None
    #: What may offer to start the cycle without somebody pressing Run.
    triggers: tuple = ()                            # tuple[Trigger]
    # Named, bounded selections. They run fresh and never borrow other nodes.
    actions: dict = field(default_factory=dict)
    phases: list = field(default_factory=list)
    steps: list = field(default_factory=list)       # list[CycleStep]
    source: str = None                              # file path it was read from
    execution: dict = field(default_factory=dict)  # explicit launch defaults only
    #: The folder the cycle works on - a path, or ``${vars.<path variable>}``.
    #: Restrictions are set on it; without one the cycle runs as it always has.
    project_folder: str = ""
    #: Network, files and limits for the steps that run on the project folder.
    restrictions: dict = field(default_factory=dict)

    def step(self, step_id):
        """The step with this id, or None. Never raises - callers are checking."""
        for step in self.steps:
            if step.id == step_id:
                return step
        return None

    @property
    def title(self):
        return self.name or self.id


@dataclass
class Artifact:
    """A file a step produced, attributed to whoever produced it.

    ``path`` is relative to the run's workspace, not absolute: a run directory
    has to stay meaningful after it is moved, zipped, or attached to a bug
    report on another machine.
    """

    type: str                          # log | json | html | screenshot | junit | ...
    path: str                          # relative to the run workspace
    producer: str                      # step id
    name: str = ""                     # what to call it; falls back to the basename
    bytes: int = 0


@dataclass
class StepRun:
    """What happened to one step in one run.

    ``outputs`` is the structured half of a result and the reason steps do not
    need to know about each other: a later step reads
    ``${steps.<id>.outputs.<key>}`` rather than knowing what this plugin does.
    """

    step_id: str
    plugin: str = ""
    status: str = PENDING
    attempts: int = 0
    started_at: float = None           # epoch seconds
    ended_at: float = None
    duration_ms: float = 0.0
    message: str = ""
    outputs: dict = field(default_factory=dict)
    artifacts: list = field(default_factory=list)   # list[Artifact]
    metrics: dict = field(default_factory=dict)
    artifact_digests: dict = field(default_factory=dict)
    input_digest: str = ""
    definition_digest: str = ""
    source_run: str = ""
    # Presentation evidence; these fields do not change scheduling or inputs.
    reason_code: str = ""
    blocked_by: str = ""
    group: str = ""
    phase: str = ""

    # None identifies legacy records with no supervised execution receipts.
    active_elapsed_ms: float = None
    execution_generation: int = 1
    execution_segments: list = field(default_factory=list)

    last_activity_at: float = None
    progress: dict = field(default_factory=dict)
    timeout_remaining_ms: float = None
    # Written before a local task-budget mutation. Legacy in-flight writes
    # without this idempotence evidence retain their uncertain-outcome policy.
    admission_reservation: str = ""
    runtime: dict = field(default_factory=dict)  # core-owned immutable policy / lifecycle evidence

    @property
    def ok(self):
        return self.status == SUCCESS

    @property
    def done(self):
        return self.status in TERMINAL


@dataclass
class CycleRun:
    """One execution of a cycle: what ran, when, how it went, and where it went.

    This is the identity ``domain/result.py`` never had. A ``RunResult`` is a
    list of flow results and nothing else - no id, no clock, no trigger - which
    is why the GUI had to reconstruct a run's account of itself from the event
    stream. A cycle run carries its own, so the record on disk is the record,
    and reading it needs nothing that produced it.
    """

    id: str                            # 20260916-193412-full_validation
    cycle_id: str
    cycle_name: str = ""
    trigger: str = MANUAL
    status: str = PENDING
    started_at: float = None
    ended_at: float = None
    duration_ms: float = 0.0
    workspace: str = ""                # absolute; the run directory
    variables: dict = field(default_factory=dict)
    steps: dict = field(default_factory=dict)       # step id -> StepRun
    message: str = ""
    resume_count: int = 0
    revisions: list = field(default_factory=list)
    revision_inputs: dict = field(default_factory=dict)
    #: What this run turned out to be about - ``{kind, key, title, memory,
    #: pin, step}`` once the cycle's ``subject`` resolved, empty until then and
    #: for a cycle that declares none. ``step`` is the one that settled it.
    subject: dict = field(default_factory=dict)
    # Orthogonal to the schema-1 technical status. None means a legacy record
    # whose selection must be inferred, rather than an explicitly empty one.
    scope: str = ""
    selected_steps: list = None
    execution_status: str = ""
    coverage: dict = field(default_factory=dict)
    outcome: str = ""
    current_phase: str = ""
    action_id: str = ""
    action_definition: dict = field(default_factory=dict)
    task_brief: dict = field(default_factory=dict)
    plugin_names: dict = field(default_factory=dict)
    active_elapsed_ms: float = None
    execution_segments: list = field(default_factory=list)
    phase_definition: list = field(default_factory=list)
    phase_receipts: dict = field(default_factory=dict)
    last_completed_phase: str = ""
    pause_reason: str = ""
    pause_requested: bool = False
    paused: bool = False
    next_action: str = ""
    control_history: list = field(default_factory=list)
    phase_actions: dict = field(default_factory=dict)
    phase_retry_count: int = 0
    # A live Run/Retry phase is bounded even when the process must Resume.
    # Cleared only when its boundary is durably reached or a person continues.
    execution_bound: dict = field(default_factory=dict)
    initial_execution_mode: str = ""  # immutable launch snapshot; blank on legacy runs
    execution_mode: str = ""
    execution_policy_version: int = 1
    node_approvals: dict = field(default_factory=dict)
    policy_changes: list = field(default_factory=list)

    @property
    def ok(self):
        return self.status == SUCCESS

    @property
    def exit_code(self):
        return 0 if self.ok or (self.status == WAITING and self.paused) else 1

    def tally(self):
        """How many steps ended each way, as ``{status: count}``.

        Every status in :data:`TERMINAL` is present even at zero, so a caller
        rendering a summary does not have to guard each lookup, and a run with
        nothing skipped still says "skipped 0" rather than going quiet about it.
        """
        counts = {status: 0 for status in TERMINAL}
        for step in self.steps.values():
            if step.status in counts:
                counts[step.status] += 1
        return counts
