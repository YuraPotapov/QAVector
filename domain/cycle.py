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

#: How a run was started. Only MANUAL exists today; the field is here because a
#: run's account of itself is worth nothing later if it cannot say what started
#: it, and adding the field afterwards means every old run answers "unknown".
MANUAL = "manual"


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

    @property
    def title(self):
        """What to call this step in a UI or a log line."""
        return self.label or self.id


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
    steps: list = field(default_factory=list)       # list[CycleStep]
    source: str = None                              # file path it was read from

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

    @property
    def ok(self):
        return self.status == SUCCESS

    @property
    def exit_code(self):
        return 0 if self.ok else 1

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
