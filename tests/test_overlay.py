"""Unit tests for the execution overlay observer (no browser needed).

Drives :class:`ExecutionOverlay` through a scripted run against a fake adapter
that just captures the state pushed to ``overlay_render``, and checks the tree
states, progress counters, log buffer, and completion banner.
"""

from domain.plan import PlanNode, GROUP, STEP
from domain.result import PASS, FAIL
from engine.overlay import ExecutionOverlay, NullOverlay, normalize_level


class FakeAdapter:
    """Captures every overlay call instead of talking to a real page."""

    def __init__(self, on_render=None):
        self.setups = []
        self.renders = []
        self.torn_down = False
        self._on_render = on_render

    def overlay_setup(self, js_source):
        self.setups.append(js_source)

    def overlay_render(self, state):
        self.renders.append(state)
        if self._on_render:
            self._on_render(state)

    def overlay_teardown(self):
        self.torn_down = True


def _tree():
    # Scenario / Login(group) / [host up, dashboard] + click apps (direct leaf)
    return PlanNode(id="0", label="Scenario", kind=GROUP, children=[
        PlanNode(id="0/0", label="Login", kind=GROUP, children=[
            PlanNode(id="0/0/0", label="assert_host_up", kind=STEP, step_index=0,
                     action="assert_host_up"),
            PlanNode(id="0/0/1", label="assert_visible dashboard", kind=STEP,
                     step_index=1, action="assert_visible"),
        ]),
        PlanNode(id="0/1", label="click apps_menu_toggle", kind=STEP, step_index=2,
                 action="click"),
    ])


def _overlay(components=("tree", "progress", "status", "logs"), on_render=None):
    adapter = FakeAdapter(on_render=on_render)
    return ExecutionOverlay(list(components), adapter), adapter


# --- lifecycle --------------------------------------------------------------
def test_flow_start_injects_once_and_seeds_state():
    ov, adapter = _overlay()
    ov.flow_start(_tree(), role="Admin")
    ov.flow_start(_tree(), role="Admin")     # a second scenario, same page
    assert len(adapter.setups) == 1          # renderer injected exactly once
    state = adapter.renders[-1]
    assert state["progress"] == {"done": 0, "total": 3}
    assert state["status"]["role"] == "Admin"
    assert state["status"]["flow"] == "Scenario"
    assert set(state["nodeStates"].values()) == {"pending"}
    assert state["treeVersion"] >= 2         # bumped per flow_start
    # The tree is now the whole SESSION: a root holding one group per scenario, so
    # a finished scenario stays on screen when the next starts.
    assert state["tree"]["id"] == "session"
    assert [c["label"] for c in state["tree"]["children"]] == ["Scenario", "Scenario"]
    assert state["activeNode"] == "s1"       # the second one is the live one


def test_component_list_is_forwarded():
    ov, adapter = _overlay(components=("tree", "logs"))
    ov.flow_start(_tree(), role="Admin")
    assert adapter.renders[-1]["components"] == ["tree", "logs"]


def test_a_named_scenario_fills_the_slot_planned_for_its_id():
    # session_start plans by id; the flow is called something else. Matched by
    # its name it missed the slot, got a second one appended, and the planned one
    # sat there saying "not started yet" beside its own result.
    ov, adapter = _overlay()
    ov.session_start(["claim75_dashboard_backend"])
    ov.flow_start(_tree(), role="Admin", scenario_id="claim75_dashboard_backend")
    state = adapter.renders[-1]
    assert [c["label"] for c in state["tree"]["children"]] == ["Scenario"]
    assert state["activeNode"] == "s0"


# --- step transitions -------------------------------------------------------
def test_step_start_and_end_update_states_and_progress():
    ov, adapter = _overlay()
    ov.flow_start(_tree(), role="Admin")

    ov.step_start(0)
    # Leaf ids carry their scenario prefix ("s0/..."), so two scenarios' states
    # cannot collide in one session tree.
    assert adapter.renders[-1]["nodeStates"]["s0/0/0/0"] == "running"
    assert adapter.renders[-1]["status"]["action"] == "assert_host_up"

    ov.step_end(0, PASS)
    end = adapter.renders[-1]
    assert end["nodeStates"]["s0/0/0/0"] == "success"
    assert end["progress"]["done"] == 1


