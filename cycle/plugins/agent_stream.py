"""What an agent is doing, one stage at a time, read off its event stream.

An agent step used to show one thing: the JSON blob the run ended with, printed
into the log as a single unreadable line. That is the *result* of thirty seconds
of work and says nothing about the work - which file it opened, what it was
weighing, whether it is stuck. For a step that takes long enough to watch, that
is the wrong thing to show.

The Claude Code CLI already says all of it. ``--output-format stream-json``
prints one JSON object per line as the work happens, and every object is either
a stage worth showing or noise worth dropping::

    system/init          started, with this model
    assistant/thinking   what it is weighing
    assistant/tool_use   Read main.py
    user/tool_result     what came back
    assistant/text       what it concluded
    result               done, what it cost

This module is the translation and nothing else: a line of JSON in, a stage
dict out, or ``None`` for a line not worth a row. No process, no I/O, no Qt - so
the mapping is testable against recorded output, which is the only honest way to
check a format somebody else owns.

**Unknown is not an error.** The CLI is a separate program on its own release
schedule, and a type this does not know about is a line dropped, never a failed
step. The review the step actually produces comes from the ``result`` object,
not from here.
"""

import json
import os

from cycle.plugins.agent_worker import extract_json, validate_review

#: How much of a stage's detail is kept. These are read in a panel, not
#: archived - the whole of everything is in the step's stdout.log on disk.
DETAIL_CHARS = 2000

#: How much of a tool's answer is worth showing. Shorter than the rest: a file
#: read comes back whole, and the point of the row is that it came back.
RESULT_CHARS = 600

#: Types that carry no stage. ``thinking_tokens`` arrives many times a turn as a
#: running count, and rate limit notices are about the account rather than the
#: work.
SKIPPED_TYPES = ("rate_limit_event", "stream_event")
SKIPPED_SUBTYPES = ("thinking_tokens",)

#: Where to look, per tool, for the one value that says what it is doing. First
#: match wins, so a tool absent from here still gets a row - just a plainer one.
TELLING = ("file_path", "path", "pattern", "command", "query", "url",
           "notebook_path", "prompt", "description")


def stages(line):
    """Every stage one line of the stream carries. Usually none or one.

    A list rather than a single stage because one assistant message can hold
    several blocks - it may weigh something up and then call a tool in the same
    breath, and both are worth a row.
    """
    event = _parse(line)
    if not isinstance(event, dict):
        return []

    kind = event.get("type")
    if kind in SKIPPED_TYPES or event.get("subtype") in SKIPPED_SUBTYPES:
        return []
    if kind == "system":
        return _system(event)
    if kind == "result":
        return _result(event)
    if kind in ("assistant", "user"):
        return _message(event)
    return []


def _parse(line):
    try:
        return json.loads(line)
    except (TypeError, ValueError):
        return None                 # a partial line, or not JSON at all


def _system(event):
    if event.get("subtype") != "init":
        return []
    model = str(event.get("model") or "").strip()
    return [_stage("start", "Started" + (" - %s" % model if model else ""),
                   status="done")]


def _result(event):
    failed = bool(event.get("is_error"))
    bits = []
    turns = event.get("num_turns")
    if isinstance(turns, int):
        bits.append("%d turn%s" % (turns, "" if turns == 1 else "s"))
    cost = event.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        bits.append("$%.4f" % float(cost))
    title = "Failed" if failed else "Finished"
    return [_stage("result", title, detail=", ".join(bits),
                   status="failed" if failed else "done")]


def _message(event):
    """One assistant or user turn, as the blocks it is made of."""
    blocks = ((event.get("message") or {}).get("content")) or []
    if isinstance(blocks, str):                 # some turns carry plain text
        blocks = [{"type": "text", "text": blocks}]
    found = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        stage = _block(block)
        if stage is not None:
            found.append(stage)
    return found


