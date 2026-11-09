"""engine.stepping: pausing a scenario and moving over the steps it has taken.

The person is a script: each time the paused gate waits, the next thing they
do happens - a command from the GUI, or one from the HUD in the page.
"""

import os

import pytest

from engine import history, stepping
from engine.overlay import NullOverlay


PAGE = ("MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary=\"b\"\r\n\r\n"
        "--b\r\nContent-Type: text/html\r\nContent-Location: http://x.invalid/\r\n\r\n"
        "<html><body>a step</body></html>\r\n--b--\r\n")


class Person:
    """An adapter whose idle() is where the person acts."""

    def __init__(self, *script):
        self.script = list(script)
        self.gate = None
        self.hud = []
        self.locks = []
        self.waits = 0

    def idle(self, seconds):
        self.waits += 1
        if not self.script:
            raise AssertionError("paused with nothing left for the person to do")
        what, command = self.script.pop(0)
        if what == "gui":
            assert stepping.deliver(dict(command, session="dev-agent")) == ""
        elif what == "hud":
            self.hud.append(command)
        elif what == "stop":
            self.gate._stopping = lambda: True

    def overlay_commands(self):
        taken, self.hud = self.hud, []
        return taken

    def lock_input(self, on):
        self.locks.append(on)
        return True

    def page_copy(self, path):
        with open(path, "w", newline="") as handle:
            handle.write(PAGE)
        return True


class Told(NullOverlay):
    def __init__(self):
        self.said = []
        self.setups = []

    def step_paused(self, cursor, last, reason, view=None):
        self.said.append(("paused", cursor, last, reason, view))

    def step_resumed(self, cursor):
        self.said.append(("resumed", cursor))

    def step_setup(self, scenario, live, breakpoints, keys):
        self.setups.append((scenario, live, breakpoints, keys))


def gui(command, **fields):
    return ("gui", dict(fields, command="scenario." + command))


def hud(command, **fields):
    return ("hud", dict(fields, command=command))


@pytest.fixture
def make(tmp_path):
    gates = []

    def build(*script, live=True, failures=False, copies=False, locked=False):
        person = Person(*script)
        told = Told()
        options = stepping.Options(
            history=history.RunHistory(base=str(tmp_path)) if copies else None,
            pause_on_failure=failures)
        gate = stepping.StepGate("dev-agent", person, told, options=options, live=live,
                                 locked=locked)
        person.gate = gate
        gate.__enter__()
        gate.start("smoke")
        gates.append(gate)
        return gate, person, told

    yield build
    for gate in gates:
        gate.__exit__(None, None, None)


def walk(gate, upto):
    """Take steps 0 .. upto - 1 without stopping."""
    for index in range(upto):
        assert gate.before_step(index) == index
        gate.after_step(index)


def test_without_anybody_to_ask_nothing_ever_pauses(make):
    gate, person, _told = make(live=False)
    stepping.deliver({"command": "scenario.pause", "session": "dev-agent"})
    walk(gate, 3)
    assert gate.after_failure(2) is None
    assert person.waits == 0


def test_pause_lands_between_steps_and_continue_goes_on(make):
    gate, person, told = make(gui("continue"))
    walk(gate, 2)
    gate.put({"command": "pause"})
    assert gate.before_step(2) == 2
    assert told.said == [("paused", 2, 2, "pause", None), ("resumed", 2)]


def test_step_takes_one_step_and_pauses_again(make):
    gate, _person, told = make(gui("step"), gui("continue"))
    gate.put({"command": "pause"})
    assert gate.before_step(0) == 0
    gate.after_step(0)
    assert gate.before_step(1) == 1
    assert [one[3] for one in told.said if one[0] == "paused"] == ["pause", "step"]


def test_back_shows_an_earlier_page_and_going_on_runs_the_next_step_again(make):
    gate, _person, told = make(gui("back"), gui("back"), gui("continue"))
    walk(gate, 4)
    gate.put({"command": "pause"})
    # Paused after step 4; back twice is the page after step 2, and going on
    # from there is step 3 again (index 2), on the page as it is.
    assert gate.before_step(4) == 2
    assert [one[1] for one in told.said if one[0] == "paused"] == [4, 3, 2]
    assert gate.last == 2 and gate.cursor == 2


def test_back_stops_at_the_page_before_the_first_step(make):
    gate, _person, _told = make(gui("back"), gui("back"), gui("back"), gui("continue"))
    walk(gate, 1)
    gate.put({"command": "pause"})
    assert gate.before_step(1) == 0


def test_forward_moves_back_towards_the_live_page_and_then_steps(make):
    gate, _person, told = make(gui("back"), gui("forward"), gui("forward"), gui("continue"))
    walk(gate, 2)
    gate.put({"command": "pause"})
    assert gate.before_step(2) == 2           # forward on the live page = step
    gate.after_step(2)
    assert gate.before_step(3) == 3           # ...which pauses again, then continue
    assert [one[1] for one in told.said if one[0] == "paused"] == [2, 1, 2, 3]


