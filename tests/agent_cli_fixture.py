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


session = value("--session-id") or value("--resume")
message = sys.stdin.read()
log = os.environ.get("FAKE_AGENT_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "argv": args, "cwd": os.getcwd(), "message": message, "pid": os.getpid(),
            "simple": os.environ.get("CLAUDE_CODE_SIMPLE"),
            "sandboxed": os.environ.get("FAKE_SANDBOXED")}) + "\n")
say({"type": "system", "subtype": "init", "model": "fake-model", "session_id": session})
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
say({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "You said: " + message.strip()},
    {"type": "tool_use", "id": "tool-1", "name": "Write",
     "input": {"file_path": os.path.join(os.getcwd(), "notes.md"), "content": "x"}}]}})
if "DENIED" in message:
    # What the real CLI answers when the turn's tools do not include Write.
    say({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tool-1", "is_error": True,
         "content": "Claude requested permissions to write, but you haven't granted it yet."}]}})
else:
    say({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tool-1", "content": "File written"}]}})
say({"type": "result", "is_error": False, "result": "Done: " + message.strip(),
     "session_id": session, "num_turns": 2, "total_cost_usd": 0.0125})
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
