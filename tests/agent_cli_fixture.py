"""A stand-in for an agent CLI that takes turns, for tests of conversations.

It answers ``auth status --json`` as signed in, and otherwise behaves as the
Claude Code CLI does in ``-p --output-format stream-json`` mode: reads the
message from stdin, prints an ``init`` line carrying the session it was told to
use, and then what the message asks for by keyword::

    (anything)   thinks, answers "You said: ...", writes a file, finishes
    DENIED       the same, but the write is refused, as a Read turn's is
    REFUSE       finishes with an error result
    CRASH        writes to stderr and exits 3 with no result
    SILENT       exits 0 with no result
    HANG         thinks, then waits to be stopped
    FOLLOW       says it is working, reads what is said meanwhile, answers the
                 message and then that, and ends when its input does
    BACKGROUND HOLD  starts a server that never ends, answers, then answers
                 what is said while it waits, and waits to be stopped

Every start is written to the file ``FAKE_AGENT_LOG`` names - its argv, where
it ran, what it was told and its pid - so a test can say what the CLI was
actually started with. No model is asked anything.
"""

import json
import os
import stat
import sys

BODY = r'''
import json, os, sys, time
args = sys.argv[1:]
if args[:3] == ["auth", "status", "--json"]:
    print(json.dumps({"loggedIn": True, "email": "someone@example.invalid"}))
    sys.exit(0)


def value(flag):
    return args[args.index(flag) + 1] if flag in args else ""


def say(payload):
    print(json.dumps(payload), flush=True)


def line():
    # Byte by byte from the descriptor: nothing said after the first line is
    # held in a buffer where select() cannot see it.
    data = b""
    while not data.endswith(b"\n"):
        chunk = os.read(0, 1)
        if not chunk:
            break
        data += chunk
    return data.decode("utf-8")


session = value("--session-id") or value("--resume")
message = (json.loads(line())["message"]["content"]
           if "--input-format" in args else sys.stdin.read())
log = os.environ.get("FAKE_AGENT_LOG")
COST = 0.0125                               # what one turn costs
USAGE = {"input_tokens": 5, "cache_read_input_tokens": 1000,
         "cache_creation_input_tokens": 200, "output_tokens": 30}
taken = 1                                   # turns of this conversation, this one too
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "argv": args, "cwd": os.getcwd(), "message": message, "pid": os.getpid(),
            "simple": os.environ.get("CLAUDE_CODE_SIMPLE"),
            "sandboxed": os.environ.get("FAKE_SANDBOXED")}) + "\n")
    with open(log, encoding="utf-8") as handle:
        taken = sum(1 for line in handle if session and session in json.loads(line)["argv"])
say({"type": "system", "subtype": "init", "model": "fake-model", "session_id": session})
if message.startswith("/compact"):
    # As the real CLI answers it: a boundary with the sizes, then a result of nothing.
    say({"type": "system", "subtype": "status", "status": "compacting", "session_id": session})
    say({"type": "system", "subtype": "compact_boundary", "session_id": session,
         "compact_metadata": {"trigger": "manual", "pre_tokens": 1205, "post_tokens": 586}})
    if "CUT" in message:
        sys.exit(3)                         # gone between the boundary and the result
    say({"type": "result", "is_error": False, "result": "", "session_id": session, "num_turns": 0,
         "total_cost_usd": round(COST * taken, 6), "usage": {"input_tokens": 0, "output_tokens": 0}})
    sys.exit(0)
if message.startswith("FOLLOW") or message.startswith("BACKGROUND HOLD"):
    import select

    def heard():
        assert select.select([0], [], [], 5)[0]
        return json.loads(line())["message"]["content"]

    def result(text, count):
        say({"type": "result", "result": text, "session_id": session, "num_turns": 1,
             "total_cost_usd": COST * (taken + count), "usage": USAGE})

    if message.startswith("FOLLOW"):
        say({"type": "assistant", "message": {"content": [{"type": "text", "text": "Working on it"}]}})
        more = heard()                      # read while it works, answered after
        result("Done: FOLLOW", 0)
        say({"type": "assistant", "message": {"content": [{"type": "text", "text": "You also said: " + more}]}})
        result("Done: " + more, 1)
        # Its input closed after the first answer; what came before it was still answered.
        assert select.select([0], [], [], 5)[0] and os.read(0, 1) == b""
        sys.exit(0)
    say({"type": "system", "subtype": "background_tasks_changed",
         "tasks": [{"task_id": "server", "task_type": "local_bash"}]})
    result("Started the server", 0)
    result("Still serving: " + heard(), 1)
    time.sleep(30)
    sys.exit(0)
if message.startswith("BACKGROUND"):
    import select
    def background(ids):
        say({"type": "system", "subtype": "background_tasks_changed",
             "tasks": [{"task_id": key, "task_type": "local_bash"} for key in ids]})

    def result(text, count):
        say({"type": "result", "result": text, "session_id": session, "num_turns": 2,
             "total_cost_usd": COST * (taken + count), "usage": USAGE})

    background(["a", "b"])
    result("Started both tasks", 0)
    if "STOP" in message:
        time.sleep(30)
    if "LOST" in message:
        sys.exit(0)
    background(["b"])
    # The second task still needs its process. EOF here is premature.
    assert not select.select([sys.stdin], [], [], 0.1)[0]
    background([])
    assert select.select([sys.stdin], [], [], 3)[0] and sys.stdin.read() == ""
    status = "failed" if "FAIL" in message else "completed"
    say({"type": "system", "subtype": "task_notification", "task_id": "b",
         "status": status, "summary": "exit code 3" if status == "failed" else "exit code 0"})
    result("The task " + status, 1)
    sys.exit(0)
if "CRASH" in message:
    sys.stderr.write("boom: something broke\n")
    sys.exit(3)
if "SILENT" in message:
    sys.exit(0)
if "REFUSE" in message:
    say({"type": "result", "is_error": True, "result": "Not allowed here.",
         "session_id": session, "num_turns": 1})
    sys.exit(1)
say({"type": "assistant", "message": {"content": [
    {"type": "thinking", "thinking": "What was asked?"}]}})
if "HANG" in message:
    time.sleep(600)
if "--json-schema" in args:
    # A planning turn, as Claude Code 2.1.291 answers one: read-only looking,
    # then the answer through its own StructuredOutput tool and again as the
    # result's structured_output. Nothing is written.
    if "ASK" in message:
        answer = {"kind": "questions", "questions": [
            {"question": "Raise or return None?", "options": ["Raise", "Return None"],
             "multiple": False},
            {"question": "Which files?", "options": ["calc.py", "all"], "multiple": True}]}
    else:
        answer = {"kind": "plan", "plan": "# Plan\n\n1. Guard divide().\n2. Test it."}
    say({"type": "assistant", "message": {"usage": USAGE, "content": [
        {"type": "tool_use", "id": "toolu_so", "name": "StructuredOutput", "input": answer}]}})
    say({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_so",
         "content": "Structured output provided successfully"}]}})
    say({"type": "result", "is_error": False, "result": json.dumps(answer),
         "structured_output": answer, "session_id": session, "num_turns": 2,
         "total_cost_usd": round(COST * taken, 6), "usage": USAGE})
    sys.exit(0)
say({"type": "assistant", "message": {"usage": USAGE, "content": [
    {"type": "text", "text": "You said: " + message.strip()},
    {"type": "tool_use", "id": "tool-1", "name": "Write",
     "input": {"file_path": os.path.join(os.getcwd(), "notes.md"), "content": "x"}}]}})
if "DENIED" in message:
    # What the real CLI answers when the turn's tools do not include Write:
    # the refusal on a line of its own, then the call's result as an error.
    say({"type": "system", "subtype": "permission_denied", "tool_name": "Write",
         "tool_use_id": "tool-1", "message": "This command requires approval"})
    say({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tool-1", "is_error": True,
         "content": "Claude requested permissions to write, but you haven't granted it yet."}]}})
else:
    with open(os.path.join(os.getcwd(), "notes.md"), "a", encoding="utf-8") as handle:
        handle.write("x\n")
    say({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tool-1", "content": "File written"}]}})
# As the real CLI does: the cost and modelUsage of a conversation it continues
# are the whole conversation's so far; usage is this turn's alone.
say({"type": "result", "is_error": False, "result": "Done: " + message.strip(),
     "session_id": session, "num_turns": 2, "total_cost_usd": round(COST * taken, 6),
     "usage": dict(USAGE, output_tokens_details={"thinking_tokens": 7}),
     "modelUsage": {"fake-model": {"inputTokens": 5 * taken, "contextWindow": 200000}}})
'''


def fake_agent(directory, name="claude"):
    """Write the stand-in into ``directory``; returns its path."""
    path = os.path.join(str(directory), name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!%s\n%s" % (sys.executable, BODY))
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


def starts(log):
    """Every start the stand-in recorded, oldest first."""
    try:
        with open(str(log), encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except OSError:
        return []


def flag(start, name):
    """The value one start was given for ``name``, or None."""
    argv = start["argv"]
    return argv[argv.index(name) + 1] if name in argv else None