def _block(block):
    kind = block.get("type")
    if kind == "thinking":
        text = _clean(block.get("thinking"))
        return _stage("thinking", "Thinking", detail=text) if text else None
    if kind == "text":
        text = _clean(block.get("text"))
        if not text:
            return None
        # The last thing a review says is the review, and it says it as JSON
        # because that is what the step asked for. Printed as it stands it is
        # one unreadable line; taken apart it is the whole point of the step.
        review = _as_review(block.get("text"))
        return _review(review) if review else _stage("text", "Answered",
                                                     detail=text)
    if kind == "tool_use":
        name = str(block.get("name") or "tool")
        arguments = block.get("input")
        title, used = _tool_title(name, arguments)
        return _stage("tool", title, detail=_arguments(arguments, used),
                      status="running")
    if kind == "tool_result":
        return _stage("tool_result", "Returned",
                      detail=_clean(_content(block.get("content")),
                                    RESULT_CHARS),
                      status="failed" if block.get("is_error") else "done")
    return None


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
    return _stage("review", title, detail=_clean(review.get("summary")),
                  status="done", body=review)


def _tool_title(name, arguments):
    """``Read main.py`` rather than ``Read`` - which file it opened is the point.

    Returns the title and the argument it fully accounts for, so the body can
    leave that one out. Only *fully*: a path shows as its basename, and the
    directory it was in is the half a reader still needs.
    """
    if not isinstance(arguments, dict):
        return name, ""
    for key in TELLING:
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip().splitlines()[0]
        if key.endswith("path"):
            text = os.path.basename(text.rstrip("/\\")) or text
        shown = _shorten(text, 70)
        return "%s %s" % (name, shown), key if shown == value else ""
    return name, ""


def _arguments(arguments, used=""):
    """The rest of what a tool was called with, as lines rather than as JSON.

    ``{\n  "file_path": "..."\n}`` is three lines of punctuation around one
    fact. The row is read, not parsed.
    """
    if not isinstance(arguments, dict) or not arguments:
        return ""
    lines = []
    for key in sorted(arguments):
        if key and key == used:
            continue                    # the title already says it, in full
        lines.append("%s: %s" % (key, _one_line(arguments[key])))
    return _clean("\n".join(lines))


def _one_line(value):
    if isinstance(value, str):
        text = value.strip()
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    return " ".join(text.split("\n"))


def _content(content):
    """A tool result's payload, which the CLI sends as text or as blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [one.get("text", "") for one in content
                 if isinstance(one, dict) and one.get("type") == "text"]
        return "\n".join(part for part in parts if part)
    return "" if content is None else str(content)


def _clean(text, limit=DETAIL_CHARS):
    text = ("" if text is None else str(text)).strip()
    return _shorten(text, limit)


def _shorten(text, limit):
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _stage(kind, title, detail="", status="done", body=None):
    """One row. ``body`` is whatever structure the row has beyond its text."""
    stage = {"kind": kind, "title": title, "detail": detail, "status": status}
    if body:
        stage["body"] = body
    return stage


#: The tools that change a file. A step that lets an agent edit needs to know
#: what it actually touched, and the stream already says so - every write is a
#: ``tool_use`` naming its path, so nothing has to diff a directory afterwards.
WRITING_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def written(line):
    """Every path one line of the stream says was written. Usually none."""
    event = _parse(line)
    if not isinstance(event, dict) or event.get("type") != "assistant":
        return []
    found = []
    for block in ((event.get("message") or {}).get("content")) or []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        if block.get("name") not in WRITING_TOOLS:
            continue
        arguments = block.get("input")
        if not isinstance(arguments, dict):
            continue
        for key in ("file_path", "notebook_path", "path"):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                found.append(value.strip())
                break
    return found


def last_result(text):
    """The final ``result`` object in a stream, as a dict.

    The stream ends with one, and it carries the review the step was actually
    asked for. Read by scanning from the end rather than parsing the whole file:
    every other line is a turn, and only the last one is the answer.
    """
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        event = _parse(line)
        if isinstance(event, dict) and event.get("type") == "result":
            return event
    return None
