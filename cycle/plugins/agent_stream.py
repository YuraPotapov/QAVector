"""What an agent is doing, one stage at a time, read off its event stream.

An agent step used to show one thing: the JSON blob the run ended with, printed
into the log as a single unreadable line. That is the *result* of thirty seconds
of work and says nothing about the work - which file it opened, what it was
weighing, whether it is stuck. For a step that takes long enough to watch, that
is the wrong thing to show.

**Reading the stream is the Agents integration's**
(``integrations/agents/adapters/claude.py``): which line is a stage, what a tool
call is titled, what was written and run. This module is the cycle's side of it
and adds the one thing only a cycle knows: an ``agent.review`` step asks for its
answer as a review document, so the text that *is* one is shown as a review
rather than as a line of JSON.
"""

from cycle.plugins.agent_worker import extract_json, validate_review
from integrations.agents.adapters import claude as _claude
from integrations.agents.adapters.claude import (                  # noqa: F401
    DETAIL_CHARS, RESULT_CHARS, SKIPPED_SUBTYPES, SKIPPED_TYPES, TELLING,
    WRITING_TOOLS, command_calls, commands, last_result, refused_calls, written)


def stages(line):
    """Every stage one line of the stream carries. Usually none or one."""
    return _claude.stages(line, on_text=_review_stage)


def _review_stage(text):
    """The stage for one piece of what the agent said, when it is a review.

    The last thing a review says is the review, and it says it as JSON because
    that is what the step asked for. Printed as it stands it is one unreadable
    line; taken apart it is the whole point of the step.
    """
    review = _as_review(text)
    return _review(review) if review else None


def _as_review(text):
    """``text`` as a validated review, or ``None`` when it is ordinary prose.

    Validated rather than merely parsed: an agent can perfectly well answer
    with some other JSON object, and showing that under the headings of a
    review would be a confident lie about what it said.
    """
    try:
        return validate_review(extract_json(text))
    except (ValueError, TypeError):
        return None


def _review(review):
    """The finished review as a stage the interface can lay out.

    The parsed fields travel in ``body``; ``detail`` carries the summary, so a
    reader with no special rendering still gets the sentence that matters
    rather than a line of punctuation.
    """
    issues = review.get("issues") or []
    risk = review.get("risk", "unknown")
    title = "Review - %s risk, %d issue%s" % (risk, len(issues),
                                              "" if len(issues) == 1 else "s")
    return _claude.stage("review", title, detail=_claude.clean(review.get("summary")),
                         status="done", body=review)
