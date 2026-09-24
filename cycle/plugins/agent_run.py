"""Running the Claude Code CLI on one prompt, and reading what it did.

Two plugins drive the same binary the same way - ``agent.edit`` makes one pass
over a directory, ``agent.implement`` makes several with checks between them -
and the machinery they share is not small: build the argv, stream the events,
turn each one into a row somebody reads, collect the paths that were written,
find the final result object at the end, and tell a usable reply apart from a
refusal. Written twice, the two copies would drift, and the half that drifted
would be the half that decides whether a step passed.

It is a separate module from ``agent_cli.py`` on purpose. That one answers
"where is the binary and is it signed in" and says in its own first line that
it never runs the agent - keeping the credential question apart from the
execution question is what lets the sign-in check be cheap enough for a form
to ask on demand.

**What comes back is a reply, not a verdict.** :func:`run_claude` reports what
the CLI did and whether its answer is usable; whether that makes the *step*
succeed is the caller's to decide, because the two plugins decide it
differently - one pass is done when the CLI is, several passes are done only
when the checks agree.
"""

import os

from cycle.operations import Operation, result_document, result_from_document

from cycle.plugins import agent_stream
from cycle.plugins._process import run_process

#: What a reading agent is allowed. A review reads; a step asked to review
#: something must not be able to change it.
READ_TOOLS = ("Read", "Glob", "Grep")

#: Read, and write. Deliberately without ``Bash``: a step that needs to run
#: something has ``command.shell``, which says so in the cycle file and is
#: covered by the same timeout and Stop. An editing agent with a shell makes
#: "what did this step do" unanswerable from the file.
EDIT_TOOLS = ("Read", "Glob", "Grep", "Edit", "Write", "MultiEdit")

#: How much of a stream's tail is read back to find the final result object.
#: A stream of turns runs to megabytes; the object that matters is its last
#: line.
RESULT_BYTES = 2 * 1024 * 1024

#: Edits are approved as they are made, because nobody is at a terminal during
#: a run and a step that waited to be asked would hang. Not
#: ``bypassPermissions``, which would also hand over everything else.
ACCEPT_EDITS = "acceptEdits"

#: How hard the CLI is asked to think, from ``--effort``. Written down here
#: rather than left to the binary because the CLI *warns and carries on* when
#: the level is not one it knows - so a typo in a cycle file would run at the
#: default, pass, and leave nobody able to tell from the report that the step
#: did not run at the level it says it did. A step is refused instead.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def min_effort_field():
    """The ``min_effort`` field, shared by every agent step that takes effort."""
    from cycle.registry import field
    return field("min_effort", "At least this effort", "choice", default="",
                 options=EFFORT_LEVELS,
                 hint="A floor under Effort. Useful when Effort comes from a "
                      "review's judgement: a task judged easy is still "
                      "reviewed at no less than this.")


def effort_for(plugin, step):
    """``(level to run at, level asked for)`` - the higher of Effort and its floor.

    Blank Effort with a floor runs at the floor: "at least medium" is not
    satisfied by a CLI default nobody can see. The pair is returned so a step
    can say when the floor raised it.
    """
    asked = str(plugin.setting(step.settings, "effort", "") or "").strip()
    floor = str(plugin.setting(step.settings, "min_effort", "") or "").strip()
    if not floor or (asked and EFFORT_LEVELS.index(asked) >= EFFORT_LEVELS.index(floor)):
        return asked, asked
    return floor, asked


def at_effort(effort):
    """How a step says the level it ran at, for the first line of its message.

    Said by the step because nothing else will: the CLI does not report the
    level back, and a level taken from ``${...}`` - a review's judgement of
    the task - is otherwise visible nowhere in the run.
    """
    return ("at %s effort" % effort) if effort else "at the CLI's default effort"


def with_effort(message, effort, asked=None):
    """``message`` with the effort added to its first line - and, when a floor
    raised it, what it was raised from."""
    first, newline, rest = str(message or "").partition("\n")
    said = at_effort(effort)
    if asked is not None and asked != effort:
        said += " (raised from %s)" % (asked or "the CLI's default")
    return "%s - %s%s%s" % (first, said, newline, rest)


class Reply(object):
    """One run of the CLI: what it wrote, what it said, and what it cost.

    ``problem`` is the one field worth reading first. It is empty when the
    answer is usable and carries a readable reason when it is not - the CLI
    exited badly, ended without a result, or refused. A caller that ignores it
    and reads ``text`` would be quoting a refusal as an answer.
    """

    def __init__(self, result, written, answer=None, problem=""):
        self.result = result            # the PluginResult from run_process
        self.written = written          # paths, once each, relative to `where`
        self.answer = answer or {}      # the CLI's final result object
        self.problem = problem

    @property
    def ok(self):
        return not self.problem

    @property
    def text(self):
        """What the agent said at the end, as prose."""
        return str(self.answer.get("result") or "").strip()

    @property
    def cost(self):
        """What the run cost, when the CLI says. ``None`` when it does not."""
        value = self.answer.get("total_cost_usd")
        return round(float(value), 4) if isinstance(value, (int, float)) else None