def test_the_cursor_goes_where_it_is_put_within_the_steps_taken(make):
    gate, _person, _told = make(gui("cursor", index=99), gui("cursor", index=1),
                                gui("continue"))
    walk(gate, 3)
    gate.put({"command": "pause"})
    assert gate.before_step(3) == 1


def test_back_while_running_pauses_first_and_then_goes_back(make):
    gate, _person, told = make(gui("continue"))
    walk(gate, 3)
    gate.put({"command": "back"})
    assert gate.before_step(3) == 2
    assert told.said[0] == ("paused", 2, 3, "pause", None)


def test_a_breakpoint_pauses_before_its_step_only(make):
    gate, person, told = make(gui("continue"))
    gate.put({"command": "breakpoints", "scenario": "smoke", "indices": [2, "x"]})
    walk(gate, 2)
    assert gate.before_step(2) == 2
    gate.after_step(2)
    assert gate.before_step(3) == 3
    assert [one[3] for one in told.said if one[0] == "paused"] == ["breakpoint"]
    assert person.waits == 1


def test_breakpoints_belong_to_their_scenario(make):
    gate, _person, _told = make()
    gate.put({"command": "breakpoints", "scenario": "other", "indices": [0]})
    assert gate.before_step(0) == 0


def test_the_hud_is_heard_as_well_as_the_gui(make):
    gate, _person, _told = make(hud("back"), hud("continue"))
    walk(gate, 2)
    gate.put({"command": "pause"})
    assert gate.before_step(2) == 1


def test_stop_while_paused_stops(make):
    gate, _person, _told = make(("stop", None))
    gate.put({"command": "pause"})
    assert gate.before_step(0) is None


def test_a_locked_window_is_let_go_while_paused(make):
    gate, person, _told = make(gui("continue"), locked=True)
    gate.put({"command": "pause"})
    gate.before_step(0)
    assert person.locks == [False, True]


def test_a_failure_does_not_pause_unless_asked(make):
    gate, person, _told = make(failures=False)
    walk(gate, 1)
    assert gate.after_failure(0) is None and person.waits == 0


def test_going_on_from_a_failed_step_ends_the_scenario(make):
    gate, _person, told = make(gui("continue"), failures=True)
    walk(gate, 2)                      # step 2 (index 1) ran, and failed
    assert gate.after_failure(1) is None
    assert told.said[0] == ("paused", 2, 2, "failure", None)


def test_stepping_back_over_a_failure_runs_it_again(make):
    gate, _person, _told = make(gui("back"), gui("continue"), failures=True)
    walk(gate, 2)
    assert gate.after_failure(1) == 1          # the failed step itself, again


def test_a_paused_page_shows_the_copy_of_the_step_looked_at(make):
    gate, _person, told = make(gui("back"), gui("forward"), gui("continue"), copies=True)
    walk(gate, 2)
    gate.put({"command": "pause"})
    gate.before_step(2)
    views = [one[4] for one in told.said if one[0] == "paused"]
    # Live, then the copy of the page after step 1, then live again.
    assert views[0] is None and views[2] is None
    assert views[1] == gate._copies.path(1, "html") and os.path.exists(views[1])


def test_going_on_from_an_earlier_step_drops_the_copies_after_it(make):
    gate, _person, _told = make(gui("back"), gui("back"), gui("continue"), copies=True)
    walk(gate, 3)
    gate.put({"command": "pause"})
    assert gate.before_step(3) == 1
    assert [gate._copies.has(p) for p in range(4)] == [True, True, False, False]


def test_a_finished_scenario_takes_its_copies_with_it(make):
    gate, _person, _told = make(copies=True)
    walk(gate, 1)
    folder = gate._copies.dir
    gate.finish()
    assert not os.path.exists(folder)


def test_a_command_for_a_window_with_nothing_running_is_refused():
    assert "no scenario" in stepping.deliver({"command": "scenario.pause",
                                              "session": "nobody"})
    assert "unknown" in stepping.deliver({"command": "scenario.rewind",
                                          "session": "nobody"})


def test_a_scenario_says_what_stepping_can_do_and_again_when_that_changes(make):
    gate, _person, told = make(gui("continue"))
    assert told.setups[-1][:3] == ("smoke", True, [])
    gate.put({"command": "breakpoints", "indices": [3, 1]})
    gate.put({"command": "keys", "keys": {"back": "Ctrl+Left", "nonsense": "X"}})
    walk(gate, 1)
    assert told.setups[-2][2] == [1, 3]
    keys = told.setups[-1][3]
    assert keys["back"] == "Ctrl+Left" and keys["forward"] == "Alt+Right"
    assert "nonsense" not in keys
