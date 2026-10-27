"""Drive the Scenario Recorder: attach, show the panel, capture, write.

The recorder is the flow engine run backwards. Instead of reading a scenario and
performing it, it watches someone perform one and writes it down - and it writes
it in the same grammar, through the same validator, into the same tree, so what
comes out is an ordinary scenario with nothing special about it.

A window launched with --recorder is a window being recorded: the panel is there
from the moment the page is attached, with no extension, no menu item and nothing
to find. That is not the same as capturing automatically - nothing becomes a step
until Capture Step is pressed - it only means the recorder does not have to be
summoned.

Polling is not a compromise here. Playwright's sync API is greenlet-based,
so a callback can only run while this thread is inside a Playwright call - an idle
recorder has to pump regardless. Given that, asking the page for its queue costs
nothing extra and survives a navigation for free: a fresh document simply answers
with an empty one.
"""

import logging
import os
import threading
import time

from engine import events, flowfile, loader, runner

log = logging.getLogger("session_launcher.recorder")


class FlowNotWritable(Exception):
    """Asked to record into a scenario that ships with the application."""

_RECORDER_JS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "recorder.js")

#: How often to ask the page whether anything happened. Fast enough that picking
#: an action feels immediate, slow enough to be invisible on the machine.
POLL_SECONDS = 0.15

#: Actions the recorder performs as well as records, so the page ends up in the
#: state the replayed flow will reach. Assertions are deliberately absent: they
#: change nothing, and "performing" one would only mean waiting for what is
#: already on screen.
PERFORMED = ("click", "fill", "select", "press", "goto")

#: What a step carries through a recording, read from a scenario being continued
#: and written back at Finish. Everything here survives the round trip: a field
#: missing from this list was erased from every step of a continued scenario -
#: which is how its docs and retries used to disappear.
STEP_FIELDS = ("action", "target", "value", "timeout", "state", "retry", "doc")


def _load_recorder_js():
    with open(_RECORDER_JS_PATH, encoding="utf-8") as handle:
        return handle.read()


RECORDER_JS = _load_recorder_js()


class Recording:
    """One window being recorded: the steps so far, and what to do with them.

    Naming an existing scenario **continues** it. Its steps are loaded and what
    is captured is appended, and its name, description and tags are kept - a
    recording is usually one more pass over something half-written, not a fresh
    start, and replacing the file would throw away the work being added to.
    """

    def __init__(self, session_name, scenario_id=None, flows_dir=None):
        self.session = session_name
        self.flows_dir = flows_dir
        self.steps = []
        self.active = False
        self.finished = False
        self.continuing = False
        self.meta = None
        if scenario_id:
            self.scenario_id = flowfile.safe_id(scenario_id)
            self._load_existing()
        else:
            self.scenario_id = _default_scenario_id()

    def _load_existing(self):
        """Pick up where an existing scenario left off, if there is one."""
        try:
            existing = flowfile.describe_flow(self.scenario_id, self.flows_dir)
        except Exception:
            return                      # no such scenario: this one is new
        if not existing.get("writable"):
            # A bundled scenario cannot be written back, so recording into it
            # would collect steps that are thrown away at the end.
            raise FlowNotWritable(
                "%s ships with the application and cannot be recorded into; "
                "duplicate it on the Scenarios page first" % self.scenario_id)
        self.continuing = True
        self.meta = dict(existing.get("meta") or {})
        for step in existing.get("steps") or []:
            if step.get("action"):
                self.steps.append({key: step.get(key) for key in STEP_FIELDS
                                   if step.get(key) is not None})

    def state(self):
        """What the in-page panel paints itself from."""
        return {"scenario": self.scenario_id, "status": "recording",
                "steps": [{"action": s["action"],
                           "target": s.get("target") or "",
                           "value": s.get("value") or "",
                           "doc": s.get("doc") or ""} for s in self.steps]}

    def add(self, step):
        self.steps.append(step)

    def save(self):
        """Write what was captured as a scenario. Returns the core's result dict.

        A new one is tagged ``template``, because a recording is a draft and
        should not join ``--run-tests=all`` until someone has looked at it. One
        being continued keeps whatever it already said about itself - its name,
        its description and its tags are edits somebody made on purpose.
        """
        meta = dict(self.meta) if self.meta else {
            "name": "Recorded %s" % self.scenario_id,
            "description": "Captured with the Scenario Recorder.",
            "tags": list(flowfile.DEFAULT_TAGS),
        }
        meta["id"] = self.scenario_id
        return flowfile.save(
            self.scenario_id, self.flows_dir, meta=meta,
            steps=[{key: value for key, value in step.items() if key in STEP_FIELDS}
                   for step in self.steps])


def _default_scenario_id():
    return flowfile.safe_id("recorded_%s" % time.strftime("%Y%m%d_%H%M%S"))


