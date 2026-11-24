"""A turn read while it happens: events as they are printed, one answer at the end.

The core here is a script the test writes. It prints what the real one prints
for ``agents send`` - a line per event, the answer last - so what is tested is
the reading: that events arrive before the process ends, that a turn always
answers exactly once however it ends, and that Stop is taken as a request the
core answers rather than as a kill.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QEventLoop, QTimer

from cms_gui import core as core_mod
from cms_gui.agentturn import AgentTurn

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the stand-in core takes SIGTERM")

CORE = r'''
import json, signal, sys, time
argv = sys.argv[1:]
source = next((one[7:] for one in argv if one.startswith("--from=")), "")
document = json.load(open(source))
message = document.get("message", "")


def say(payload):
    print(json.dumps(payload), flush=True)


def stopped(*_):
    say({"event": {"type": "stopped", "seq": 3}})
    say({"ok": False, "kind": "stopped", "error": "The turn was stopped.",
         "turn": {"state": "stopped"}})
    sys.exit(1)


signal.signal(signal.SIGTERM, stopped)
say({"event": {"type": "user", "text": message, "seq": 1, "argv": argv, "source": source}})
if "DIE" in message:
    sys.stderr.write("Traceback: something broke\n")
    sys.exit(3)
if "REFUSED" in message:
    say({"ok": False, "kind": "not_installed", "error": "Claude Code is not installed."})
    sys.exit(1)
if "UNICODE" in message:
    raw = json.dumps({"event": {"type": "text", "text": "Grüße, café 👋"}}, ensure_ascii=False).encode()
    for byte in raw:
        sys.stdout.buffer.write(bytes([byte]))
        sys.stdout.buffer.flush()
        time.sleep(0.003)
    sys.stdout.buffer.write(b"\n")
    say({"ok": True})
    sys.exit(0)
if "FOLLOW" in message:
    # Heard on stdin, only when the document asks for it, as the core does.
    say({"event": {"type": "thinking", "text": "listening", "seq": 2,
                   "asked": document.get("follow_ups")}})
    said = json.loads(sys.stdin.readline())["say"]
    say({"event": {"type": "user", "text": said["message"], "follow_up": True, "id": said["id"], "seq": 3}})
    say({"ok": True, "turn": {"state": "completed", "n": 1}})
    sys.exit(0)
say({"event": {"type": "thinking", "text": "hm", "seq": 2}})
if "HANG" in message:
    time.sleep(60)
if "SLOW" in message:
    time.sleep(0.4)
sys.stdout.write("not json at all\n")
sys.stdout.write(json.dumps({"event": {"type": "text", "text": "split", "seq": 3}})[:20])
sys.stdout.flush()
time.sleep(0.05)
sys.stdout.write(json.dumps({"event": {"type": "text", "text": "split", "seq": 3}})[20:] + "\n")
say({"ok": True, "turn": {"state": "completed", "n": 1}, "session": {"id": "s"}})
'''


@pytest.fixture
def core(tmp_path):
    script = tmp_path / "core.py"
    script.write_text(CORE)
    return core_mod.Core(str(script), sys.executable, agents_path="/data/agents.json",
                         agent_sessions_path="/data/shop/agent-sessions")


def _run(qapp, turn, during=None, timeout_ms=15000):
    """Start ``turn`` and wait for its answer: ``(events, answers)``."""
    events, answers = [], []
    loop = QEventLoop()
    turn.event.connect(events.append)
    if during is not None:
        turn.event.connect(lambda event: during(event, events))
    turn.answered.connect(answers.append)
    turn.answered.connect(lambda _answer: loop.quit())
    QTimer.singleShot(timeout_ms, loop.quit)
    turn.start()
    if not answers:
        loop.exec()
    qapp.processEvents()
    return events, answers


def test_events_arrive_as_they_are_printed_and_the_answer_once_at_the_end(qapp, core):
    turn = AgentTurn(core, "20261111-172345-3fa91c", {"message": "hello", "mode": "read"})
    events, answers = _run(qapp, turn)
    assert [one["type"] for one in events] == ["user", "thinking", "text"]
    assert events[0]["text"] == "hello"
    assert answers == [{"ok": True, "turn": {"state": "completed", "n": 1},
                        "session": {"id": "s"}}]
    assert not turn.is_running()


def test_the_core_is_asked_for_this_session_with_the_message_in_a_private_file(qapp, core):
    turn = AgentTurn(core, "20261111-172345-3fa91c", {"message": "secret plans"})
    events, _answers = _run(qapp, turn)
    argv, source = events[0]["argv"], events[0]["source"]
    assert argv[:4] == ["--integrations", "agents", "send", "20261111-172345-3fa91c"]
    assert "--agents-file=/data/agents.json" in argv
    assert "--agent-sessions-dir=/data/shop/agent-sessions" in argv
    assert "secret plans" not in " ".join(argv)
    # Gone once the turn is over: what was said is in the session, not in /tmp.
    assert source and not os.path.exists(source)


def test_an_event_is_told_while_the_agent_is_still_working(qapp, core):
    running = []
    turn = AgentTurn(core, "s", {"message": "SLOW please"})
    _events, answers = _run(qapp, turn, during=lambda _event, _all: running.append(
        turn.is_running()))
    assert running[0] is True and answers[0]["ok"] is True


def test_a_line_that_arrives_in_two_pieces_is_one_event_and_noise_is_stepped_over(qapp, core):
    events, _answers = _run(qapp, AgentTurn(core, "s", {"message": "hello"}))
    assert [one["seq"] for one in events] == [1, 2, 3]
    assert events[2] == {"type": "text", "text": "split", "seq": 3}


def test_utf8_characters_split_across_reads_are_preserved(qapp, core):
    events, answers = _run(qapp, AgentTurn(core, "s", {"message": "UNICODE"}))
    assert events[-1] == {"type": "text", "text": "Grüße, café 👋"}
    assert answers == [{"ok": True}]


def test_stop_is_answered_by_the_core_as_a_stopped_turn(qapp, core):
    turn = AgentTurn(core, "s", {"message": "HANG please"})

    def during(event, _all):
        if event["type"] == "thinking":
            turn.stop()
            turn.stop()                 # pressed twice: still one stop

    events, answers = _run(qapp, turn, during=during)
    assert [one["type"] for one in events] == ["user", "thinking", "stopped"]
    assert len(answers) == 1
    assert (answers[0]["kind"], answers[0]["turn"]["state"]) == ("stopped", "stopped")
    assert not turn.is_running()


def test_a_refusal_is_the_answer_with_no_turn_in_it(qapp, core):
    _events, answers = _run(qapp, AgentTurn(core, "s", {"message": "REFUSED"}))
    assert answers == [{"ok": False, "kind": "not_installed",
                        "error": "Claude Code is not installed."}]


def test_a_core_that_dies_without_answering_is_still_an_answer(qapp, core):
    events, answers = _run(qapp, AgentTurn(core, "s", {"message": "DIE now"}))
    assert [one["type"] for one in events] == ["user"]
    assert len(answers) == 1 and answers[0]["ok"] is False
    assert "ended without an answer (exit 3)" in answers[0]["error"]
    assert "something broke" in answers[0]["error"]


def test_a_core_that_cannot_be_started_is_an_answer_too(qapp, tmp_path):
    missing = core_mod.Core(str(tmp_path / "no-such-core"), "")
    _events, answers = _run(qapp, AgentTurn(missing, "s", {"message": "hello"}))
    assert len(answers) == 1 and answers[0]["ok"] is False
    assert "Could not start" in answers[0]["error"]


def test_shutting_down_mid_turn_ends_the_process_before_the_object_goes(qapp, core):
    turn = AgentTurn(core, "s", {"message": "HANG please"})
    answers = []
    turn.answered.connect(answers.append)
    loop = QEventLoop()
    turn.event.connect(lambda event: loop.quit() if event["type"] == "thinking" else None)
    QTimer.singleShot(15000, loop.quit)
    turn.start()
    loop.exec()
    assert turn.is_running()
    turn.shutdown()
    qapp.processEvents()
    assert not turn.is_running()
    assert len(answers) == 1 and answers[0]["kind"] == "stopped"


def test_what_is_said_meanwhile_reaches_the_core_on_its_stdin(qapp, core):
    turn = AgentTurn(core, "20261111-172345-3fa91c", {"message": "FOLLOW"})

    def during(event, _events):
        if event["type"] == "thinking":
            assert turn.follow_up("f-1", "and the tests too")

    events, answers = _run(qapp, turn, during)
    assert events[1]["asked"] is True
    assert events[-1] == {"type": "user", "text": "and the tests too", "follow_up": True, "id": "f-1", "seq": 3}
    assert answers[0]["ok"] and not turn.follow_up("f-2", "too late")       # over: nothing more is heard