def run_claude(context, step, prompt, cwd, tools, binary, mode="", model="",
               max_turns=None, add_dir="", name="agent", effort=""):
    """Run the CLI on ``prompt`` in ``cwd``. Returns a :class:`Reply`.

    ``binary`` is the resolved path from ``agent_cli.auth_status`` - resolving
    and checking the sign-in stays with the caller, so a plugin can refuse
    early with its own wording rather than failing inside here.

    ``add_dir`` is a directory outside ``cwd`` the agent may also reach, and is
    omitted when empty: a review with no repository to read should not be
    handed one.

    ``effort`` is one of :data:`EFFORT_LEVELS`, and empty leaves the choice to
    the CLI's own configuration - which is what a cycle written before the
    field existed should keep doing.

    ``max_turns`` is passed only when a step set one. A count of turns is a
    poor measure of an agent being stuck - a change across six files takes
    more of them than a guess would allow - and an attempt cut off by it
    stops halfway with its edits applied and unchecked. The step's own
    ``timeout:`` is what bounds a run that goes on too long.
    """
    argv = [binary, "-p", prompt,
            # stream-json rather than json: the same answer arrives at the end,
            # but every turn is announced as it happens, so the step can show
            # what it is doing instead of going quiet and then printing a wall
            # of JSON. --verbose is what makes the CLI emit the turns at all.
            "--output-format", "stream-json", "--verbose",
            "--allowedTools", " ".join(tools)]
    if max_turns not in (None, ""):
        argv += ["--max-turns", str(int(max_turns))]
    if mode:
        argv += ["--permission-mode", mode]
    model = str(model or "").strip()
    if model:
        argv += ["--model", model]
    effort = str(effort or "").strip()
    if effort:
        argv += ["--effort", effort]
    if add_dir:
        argv += ["--add-dir", add_dir]

    operation = Operation(context, step, name, {"argv": argv, "cwd": cwd})
    cached = operation.cached()
    if cached is not None:
        return Reply(result_from_document(cached["result"]), cached["written"],
                     cached["answer"], cached["problem"])
    operation.begin()
    written = []
    result = run_process(operation.process_context(), step, argv, cwd, name,
                         env={"CLAUDE_CODE_SIMPLE": None},
                         on_stdout=_reader(context, step, written))
    # Collected whether or not the run went well. A pass that failed halfway
    # has still edited whatever it edited, and leaving that out of the record
    # would be the worst possible answer.
    # Against cwd, where the edits are made: add_dir may be a directory the
    # agent only reads, and a write there is exactly what should stand out.
    changed = distinct(written, cwd)
    answer = agent_stream.last_result(_read_stdout(result,
                                                   operation.directory))
    if not result.ok:
        reply = Reply(result, changed, answer, problem=result.message)
    elif answer is None:
        reply = Reply(result, changed, problem=(
            "Claude Code ended without a result. Its output is in %s"
            % result.outputs.get("stdout_path", "the step log")))
    elif answer.get("is_error"):
        reply = Reply(result, changed, answer, problem=(
            "Claude Code refused: %s" % str(answer.get("result")
                                            or answer.get("subtype") or "")[:200]))
    else:
        reply = Reply(result, changed, answer)
    # An exit code alone cannot say whether a remote request was billed. Only
    # a provider result (or failure before the process started) settles it.
    if answer is not None or result.outputs.get("started") is False:
        operation.complete({"result": result_document(result), "written": changed,
                            "answer": reply.answer, "problem": reply.problem},
                           bool(answer is not None and not answer.get("is_error")))
    return reply


def _reader(context, step, written):
    """One handler for both jobs the stream serves: the rows, and the paths."""
    def read(line):
        for one in agent_stream.stages(line):
            context.stage(step.id, one)
        written.extend(agent_stream.written(line))
    return read


def distinct(paths, where):
    """The files that were written, once each, relative to ``where``.

    Relative because that is what a later step can use and what a person reads;
    a path outside the directory is kept as it is rather than dropped, because
    a step that wrote somewhere unexpected is exactly what the record is for.
    """
    found = []
    for path in paths:
        try:
            shown = os.path.relpath(os.path.realpath(path), where)
        except ValueError:                      # different drive, on Windows
            shown = path
        if shown.startswith(".."):
            shown = path
        if shown not in found:
            found.append(shown)
    return found


def _read_stdout(result, directory):
    """The end of what the runner wrote for this step's stdout.

    ``run_process`` streams to a file and keeps only a short tail in memory, so
    the answer is read back off disk - and read from the **end**, because a
    stream of turns can run to megabytes while the object that matters is its
    last line. A first line cut in half by that is dropped by the reader rather
    than mistaken for anything, which is why reading the tail is safe here and
    reading the head would not be.
    """
    newest, when = "", -1.0
    for root, _dirs, names in os.walk(directory):
        for name in names:
            if name != "stdout.log":
                continue
            path = os.path.join(root, name)
            stamp = os.path.getmtime(path)
            if stamp > when:
                newest, when = path, stamp
    if not newest:
        return ""
    with open(newest, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        handle.seek(max(0, handle.tell() - RESULT_BYTES))
        raw = handle.read()
    return raw.decode("utf-8", "replace")