def test_failure_marks_node_failed_and_sets_state():
    ov, adapter = _overlay()
    ov.flow_start(_tree(), role="Admin")
    ov.step_start(2)
    ov.step_end(2, FAIL, attempt=1, message="boom")
    state = adapter.renders[-1]
    assert state["nodeStates"]["s0/0/1"] == "failed"
    assert state["status"]["state"] == "Failed"
    assert state["progress"]["done"] == 0    # a failed step is not counted done


def test_full_pass_run_ends_with_success_banner():
    ov, adapter = _overlay()
    ov.flow_start(_tree(), role="Admin")
    for i in range(3):
        ov.step_start(i)
        ov.step_end(i, PASS)
    ov.flow_end(PASS, passed=3, total=3)
    state = adapter.renders[-1]
    assert state["banner"]["kind"] == "success"
    assert "3 / 3" in state["banner"]["sub"]
    assert state["progress"] == {"done": 3, "total": 3}


def test_failed_run_ends_with_failed_banner():
    ov, adapter = _overlay()
    ov.flow_start(_tree(), role="Admin")
    ov.step_start(0)
    ov.step_end(0, FAIL, message="nope")
    ov.flow_end(FAIL, passed=0, total=3)
    assert adapter.renders[-1]["banner"]["kind"] == "failed"


# --- logs -------------------------------------------------------------------
def test_retry_emits_warn_log():
    ov, adapter = _overlay()
    ov.flow_start(_tree(), role="Admin")
    ov.retry(0, attempt=2)                    # upcoming attempt #2 == retry #1
    logs = adapter.renders[-1]["logs"]
    assert logs[-1] == {"level": "WARN", "msg": "Retry #1"}


def test_log_buffer_is_bounded():
    ov, adapter = _overlay()
    ov.flow_start(_tree(), role="Admin")
    for i in range(100):
        ov.log("INFO", "line %d" % i)
    logs = adapter.renders[-1]["logs"]
    assert len(logs) <= 40
    assert logs[-1]["msg"] == "line 99"       # newest kept


def test_normalize_level_buckets():
    assert normalize_level("WARNING") == "WARN"
    assert normalize_level("critical") == "ERROR"
    assert normalize_level("DEBUG") == "INFO"


# --- robustness -------------------------------------------------------------
def test_push_is_reentrancy_safe():
    # If a render triggers a log (the real log-bridge can), the nested push must
    # not recurse forever - the entry is buffered and flushed on the next event.
    box = {}

    def on_render(_state):
        if not box.get("fired"):
            box["fired"] = True
            box["ov"].log("INFO", "from inside render")

    ov, adapter = _overlay(on_render=on_render)
    box["ov"] = ov
    ov.flow_start(_tree(), role="Admin")      # would RecursionError without the guard
    ov.step_start(0)                          # a fresh (non-reentrant) push flushes it
    assert any(entry["msg"] == "from inside render"
               for entry in adapter.renders[-1]["logs"])


def test_teardown_calls_adapter():
    ov, adapter = _overlay()
    ov.flow_start(_tree(), role="Admin")
    ov.teardown()
    assert adapter.torn_down is True


# --- NullOverlay ------------------------------------------------------------
def test_null_overlay_is_inert():
    ov = NullOverlay()
    assert ov.enabled is False
    # every hook is callable and does nothing
    ov.flow_start(_tree(), role="Admin")
    ov.step_start(0)
    ov.step_end(0, PASS)
    ov.retry(0, 2)
    ov.log("INFO", "x")
    ov.flow_end(PASS, 1, 1)
    ov.teardown()


# ------------------------------------------------------------- element marker

class _MarkAdapter:
    def __init__(self):
        self.marks = []
        self.setups = 0

    def overlay_setup(self, js):
        self.setups += 1

    def overlay_render(self, state):
        pass

    def overlay_mark(self, selector, label=None, timeout=None):
        self.marks.append((selector, label, timeout))


def test_mark_is_skipped_without_the_highlight_component():
    adapter = _MarkAdapter()
    ov = ExecutionOverlay(["tree", "progress"], adapter)
    ov.mark(".btn", "click")
    assert adapter.marks == []


def test_mark_forwards_when_highlight_is_requested():
    adapter = _MarkAdapter()
    ov = ExecutionOverlay(["tree", "highlight"], adapter)
    ov.mark(".btn", "click", timeout=2500)
    assert adapter.marks == [(".btn", "click", 2500)]


def test_mark_installs_the_hud_if_a_flow_has_not_started_yet():
    adapter = _MarkAdapter()
    ov = ExecutionOverlay(["highlight"], adapter)
    ov.mark(".btn", "click")
    assert adapter.setups == 1