def record_sessions(sessions, env=None, flows_dir=None, scenario_id=None,
                    stop_event=None):
    """Show the recorder in the launched window and keep it fed.

    Normally one window: the launcher refuses --recorder with more than one user
    selected, because a person can only be clicking in one of them and only the
    first would get the scenario id that was asked for. The loop still handles a
    list so the shape matches ``engine.runner``, and so a caller that builds its
    own session list is not silently mis-served.

    One thread per window, because Playwright's sync API is bound to the thread
    that created it - the same rule ``engine.runner`` follows for parallel runs.
    Returns when ``stop_event`` is set, which is what CTRL+C and the GUI's Stop
    both come down to.
    """
    if len(sessions) > 1:
        log.warning("Recording %d windows at once: only the first is given the "
                    "scenario id, the rest write timestamped files of their own.",
                    len(sessions))
    stop_event = stop_event or threading.Event()
    threads = []
    for index, session in enumerate(sessions):
        cls, _proc, profile, login, origin = session[:5]
        name = os.path.basename(profile) or login or ("session-%d" % index)
        thread = threading.Thread(
            target=_record_one, name="recorder-%d" % index, daemon=True,
            args=(name, profile, origin, flows_dir,
                  # Only the first window gets the requested id; the others would
                  # otherwise all write to the same file.
                  scenario_id if index == 0 else None, stop_event))
        thread.start()
        threads.append(thread)
    if not threads:
        log.error("No windows to record.")
        return 1
    log.info("Recorder ready in %d window(s). Press Capture Step (or F2) to "
             "record one.", len(threads))
    events.emit("recorder.ready", sessions=len(threads))
    try:
        while not stop_event.is_set() and any(t.is_alive() for t in threads):
            time.sleep(0.2)
    except KeyboardInterrupt:
        stop_event.set()
    for thread in threads:
        thread.join(timeout=5)
    return 0


def _record_one(session_name, profile, origin, flows_dir, scenario_id, stop_event):
    """Attach to one window and pump it until asked to stop."""
    try:
        endpoint = runner.wait_for_devtools(profile)
        # Lazy, as everywhere else: playwright is only needed once a window is
        # actually being driven.
        from adapters.playwright_adapter import PlaywrightAdapter
        adapter = PlaywrightAdapter.connect(endpoint, runner.DEFAULT_TIMEOUT_MS)
    except Exception as exc:
        log.error("[%s] could not attach: %s", session_name, exc)
        events.emit("recorder.attach_failed", session=session_name, error=str(exc))
        return
    events.emit("recorder.attached", session=session_name, endpoint=endpoint)

    selectors = loader.load_selectors(flows_dir)
    grammar = _grammar()
    try:
        recording = Recording(session_name, scenario_id, flows_dir)
    except FlowNotWritable as exc:
        log.error("[%s] %s", session_name, exc)
        events.emit("recorder.attach_failed", session=session_name, error=str(exc))
        adapter.disconnect()
        return
    adapter.recorder_setup(RECORDER_JS)
    _configure(adapter, selectors, grammar)

    try:
        while not stop_event.is_set():
            _tick(adapter, recording, selectors, grammar)
            time.sleep(POLL_SECONDS)
    finally:
        if recording.active and recording.steps and not recording.finished:
            # The window was closed mid-recording. Keep the work rather than
            # discarding it because nobody pressed Finish.
            _finish(adapter, recording)
        try:
            adapter.disconnect()
        except Exception:
            pass


def _tick(adapter, recording, selectors, grammar):
    """One poll: has anything happened, and does the page still have a recorder?"""
    answer = adapter.recorder_call(
        "{running: recorder.running(), events: recorder.drain()}")
    if not answer:
        return                      # navigating, or the page has no recorder yet

    # A recorder that is not running is either the first document or a new one:
    # the renderer survives a navigation, its state does not. Either way it is
    # set up and handed the steps so far, so the panel comes back complete.
    if not answer.get("running"):
        _begin(adapter, recording, selectors, grammar)
        return

    for event in answer.get("events") or []:
        _handle(adapter, recording, event)


def _begin(adapter, recording, selectors, grammar):
    """Put the panel in the page, with whatever has been captured so far.

    Runs on the first tick and again after every navigation; only the first one
    announces itself.
    """
    _configure(adapter, selectors, grammar)
    adapter.recorder_call("recorder.start()")
    adapter.recorder_call("recorder.render(arg)", recording.state())
    if recording.active:
        return                                  # a navigation, not a beginning
    recording.active = True
    if recording.continuing:
        log.info("[%s] continuing %s (%d steps so far)", recording.session,
                 recording.scenario_id, len(recording.steps))
    else:
        log.info("[%s] recording into %s", recording.session, recording.scenario_id)
    events.emit("recorder.started", session=recording.session,
                scenario=recording.scenario_id,
                continuing=recording.continuing, steps=len(recording.steps))


def _configure(adapter, selectors, grammar):
    adapter.recorder_call("recorder.configure(arg.selectors, arg.grammar)",
                          {"selectors": selectors, "grammar": grammar})


def _handle(adapter, recording, event):
    kind = (event or {}).get("kind")
    if kind == "step":
        _capture(adapter, recording, event.get("step") or {})
    elif kind == "finish":
        _finish(adapter, recording)
    elif kind in ("delete", "move", "edit", "doc"):
        _amend(adapter, recording, kind, event)


