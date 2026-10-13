"""Flow and Step models.

A Flow is a scenario or a reusable block - the two are structurally identical,
which is what lets one block (e.g. ``auth.login``) be referenced from many
scenarios. A Step is a single normalized action or assertion. The engine's
compiler turns the declarative YAML (which supports both a verbose ``type:``
form and a shorthand single-key form) into a flat list of these Steps.
"""

from dataclasses import dataclass, field


# The pseudo-action a step carries when it pulls in another flow's steps. The
# compiler expands these away, so no Step with action == USE ever reaches the
# runner.
USE = "use"


@dataclass
class Step:
    """A single normalized step in an execution plan.

    Which fields are meaningful depends on ``action``:
      - selector actions (click/wait_for/assert_visible/...): ``target`` is a
        CSS selector (already resolved from a named selector at compile time).
      - ``fill`` / ``assert_text_contains``: ``target`` is a selector and
        ``value`` is the text to type / expect.
      - ``goto``: ``target`` is the URL.
      - ``assert_url_contains`` / ``assert_title``: ``value`` is the expected
        substring.
      - ``use``: ``target`` is the referenced flow id (compile-time only).

    ``doc`` is the sentence a person reads in the guide a run writes
    (``--report-doc``, engine.livedoc): free text in whatever language the
    guide is for, never matched against the page.
    """

    action: str
    target: str = None
    value: str = None
    state: str = None          # wait_for only: "visible" | "attached" | ...
    timeout: float = None      # milliseconds; None -> engine default
    retry: dict = None         # {"attempts": int, "delay": float(seconds)}
    source: str = None         # flow id this step came from (diagnostics)
    doc: str = None            # what the step means to a reader of the guide


@dataclass
class Flow:
    """A parsed flow: its id, its raw steps, and metadata.

    ``steps`` holds the raw step mappings straight from YAML; the compiler
    normalizes and expands them. ``name`` / ``description`` / ``tags`` are
    optional metadata for reporting and future grouping.
    """

    id: str
    steps: list = field(default_factory=list)
    name: str = None
    description: str = None
    tags: list = field(default_factory=list)
    source: str = None         # file path the flow was loaded from