def test_mark_passes_none_for_the_focused_element():
    # A bare `press` has no target: the marker must fall back to whatever has focus.
    adapter = _MarkAdapter()
    ov = ExecutionOverlay(["highlight"], adapter)
    ov.mark(None, "press Enter")
    assert adapter.marks == [(None, "press Enter", None)]


def test_null_overlay_mark_is_a_noop():
    NullOverlay().mark(".btn", "click")     # must not raise


# ------------------------------------------------- whole-session planned tree

def test_session_start_lists_every_planned_scenario():
    # The point of the session view: what already ran stays on screen when the
    # next scenario starts, instead of being replaced by it.
    ov, adapter = _overlay()
    ov.session_start(["access_manager", "multicompany_manager"])
    tree = adapter.renders[-1]["tree"]
    assert tree["id"] == "session"
    assert [c["label"] for c in tree["children"]] == ["access_manager",
                                                      "multicompany_manager"]
    # Not-yet-run scenarios still paint, via a placeholder leaf.
    assert tree["children"][0]["children"][0]["label"] == "not started yet"
    assert adapter.renders[-1]["activeNode"] is None      # nothing running yet


def test_session_start_injects_the_renderer_before_any_flow():
    # session_start is now the first hook of a session; without the injection the
    # planned list would not paint until the first scenario began.
    ov, adapter = _overlay()
    ov.session_start(["a"])
    assert len(adapter.setups) == 1


def test_flow_start_grafts_into_its_planned_slot():
    ov, adapter = _overlay()
    ov.session_start(["Scenario", "second"])
    ov.flow_start(_tree(), role="Admin")
    state = adapter.renders[-1]
    assert state["activeNode"] == "s0"
    kids = state["tree"]["children"]
    assert len(kids) == 2                       # the second is still listed
    assert kids[0]["children"][0]["id"].startswith("s0/")
    assert kids[1]["children"][0]["label"] == "not started yet"


def test_finished_scenario_keeps_its_states_when_the_next_starts():
    ov, adapter = _overlay()
    ov.session_start(["Scenario", "Scenario"])
    ov.flow_start(_tree(), role="Admin")
    ov.step_start(0)
    ov.step_end(0, PASS)
    ov.flow_start(_tree(), role="Admin")        # the next scenario begins
    states = adapter.renders[-1]["nodeStates"]
    assert states["s0/0/0/0"] == "success"      # the first one's result survives
    assert states["s1/0/0/0"] == "pending"
    assert adapter.renders[-1]["activeNode"] == "s1"


def test_scenario_ids_are_namespaced_so_states_cannot_collide():
    # Plan ids are path-based ("0", "0/1"), identical in every scenario, so without
    # a per-scenario prefix the second run would overwrite the first's states.
    ov, adapter = _overlay()
    ov.session_start(["Scenario", "Scenario"])
    ov.flow_start(_tree(), role="Admin")
    first = set(adapter.renders[-1]["nodeStates"])
    ov.flow_start(_tree(), role="Admin")
    second = set(adapter.renders[-1]["nodeStates"]) - first
    assert first and second and not (first & second)


def test_a_later_pass_does_not_erase_an_earlier_failure():
    """The banner speaks for the window, not for whichever flow ended last.

    A session runs several scenarios in one window. Reporting the last one as if
    it were the session put a green "Flow completed" above a tree with a red mark
    still in it.
    """
    ov, adapter = _overlay()
    ov.flow_start(_tree(), role="Agent")
    ov.step_start(0)
    ov.step_end(0, FAIL)
    ov.flow_end(FAIL, passed=0, total=3)

    ov.flow_start(_tree(), role="Agent")
    for i in range(3):
        ov.step_start(i)
        ov.step_end(i, PASS)
    ov.flow_end(PASS, passed=3, total=3)

    banner = adapter.renders[-1]["banner"]
    assert banner["kind"] == "failed"
    assert "1 of 2 scenarios failed" in banner["text"]
    assert "this one passed" in banner["sub"]


def test_every_scenario_passing_still_says_completed():
    ov, adapter = _overlay()
    for _ in range(2):
        ov.flow_start(_tree(), role="Agent")
        for i in range(3):
            ov.step_start(i)
            ov.step_end(i, PASS)
        ov.flow_end(PASS, passed=3, total=3)
    assert adapter.renders[-1]["banner"]["kind"] == "success"