def _set_doc(step, text):
    """Give ``step`` the doc the panel sent - or take it away, when it is empty."""
    text = text.strip() if isinstance(text, str) else ""
    if text:
        step["doc"] = text
    else:
        step.pop("doc", None)


def _amend(adapter, recording, kind, event):
    """Delete, reorder, retarget or describe a step from the panel.

    Fixing a capture belongs in the recording, not only afterwards: while the
    page is still on screen it is obvious which step went wrong and what it
    should have pointed at. Python owns the list, so the panel sends an intent
    and gets the new state back - it never edits its own copy, which is what
    keeps the two from disagreeing after a navigation.
    """
    index = event.get("index")
    if not isinstance(index, int) or not 0 <= index < len(recording.steps):
        return                                  # a stale click; the list moved
    if kind == "delete":
        removed = recording.steps.pop(index)
        log.info("[%s] removed step %d (%s)", recording.session, index + 1,
                 removed.get("action"))
        events.emit("recorder.step_removed", session=recording.session,
                    index=index + 1, action=removed.get("action"))
    elif kind == "move":
        target = index + (1 if event.get("delta", 0) > 0 else -1)
        if not 0 <= target < len(recording.steps):
            return
        steps = recording.steps
        steps[index], steps[target] = steps[target], steps[index]
        events.emit("recorder.step_moved", session=recording.session,
                    index=index + 1, to=target + 1)
    elif kind == "doc":
        # The sentence the run's guide shows for this step (engine.livedoc).
        # Added, rewritten or deleted - an empty one is a delete - while the
        # page it describes is still on screen; the step itself stays.
        step = recording.steps[index]
        _set_doc(step, event.get("doc"))
        events.emit("recorder.step_documented", session=recording.session,
                    index=index + 1, action=step.get("action"), doc=step.get("doc"))
    else:                                        # edit
        step = recording.steps[index]
        target = (event.get("target") or "").strip()
        value = event.get("value")
        if target:
            step["target"] = target
        # An emptied value means "no value", which is not the same as untouched.
        step["value"] = value if (value or "").strip() else None
        if "doc" in event:
            _set_doc(step, event.get("doc"))
        events.emit("recorder.step_edited", session=recording.session,
                    index=index + 1, action=step.get("action"),
                    target=step.get("target"), value=step.get("value"),
                    doc=step.get("doc"))
    adapter.recorder_call("recorder.render(arg)", recording.state())


def _capture(adapter, recording, raw):
    """Perform what was picked, then write it down."""
    action = raw.get("action")
    if not action:
        return
    step = {"action": action, "target": raw.get("target") or None,
            "value": raw.get("value")}
    if action in PERFORMED:
        # Perform against the exact element that was picked, not the named
        # selector: a name from selectors.yaml may match several things, and the
        # user pointed at one of them.
        selector = raw.get("selector") or step["target"]
        try:
            _perform(adapter, action, selector, step.get("value"))
        except Exception as exc:
            # Record it anyway. The step is what the user asked for; that it did
            # not take effect right now is worth saying, not worth discarding.
            log.warning("[%s] %s did not take effect: %s",
                        recording.session, action, exc)
            events.emit("recorder.step_failed", session=recording.session,
                        action=action, target=step["target"], error=str(exc))
    recording.add(step)
    log.info("[%s] captured %s %s", recording.session, action, step["target"] or "")
    events.emit("recorder.step_captured", session=recording.session,
                scenario=recording.scenario_id, index=len(recording.steps),
                action=action, target=step["target"], value=step.get("value"))
    adapter.recorder_call("recorder.render(arg)", recording.state())


def _perform(adapter, action, selector, value):
    if action == "click":
        adapter.click(selector)
    elif action == "fill":
        adapter.fill(selector, value or "")
    elif action == "select":
        adapter.select(selector, value or "")
    elif action == "press":
        adapter.press_key(value or "")
    elif action == "goto":
        adapter.goto(selector)


def _finish(adapter, recording):
    """Stop capturing and write the scenario."""
    recording.finished = True
    recording.active = False
    result = recording.save()
    if result.get("ok"):
        log.info("[%s] wrote %s (%d steps)", recording.session,
                 result.get("path"), len(recording.steps))
    else:
        log.error("[%s] could not write %s: %s", recording.session,
                  recording.scenario_id, "; ".join(result.get("problems") or []))
    events.emit("recorder.finished", session=recording.session,
                scenario=recording.scenario_id, ok=bool(result.get("ok")),
                path=result.get("path"), steps=len(recording.steps),
                problems=list(result.get("problems") or []))
    adapter.recorder_call("recorder.stop()")


def _grammar():
    """The step vocabulary, handed to the page so its menu cannot drift."""
    from engine import compiler
    return {
        "selector_only": sorted(compiler.SELECTOR_ONLY),
        "selector_and_value": sorted(compiler.SELECTOR_AND_VALUE),
        "value_only": sorted(compiler.VALUE_ONLY),
        "url_target": sorted(compiler.URL_TARGET),
    }
