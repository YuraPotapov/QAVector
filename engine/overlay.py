"""Execution overlay (HUD) - the runner's observer that drives the in-page HUD.

The runner notifies an overlay object at each execution transition (flow start,
step start/end, retry, log line, flow end). :class:`ExecutionOverlay` keeps the
live state in Python and, after every event, pushes the whole state into the
page where ``engine/hud.js`` paints it (an isolated, click-through Shadow DOM -
see that file). :class:`NullOverlay` is the no-op used when the overlay is
disabled, so the runner can call the same methods unconditionally.

Rendering happens over the existing browser adapter (``overlay_setup`` /
``overlay_render``); the overlay never touches Playwright directly, so it stays
unit-testable against a fake adapter.
"""

import os
import time
from collections import deque

from domain.result import PASS, FAIL, ERROR

# Every overlay component the launcher accepts. The JS renderer currently paints
# tree/progress/status/logs plus "highlight" (the per-step element marker);
# "notifications" is accepted (e.g. via --execution-overlay=all) but not drawn yet.
KNOWN_COMPONENTS = ("tree", "progress", "status", "logs", "highlight", "notifications")

_HUD_JS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hud.js")

# States the HUD understands (mirrors ICON/COLOR maps in hud.js).
_PENDING, _RUNNING, _SUCCESS, _FAILED = "pending", "running", "success", "failed"

_LOG_MAXLEN = 40   # live-log ring buffer size


def _load_hud_js():
    with open(_HUD_JS_PATH, encoding="utf-8") as fh:
        return fh.read()


HUD_JS = _load_hud_js()


def _now_ms():
    return int(time.time() * 1000)


def normalize_level(levelname):
    """Map a logging level name to the HUD's INFO/WARN/ERROR buckets."""
    name = (levelname or "INFO").upper()
    if name in ("WARNING", "WARN"):
        return "WARN"
    if name in ("ERROR", "CRITICAL", "FATAL"):
        return "ERROR"
    return "INFO"


class NullOverlay:
    """Disabled overlay: every hook is a no-op (the runner's default)."""

    enabled = False

    def session_start(self, scenario_ids):
        pass

    def flow_start(self, root, role=None, scenario_id=None):
        pass

    def step_start(self, index):
        pass

    def step_end(self, index, status, attempt=1, message=""):
        pass

    def mark(self, selector, label=None, timeout=None):
        pass

    def retry(self, index, attempt):
        pass

    def log(self, level, message):
        pass

    def flow_end(self, status, passed, total):
        pass

    def teardown(self):
        pass


class ExecutionOverlay:
    """Live overlay state, pushed into the page after every execution event."""

    enabled = True

    def __init__(self, components, adapter):
        self._components = list(components)
        self._adapter = adapter
        self._setup_done = False
        self._pushing = False       # reentrancy guard (log-bridge can re-enter)
        # per-flow state
        self._version = 0
        self._tree_dict = None
        self._leaves = {}           # step_index -> leaf PlanNode
        self._index_to_id = {}      # step_index -> leaf node id
        self._node_states = {}      # leaf node id -> state
        self._done = 0
        self._total = 0
        self._status = {"role": None, "flow": None, "action": "-",
                        "state": "-", "startedAt": None}
        self._logs = deque(maxlen=_LOG_MAXLEN)
        self._banner = None
        # Whole-session view: every scenario this window will run, so a finished
        # or failed one stays on screen instead of being replaced by the next.
        self._planned = []          # [{"id", "node", "tree"}] in run order
        self._active_node = None    # node id of the scenario currently running
        # Every finished flow's status, so the banner can speak for the session
        # rather than for whichever scenario happened to end last.
        self._results = []

    # -- execution hooks ---------------------------------------------------
    def _ensure_setup(self):
        """Inject the renderer once, on whichever hook fires first."""
        if not self._setup_done:
            self._adapter.overlay_setup(HUD_JS)
            self._setup_done = True

    def session_start(self, scenario_ids):
        """Announce every scenario this window will run, before the first starts.

        Without this the HUD only ever showed the scenario in flight: when one
        failed and the next began, what came before vanished. Each scenario gets a
        group node up front - pending ones carry a placeholder leaf so they still
        paint - and its real step tree is grafted in when it starts.
        """
        self._planned = [{"id": sid, "node": "s%d" % i, "tree": None}
                         for i, sid in enumerate(scenario_ids)]
        self._active_node = None
        self._node_states = {}
        self._rebuild_tree()
        self._ensure_setup()      # this is now the FIRST hook of a session
        self._push()

    def _scenario_node(self, entry):
        """One scenario's group node: its real tree once started, else a stub."""
        if entry["tree"] is not None:
            children = entry["tree"].get("children") or []
        else:
            children = [{"id": entry["node"] + "/_", "label": "not started yet",
                         "kind": "step"}]
        # Named by the flow once it has started - what the scenario calls itself -
        # and by its id until then, which is all session_start knows.
        return {"id": entry["node"], "label": entry.get("label") or entry["id"],
                "kind": "group", "children": children}

    def _rebuild_tree(self):
        """Compose the session tree; bump the version so the HUD re-renders it."""
        self._version += 1
        self._tree_dict = {
            "id": "session", "label": "Planned tests", "kind": "group",
            "children": [self._scenario_node(e) for e in self._planned],
        }

    @staticmethod
    def _namespace(node, prefix):
        """Re-id a plan subtree so ids stay unique across scenarios.

        PlanNode ids are path-based ("0", "0/1"), so every scenario would
        otherwise reuse the same ids and their states would bleed together.
        """
        out = dict(node)
        out["id"] = prefix + "/" + node["id"]
        if node.get("children"):
            out["children"] = [ExecutionOverlay._namespace(c, prefix)
                               for c in node["children"]]
        return out

    def flow_start(self, root, role=None, scenario_id=None):
        leaves = list(root.leaves())
        # Graft this scenario's real tree into its slot, keeping every other
        # scenario on screen. Ids are namespaced per scenario, so leaf states from
        # earlier ones survive untouched in _node_states.
        # By id: session_start planned the slots by id, and a scenario with a
        # name of its own used to miss its slot, get a new one appended, and leave
        # the planned one saying "not started yet" beside its own result.
        entry = self._entry_for(scenario_id or root.label)
        if entry is None:
            # Either session_start was never called (a bare flow, or a test driving
            # the overlay directly) or this scenario is running again - append a
            # slot so nothing already on screen is overwritten.
            entry = {"id": scenario_id or root.label,
                     "node": "s%d" % len(self._planned), "tree": None}
            self._planned.append(entry)
        prefix = entry["node"]
        entry["label"] = root.label
        entry["tree"] = self._namespace(root.to_dict(), prefix)
        self._active_node = prefix

        self._leaves = {leaf.step_index: leaf for leaf in leaves}
        self._index_to_id = {leaf.step_index: prefix + "/" + leaf.id for leaf in leaves}
        for node_id in self._index_to_id.values():
            self._node_states[node_id] = _PENDING
        self._node_states.pop(prefix + "/_", None)   # drop the "not started" stub
        self._done = 0
        self._total = len(leaves)
        self._banner = None
        self._status = {"role": role, "flow": root.label, "action": "-",
                        "state": "Starting", "startedAt": _now_ms()}
        self._rebuild_tree()
        self._ensure_setup()
        self._push()

    def _entry_for(self, scenario_id):
        """The planned, not-yet-run slot for a scenario id (None if there is none).

        Matching only unrun slots means a scenario listed twice fills each slot in
        turn, and a re-run of a finished one gets a fresh slot rather than wiping
        the result already on screen.
        """
        for entry in self._planned:
            if entry["id"] == scenario_id and entry["tree"] is None:
                return entry
        return None

    def mark(self, selector, label=None, timeout=None):
        """Flash a marker over the element a step is about to act on.

        Only when "highlight" was requested via --execution-overlay: it costs an
        extra round-trip per acting step, and it paints into the page, so it stays
        opt-in rather than riding along with the tree/progress widgets.

        ``selector`` None means the focused element - what a bare `press` acts on,
        and otherwise invisible in a screenshot.
        """
        if "highlight" not in self._components:
            return
        self._ensure_setup()
        self._adapter.overlay_mark(selector, label, timeout)

    def step_start(self, index):
        node_id = self._index_to_id.get(index)
        if node_id is not None:
            self._node_states[node_id] = _RUNNING
        leaf = self._leaves.get(index)
        if leaf is not None:
            self._status["action"] = leaf.label
        self._status["state"] = "Running"
        self._push()

    def step_end(self, index, status, attempt=1, message=""):
        node_id = self._index_to_id.get(index)
        if node_id is not None:
            if status == PASS:
                self._node_states[node_id] = _SUCCESS
                self._done += 1
            else:
                self._node_states[node_id] = _FAILED
        if status != PASS:
            self._status["state"] = "Failed"
        self._push()

    def retry(self, index, attempt):
        # attempt is the upcoming attempt number (2 = first retry).
        self.log("WARN", "Retry #%d" % (attempt - 1))

    def log(self, level, message):
        self._logs.append({"level": normalize_level(level), "msg": message})
        self._push()

    def flow_end(self, status, passed, total):
        self._results.append(status)
        failed = [s for s in self._results if s != PASS]
        if status != PASS:
            self._banner = {"kind": "failed", "text": "✖ Flow failed",
                            "sub": self._status.get("action") or ""}
            self._status["state"] = "Failed"
        elif failed:
            # This flow passed, but an earlier one in the same window did not.
            # Saying "✓ Flow completed" here reports the last scenario as if it
            # were the session, and the red mark in the tree above says otherwise.
            self._banner = {"kind": "failed",
                            "text": "✖ %d of %d scenarios failed"
                                    % (len(failed), len(self._results)),
                            "sub": "this one passed: %d / %d steps" % (passed, total)}
            self._status["state"] = "Failed earlier"
        else:
            self._banner = {"kind": "success", "text": "✓ Flow completed",
                            "sub": "%d / %d steps executed" % (passed, total)}
            self._status["state"] = "Completed"
        # The HUD stays on screen after the run, so freeze the elapsed clock here.
        self._status["stoppedAt"] = _now_ms()
        self._push()

    def teardown(self):
        try:
            self._adapter.overlay_teardown()
        except Exception:
            pass

    # -- rendering ---------------------------------------------------------
    def state(self):
        """The full state dict pushed to the renderer (also handy for tests)."""
        return {
            "components": list(self._components),
            "treeVersion": self._version,
            "tree": self._tree_dict,
            "nodeStates": dict(self._node_states),
            "progress": {"done": self._done, "total": self._total},
            "activeNode": self._active_node,
            "status": dict(self._status),
            "logs": list(self._logs),
            "banner": self._banner,
        }

    def _push(self):
        if self._pushing:   # a log emitted mid-push must not recurse
            return
        self._pushing = True
        try:
            self._adapter.overlay_render(self.state())
        finally:
            self._pushing = False
