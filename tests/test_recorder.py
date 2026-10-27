"""The recorder's state machine: what it does with what the page reports.

The page is faked. What is worth pinning here is the orchestration - when a
recording starts, which steps are performed as well as written down, what happens
when the window navigates mid-recording, and above all that whatever comes out is
an ordinary scenario the compiler accepts. A recorder that produces something the
runner cannot run is worse than no recorder.
"""

import json
import os
import subprocess

import pytest

from engine import compiler, flowfile, loader, recorder

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "flows")


class FakeAdapter:
    """A page that answers whatever the test queued, and remembers what was done."""

    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.calls = []          # every recorder_call expression, in order
        self.performed = []      # (action, selector, value)
        self.setups = []
        self.rendered = []
        self.disconnected = False

    # -- what the recorder uses ----------------------------------------------
    def recorder_setup(self, js_source):
        self.setups.append(js_source)

    def recorder_call(self, expression, argument=None):
        self.calls.append(expression)
        if expression.startswith("recorder.render"):
            self.rendered.append(argument)
            return None
        if expression.startswith("{running:"):
            return self.answers.pop(0) if self.answers else None
        return None

    def click(self, selector, timeout=None):
        self.performed.append(("click", selector, None))

    def fill(self, selector, value, timeout=None):
        self.performed.append(("fill", selector, value))

    def select(self, selector, value, timeout=None):
        self.performed.append(("select", selector, value))

    def press_key(self, key):
        self.performed.append(("press", None, key))

    def goto(self, url, timeout=None):
        self.performed.append(("goto", url, None))

    def disconnect(self):
        self.disconnected = True


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A writable flows tree in front of the fixtures, as an installed build has."""
    user = tmp_path / "user-flows"
    (user / "scenarios").mkdir(parents=True)
    monkeypatch.setattr(loader.runtime_paths, "flows_search_path",
                        lambda: [str(user), FIXTURES])
    return user


def _answer(running=True, request=False, events=()):
    return {"running": running, "request": request, "events": list(events)}


def _step_event(action, target, selector=None, value=None, named=False):
    return {"kind": "step", "step": {"action": action, "target": target,
                                     "selector": selector or target,
                                     "value": value, "named": named}}


def _recording(tree, scenario_id="recorded"):
    return recorder.Recording("session-1", scenario_id, None)


# ------------------------------------------------------------------- starting

def test_the_panel_appears_without_being_asked_for(tree):
    """A window launched with --recorder is a window being recorded."""
    adapter = FakeAdapter([_answer(running=False)])
    rec = _recording(tree)
    recorder._tick(adapter, rec, {"dashboard": ".navbar"}, {})
    assert rec.active
    assert any(c.startswith("recorder.start") for c in adapter.calls)
    # The page is told the vocabulary and the selectors, so its menu and its
    # naming cannot drift from what the compiler and the tree actually have.
    assert any(c.startswith("recorder.configure") for c in adapter.calls)


def test_showing_the_panel_is_not_capturing(tree):
    """The whole promise: ordinary browsing never becomes a step."""
    adapter = FakeAdapter([_answer(running=False), _answer(events=[])])
    rec = _recording(tree)
    recorder._tick(adapter, rec, {}, {})
    recorder._tick(adapter, rec, {}, {})
    assert rec.steps == []


def test_a_page_with_no_recorder_is_not_an_error(tree):
    # Mid-navigation the evaluate returns nothing at all; that is a normal tick.
    adapter = FakeAdapter([None])
    rec = _recording(tree)
    recorder._tick(adapter, rec, {}, {})
    assert not rec.active


# ------------------------------------------------------------------ capturing

def test_a_captured_step_is_performed_and_recorded(tree):
    rec = _recording(tree)
    rec.active = True
    adapter = FakeAdapter([_answer(events=[_step_event("click", "menu_settings",
                                                       selector=".o_menu")])])
    recorder._tick(adapter, rec, {}, {})
    # Performed against the element that was picked, not the name: a name from
    # selectors.yaml may match several things and the user pointed at one.
    assert adapter.performed == [("click", ".o_menu", None)]
    # Recorded under the name, so the scenario reads like the tree it joins.
    assert rec.steps == [{"action": "click", "target": "menu_settings", "value": None}]


def test_an_assertion_is_recorded_without_being_performed(tree):
    rec = _recording(tree)
    rec.active = True
    adapter = FakeAdapter([_answer(events=[_step_event("assert_visible", "dashboard")])])
    recorder._tick(adapter, rec, {}, {})
    assert adapter.performed == []          # nothing to do; it changes nothing
    assert rec.steps[0]["action"] == "assert_visible"


def test_a_value_step_carries_its_value(tree):
    rec = _recording(tree)
    rec.active = True
    adapter = FakeAdapter([_answer(events=[_step_event("fill", "login_input",
                                                       value="admin")])])
    recorder._tick(adapter, rec, {}, {})
    assert adapter.performed == [("fill", "login_input", "admin")]
    assert rec.steps[0]["value"] == "admin"


def test_a_step_that_will_not_take_effect_is_still_recorded(tree):
    """What the user asked for is the step; that it failed now is a warning."""
    rec = _recording(tree)
    rec.active = True
    adapter = FakeAdapter([_answer(events=[_step_event("click", "gone")])])

    def boom(selector, timeout=None):
        raise RuntimeError("element detached")

    adapter.click = boom
    recorder._tick(adapter, rec, {}, {})
    assert rec.steps[0]["action"] == "click"


def test_the_panel_is_repainted_after_every_capture(tree):
    rec = _recording(tree)
    rec.active = True
    adapter = FakeAdapter([_answer(events=[_step_event("click", "a"),
                                           _step_event("click", "b")])])
    recorder._tick(adapter, rec, {}, {})
    assert len(adapter.rendered) == 2
    assert [s["target"] for s in adapter.rendered[-1]["steps"]] == ["a", "b"]


# ---------------------------------------------------------------- navigation

def test_a_navigation_brings_the_panel_back_with_its_steps(tree):
    """The renderer survives a new document; its state does not, so Python re-seeds it."""
    rec = _recording(tree)
    rec.active = True   # already announced; a navigation must not announce again
    rec.add({"action": "click", "target": "first", "value": None})
    adapter = FakeAdapter([_answer(running=False)])
    recorder._tick(adapter, rec, {}, {})
    assert any(c.startswith("recorder.start") for c in adapter.calls)
    assert adapter.rendered[-1]["steps"] == [{"action": "click", "target": "first",
                                              "value": "", "doc": ""}]
    assert rec.active                      # still the same recording


# ------------------------------------------------------------------ finishing

def test_finishing_writes_a_scenario_that_compiles(tree):
    """The property that matters most: a recording is an ordinary scenario."""
    rec = _recording(tree, "recorded_smoke")
    rec.active = True
    adapter = FakeAdapter([_answer(events=[
        _step_event("click", "menu_settings"),
        _step_event("fill", "login_input", value="admin"),
        _step_event("assert_visible", "dashboard"),
        {"kind": "finish"},
    ])])
    recorder._tick(adapter, rec, {}, {})

    assert rec.finished and not rec.active
    path = os.path.join(str(tree), "scenarios", "recorded_smoke.yaml")
    assert os.path.exists(path)
    steps = compiler.compile_scenario("recorded_smoke", selectors=loader.load_selectors(),
                                      ctx=flowfile.CHECK_CONTEXT)
    assert [s.action for s in steps] == ["click", "fill", "assert_visible"]
    assert any(c.startswith("recorder.stop") for c in adapter.calls)


def test_a_recording_is_tagged_template(tree):
    # A draft should not join --run-tests=all until someone has looked at it.
    rec = _recording(tree, "draft")
    rec.add({"action": "assert_visible", "target": "dashboard", "value": None})
    assert rec.save()["ok"]
    assert loader.load_flow("draft").tags == ["template"]


def test_the_default_name_is_a_timestamp(tree):
    rec = recorder.Recording("s", None, None)
    assert rec.scenario_id.startswith("recorded_")
    assert flowfile.safe_id(rec.scenario_id) == rec.scenario_id


def test_an_empty_recording_still_produces_a_valid_file(tree):
    rec = _recording(tree, "nothing")
    result = rec.save()
    assert result["ok"]
    assert loader.load_flow("nothing").steps == []


# -------------------------------------------------------------- what we ship

def test_the_in_page_recorder_is_valid_javascript():
    """It is shipped as source and evaluated in the page; a typo is a dead panel."""
    node = _node()
    if not node:
        pytest.skip("no node available to parse the recorder")
    for name in ("recorder.js", "hud.js"):
        path = os.path.join(os.path.dirname(os.path.abspath(recorder.__file__)), name)
        done = subprocess.run([node, "--check", path], capture_output=True, text=True)
        assert done.returncode == 0, "%s: %s" % (name, done.stderr)


def test_the_recorder_needs_no_extension_of_its_own():
    """It is shown by the launcher, not summoned from a menu.

    The extension existed only to carry a right-click into the page. Showing the
    panel on attach removes the extension, the contextMenus permission, the
    per-profile install and the DOM flag they talked over.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert not os.path.exists(os.path.join(root, "extensions", "_recorder"))
    with open(os.path.join(root, "engine", "recorder.js"), encoding="utf-8") as handle:
        source = handle.read()
    assert "takeRequest" not in source
    assert "data-cms-record" not in source


def _node():
    import shutil

    return shutil.which("node")


# A page small enough to build by hand and real enough for selector synthesis:
# tag, #id, classes, [attr="value"], :nth-of-type, comma groups and descendant
# combinators - which is everything the recorder writes and everything it asks a
# document about. The tests below say what the shape is; this says how it answers.
_DOM = r"""
function mk(tag, props) {
  var el = {tagName: tag.toUpperCase(), className: '', id: '', children: [],
            parentElement: null, attrs: {}};
  for (var key in (props || {})) el[key] = props[key];
  el.attrs = Object.assign({}, (props || {}).attrs);
  if (el.id) el.attrs.id = el.id;
  el.getAttribute = function (k) {
    return el.attrs[k] === undefined ? null : el.attrs[k];
  };
  return el;
}

function adopt(parent, kids) {
  kids.forEach(function (kid) { kid.parentElement = parent; parent.children.push(kid); });
  return parent;
}

function one(el, part) {
  if (!part) return false;
  var m = part.match(
    /^([a-z]+)?(#[\w-]+)?((?:\.[\w-]+)*)((?:\[[^\]]*\])*)(?::nth-of-type\((\d+)\))?$/);
  if (!m) return false;
  if (m[1] && (el.tagName || '').toLowerCase() !== m[1]) return false;
  if (m[2] && el.attrs.id !== m[2].slice(1)) return false;
  var classes = m[3] ? m[3].slice(1).split('.') : [];
  for (var i = 0; i < classes.length; i++) {
    if ((' ' + el.className + ' ').indexOf(' ' + classes[i] + ' ') < 0) return false;
  }
  var attrs = m[4] ? (m[4].match(/\[[^\]]*\]/g) || []) : [];
  for (var j = 0; j < attrs.length; j++) {
    var pair = attrs[j].slice(1, -1).match(/^([\w-]+)="?([^"]*)"?$/);
    if (!pair || String(el.getAttribute(pair[1])) !== pair[2]) return false;
  }
  if (m[5]) {
    var same = (el.parentElement ? el.parentElement.children : []).filter(function (x) {
      return x.tagName === el.tagName;
    });
    if (same.indexOf(el) !== +m[5] - 1) return false;
  }
  return true;
}

function sel(el, selector) {
  return String(selector).split(',').some(function (group) {
    var parts = group.trim().split(/\s+/);
    if (!one(el, parts[parts.length - 1])) return false;
    var node = el.parentElement;
    for (var i = parts.length - 2; i >= 0; i--) {
      while (node && !one(node, parts[i])) node = node.parentElement;
      if (!node) return false;
      node = node.parentElement;
    }
    return true;
  });
}

function tree(root) {
  var out = [root];
  root.children.forEach(function (kid) { out = out.concat(tree(kid)); });
  return out;
}

function install(root) {
  var all = tree(root);
  all.forEach(function (el) {
    el.matches = function (s) { return sel(el, s); };
    el.querySelectorAll = function (s) {
      return tree(el).slice(1).filter(function (x) { return sel(x, s); });
    };
  });
  // The document is replaced; the window is not, because the recorder has
  // already been loaded onto it by the time a test builds its page.
  global.document.getElementById = function (id) {
    return all.filter(function (e) { return e.attrs.id === id; })[0] || null;
  };
  global.document.querySelectorAll = function (s) {
    return all.filter(function (e) { return sel(e, s); });
  };
  return all;
}

// What the compiler knows, which is what the menu is allowed to offer.
var GRAMMAR = {selector_only: ['click', 'wait_for', 'assert_exists', 'assert_visible',
                               'assert_not_visible'],
               selector_and_value: ['fill', 'assert_text_contains'],
               value_only: ['press'], url_target: ['goto']};

// The Odoo list the report came from: three lines, each with a record selector
// in its first cell and data cells beside it that have nothing to do with it.
function listRow() {
  var rows = [], cells = [];
  for (var i = 0; i < 3; i++) {
    var box = mk('input', {id: 'checkbox-comp-' + (i + 2),
                           className: 'form-check-input', attrs: {type: 'checkbox'}});
    var selector = adopt(mk('td', {className: 'o_list_record_selector'}), [box]);
    var ttn = mk('td', {className: 'o_data_cell'});
    var status = mk('td', {className: 'o_data_cell'});
    rows.push(adopt(mk('tr', {className: 'o_data_row'}), [selector, ttn, status]));
    cells.push({box: box, cell: selector, ttn: ttn});
  }
  var table = adopt(mk('table', {className: 'o_list_table'}), rows);
  var body = adopt(mk('body'), [table]);
  install(body);
  return {box: cells[0].box, cell: cells[0].cell, ttn: cells[0].ttn,
          rows: rows, table: table, body: body};
}
"""


_LOAD = """
global.document = {
  getElementById: function () { return null; },
  querySelectorAll: function () { return []; },
  createElement: function () {
    return {style: {}, setAttribute: function () {}, appendChild: function () {}};
  },
  documentElement: {appendChild: function () {}, hasAttribute: function () { return false; },
                    removeAttribute: function () {}},
  addEventListener: function () {}, removeEventListener: function () {}
};
global.window = {};
require(%s);
var r = window.__Recorder;
r.configure({}, GRAMMAR);
"""


def _run(node, body):
    """Run one harness against recorder.js and return what it printed."""
    path = os.path.join(os.path.dirname(os.path.abspath(recorder.__file__)),
                        "recorder.js")
    harness = _DOM + (_LOAD % json.dumps(path)) + body
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip())


# ------------------------------------------------- the two that bit in testing
# Both were invisible from Python: one threw inside a swallowed evaluate, the
# other made the recorder eat its own menu. They are the reason these exist.

def test_the_evaluate_wrapper_binds_the_name_the_callers_use():
    """recorder_call's little expressions are written against `recorder`.

    Binding it to anything else makes every poll throw a ReferenceError, which
    the adapter swallows by design - so the recorder simply never starts and
    nothing anywhere says why.
    """
    from adapters.playwright_adapter import PlaywrightAdapter

    class FakePage:
        def __init__(self):
            self.expression = None

        def evaluate(self, expression, argument=None):
            self.expression = expression
            return {"ok": True}

    adapter = PlaywrightAdapter.__new__(PlaywrightAdapter)
    adapter._page = FakePage()
    assert adapter.recorder_call("recorder.running()") == {"ok": True}
    assert "const recorder = window.__Recorder" in adapter._page.expression


def test_a_click_on_the_recorders_own_panel_is_left_alone():
    """Arming must not swallow the menu it just opened.

    While armed the recorder takes every click, so it has to recognise its own.
    Events leaving a shadow tree are retargeted to the host, so a click on a menu
    item arrives as the host element - which is exactly what has to pass through.
    """
    node = _node()
    if not node:
        pytest.skip("no node available to run the recorder")
    path = os.path.join(os.path.dirname(os.path.abspath(recorder.__file__)),
                        "recorder.js")
    harness = """
      const HOST = '__cms_recorder_host__';
      const host = {id: HOST, contains: (el) => el && el.inside === true};
      global.document = {
        getElementById: (id) => (id === HOST ? host : null),
        createElement: () => ({style: {}, setAttribute() {}, appendChild() {}}),
        documentElement: {appendChild() {}, hasAttribute: () => false,
                          removeAttribute() {}},
        addEventListener() {}, removeEventListener() {}
      };
      global.window = {};
      require(%s);
      const r = window.__Recorder;
      const results = {
        host: r._ours({target: host}),
        inside: r._ours({target: {inside: true}}),
        page: r._ours({target: {inside: false, id: 'app'}}),
        nothing: r._ours({target: null})
      };
      console.log(JSON.stringify(results));
    """ % json.dumps(path)
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    results = json.loads(done.stdout.strip())
    assert results["host"] is True         # the retargeted case: a menu click
    assert results["inside"] is True
    assert results["nothing"] is True      # nothing to pick is not a pick
    assert results["page"] is False        # an ordinary element IS pickable


def test_a_value_is_never_asked_for_with_window_prompt():
    """`fill` asked for its value with window.prompt, and so never worked.

    The recorder is driven over CDP, and Playwright dismisses a page's dialogs by
    default: prompt() returned null, the step was dropped, and nothing anywhere
    said why. The end-to-end test did not catch it because the driver stubbed
    window.prompt - it tested around the bug. Asking has to happen inside the
    recorder's own panel, where no dialog is involved.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(recorder.__file__)),
                        "recorder.js")
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    # Read the code, not the comments: the comment explaining why this rule
    # exists names the very functions it forbids.
    code = "\n".join(line.split("//")[0] for line in source.splitlines())
    for banned in ("prompt(", "confirm(", "alert("):
        assert banned not in code, "recorder.js still calls %s" % banned
    # What replaced it: an input rendered into the shadow DOM.
    assert "_askValue" in source and "menu-value" in source


# --------------------------------------------------- continuing what exists
# A recording is usually one more pass over something half-written. Naming an
# existing scenario has to add to it, not replace it - the steps already there
# are work, and so are its name and its tags.

def test_naming_an_existing_scenario_loads_its_steps(tree):
    flowfile.save("wip", meta={"name": "Half done", "tags": ["smoke", "wip"]},
                  steps=[{"action": "use", "target": "auth.login"},
                         {"action": "click", "target": "menu_settings"}])
    rec = recorder.Recording("s", "wip", None)
    assert rec.continuing
    assert [s["action"] for s in rec.steps] == ["use", "click"]


def test_continuing_appends_and_keeps_what_the_file_said(tree):
    flowfile.save("wip", meta={"name": "Half done", "description": "mine",
                               "tags": ["smoke", "wip"]},
                  steps=[{"action": "click", "target": "menu_settings"}])
    rec = recorder.Recording("s", "wip", None)
    rec.add({"action": "assert_visible", "target": "dashboard", "value": None})
    assert rec.save()["ok"]

    flow = loader.load_flow("wip")
    assert [compiler.parse_step(s).action for s in flow.steps] == [
        "click", "assert_visible"]
    # Its own name and tags are edits somebody made; a recorder does not undo them.
    assert flow.name == "Half done"
    assert flow.tags == ["smoke", "wip"]
    assert flow.description == "mine"


def test_a_new_recording_is_not_marked_as_continuing(tree):
    rec = recorder.Recording("s", "brand_new", None)
    assert not rec.continuing and rec.steps == []
    assert rec.scenario_id == "brand_new"


def test_recording_into_a_bundled_scenario_is_refused(tree):
    """Otherwise the steps are collected and then thrown away at the end."""
    with pytest.raises(recorder.FlowNotWritable) as exc:
        recorder.Recording("s", "demo_smoke", None)
    assert "duplicate it" in str(exc.value)


def test_continuing_survives_a_navigation_with_every_step(tree):
    # The panel is re-seeded from Python, so what it shows after a reload has to
    # include the steps that were already in the file.
    flowfile.save("wip", steps=[{"action": "click", "target": "first"}])
    rec = recorder.Recording("s", "wip", None)
    rec.active = True
    adapter = FakeAdapter([_answer(running=False)])
    recorder._tick(adapter, rec, {}, {})
    assert [s["target"] for s in adapter.rendered[-1]["steps"]] == ["first"]


def test_a_selector_is_never_just_a_tag_name():
    """`click: "a"` was a real recording, and it means every link on the page.

    Synthesis used to give up after four ancestors and return whatever it had.
    For a plain <a> in an unremarkable list - Odoo's search dropdown, where this
    came from - that is the tag on its own. Counting position among siblings is
    ugly and it is still infinitely better than that.
    """
    node = _node()
    if not node:
        pytest.skip("no node available to run the recorder")
    path = os.path.join(os.path.dirname(os.path.abspath(recorder.__file__)),
                        "recorder.js")
    # A dropdown of bare anchors, which is the shape that produced "a".
    harness = """
      const HOST = '__cms_recorder_host__';
      // Minimal DOM: enough for querySelectorAll/matches over one small tree.
      const anchors = [];
      const list = {tagName: 'UL', className: 'o_searchview_autocomplete',
                    getAttribute: () => null, id: '', children: anchors,
                    parentElement: null};
      for (let i = 0; i < 3; i++) {
        anchors.push({tagName: 'A', className: '', id: '', children: [],
                      getAttribute: () => null, parentElement: list});
      }
      function matchesSel(el, sel) {
        if (sel === 'a') return el.tagName === 'A';
        if (sel === 'ul.o_searchview_autocomplete') return el === list;
        const m = sel.match(/^a:nth-of-type\\((\\d+)\\)$/);
        if (m) return el.tagName === 'A' && anchors.indexOf(el) === +m[1] - 1;
        const s = sel.match(/^ul\\.o_searchview_autocomplete a:nth-of-type\\((\\d+)\\)$/);
        if (s) return el.tagName === 'A' && anchors.indexOf(el) === +s[1] - 1;
        if (sel === 'ul.o_searchview_autocomplete a') return el.tagName === 'A';
        return false;
      }
      const all = [list].concat(anchors);
      global.document = {
        getElementById: () => null,
        querySelectorAll: (sel) => all.filter(e => matchesSel(e, sel)),
        createElement: () => ({style: {}, setAttribute() {}, appendChild() {}}),
        documentElement: {appendChild() {}, hasAttribute: () => false,
                          removeAttribute() {}},
        addEventListener() {}, removeEventListener() {}
      };
      anchors.forEach(a => { a.matches = (s) => matchesSel(a, s); });
      list.matches = (s) => matchesSel(list, s);
      global.window = {};
      require(%s);
      const r = window.__Recorder;
      r.configure({}, {});
      console.log(JSON.stringify(r._describe(anchors[1])));
    """ % json.dumps(path)
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    described = json.loads(done.stdout.strip())
    assert described["selector"] != "a"
    assert "nth-of-type(2)" in described["selector"]
    assert described["unique"] is True


def test_a_numeric_id_is_a_render_counter_and_not_a_name():
    """`#[id="49"]` reached a scenario file and the runner refused to parse it.

    The same search dropdown as above, one fault further in. Odoo numbers those
    rows from a counter - 49, 65 - which read as ids worth keeping, so synthesis
    stopped there. Two things then went wrong at once: `#` takes an identifier
    and `49` is not one, so the id fell to the attribute form, which arrives with
    its own brackets and got a `#` glued in front of it anyway; and the number
    would have been worthless even spelled correctly, because the next render
    hands out different ones.

    The fake browser here throws on a `#` that is not followed by an identifier,
    which is the part a permissive matcher would have let through: every lookup
    inside synthesis is wrapped in a try/catch, so an unparseable selector comes
    back looking merely ambiguous and is written down.
    """
    node = _node()
    if not node:
        pytest.skip("no node available to run the recorder")
    path = os.path.join(os.path.dirname(os.path.abspath(recorder.__file__)),
                        "recorder.js")
    harness = """
      const rows = [];
      const list = {tagName: 'UL', className: 'o_searchview_autocomplete',
                    getAttribute: () => null, id: '', children: rows,
                    parentElement: null};
      ['47', '48', '49'].forEach((id) => {
        rows.push({tagName: 'LI', className: 'o_menu_item', id: id, children: [],
                   getAttribute: () => null, parentElement: list});
      });
      function matchesSel(el, sel) {
        // A browser parses before it matches, and refuses this outright.
        if (/#(?![A-Za-z_])/.test(sel)) {
          throw new Error('Unexpected token while parsing css selector "' + sel + '"');
        }
        if (sel === 'li.o_menu_item') return el.tagName === 'LI';
        if (sel === 'ul.o_searchview_autocomplete') return el === list;
        if (sel === 'ul.o_searchview_autocomplete li.o_menu_item') return el.tagName === 'LI';
        const m = sel.match(/^li\\.o_menu_item:nth-of-type\\((\\d+)\\)$/);
        if (m) return el.tagName === 'LI' && rows.indexOf(el) === +m[1] - 1;
        const s = sel.match(/^\\[id="(\\d+)"\\]$/);
        if (s) return el.id === s[1];
        return false;
      }
      const all = [list].concat(rows);
      global.document = {
        getElementById: () => null,
        querySelectorAll: (sel) => all.filter(e => matchesSel(e, sel)),
        createElement: () => ({style: {}, setAttribute() {}, appendChild() {}}),
        documentElement: {appendChild() {}, hasAttribute: () => false,
                          removeAttribute() {}},
        addEventListener() {}, removeEventListener() {}
      };
      all.forEach(e => { e.matches = (s) => matchesSel(e, s); });
      global.window = {};
      require(%s);
      const r = window.__Recorder;
      r.configure({}, {});
      console.log(JSON.stringify(r._describe(rows[2])));
    """ % json.dumps(path)
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    described = json.loads(done.stdout.strip())

    # Nothing that will not parse, and nothing keyed on the counter either.
    assert "#[" not in described["selector"]
    assert "49" not in described["selector"]
    # What is left is the structural path, which does pick out the row.
    assert described["selector"] == "li.o_menu_item:nth-of-type(3)"
    assert described["unique"] is True


def test_an_id_a_selector_cannot_spell_is_written_the_long_way():
    """Not every id is a counter, and not every id fits behind a `#`.

    `2fa-code` is a name somebody chose, worth keeping - and still not a CSS
    identifier, because identifiers do not start with a digit. It has to come out
    as the attribute form on its own, with no `#` in front of it.
    """
    node = _node()
    if not node:
        pytest.skip("no node available to run the recorder")
    path = os.path.join(os.path.dirname(os.path.abspath(recorder.__file__)),
                        "recorder.js")
    harness = """
      const field = {tagName: 'INPUT', className: '', id: '2fa-code', children: [],
                     getAttribute: () => null, parentElement: null};
      function matchesSel(el, sel) {
        if (/#(?![A-Za-z_])/.test(sel)) {
          throw new Error('Unexpected token while parsing css selector "' + sel + '"');
        }
        return sel === '[id="2fa-code"]' && el === field;
      }
      global.document = {
        getElementById: () => null,
        querySelectorAll: (sel) => [field].filter(e => matchesSel(e, sel)),
        createElement: () => ({style: {}, setAttribute() {}, appendChild() {}}),
        documentElement: {appendChild() {}, hasAttribute: () => false,
                          removeAttribute() {}},
        addEventListener() {}, removeEventListener() {}
      };
      field.matches = (s) => matchesSel(field, s);
      global.window = {};
      require(%s);
      const r = window.__Recorder;
      r.configure({}, {});
      console.log(JSON.stringify(r._describe(field)));
    """ % json.dumps(path)
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    described = json.loads(done.stdout.strip())
    assert described["selector"] == '[id="2fa-code"]'
    assert described["unique"] is True


def test_a_radio_offers_a_way_to_check_which_one_is_on():
    """"Is this option selected?" is the question none of the assertions answer.

    CSS says it exactly - :checked - so it needs no new step type, and the tree
    already writes selectors that way by hand
    (flows/selectors.yaml: roles_wizard_agent_checked). What the recorder has to
    do is find the input behind the label somebody actually clicked, and name
    which option it is: every radio in a group shares its `name`.
    """
    node = _node()
    if not node:
        pytest.skip("no node available to run the recorder")
    path = os.path.join(os.path.dirname(os.path.abspath(recorder.__file__)),
                        "recorder.js")
    # Odoo's shape: a wrapping div per option, the input carrying data-value.
    harness = """
      const options = ['agent', 'manager', 'ov_manager'];
      const inputs = [], wrappers = [];
      options.forEach((value, i) => {
        const input = {tagName: 'INPUT', className: 'form-check-input o_radio_input',
                       id: '', children: [], querySelectorAll: () => [],
                       attrs: {type: 'radio', name: 'role', 'data-value': value}};
        input.getAttribute = (k) => input.attrs[k] || null;
        const wrap = {tagName: 'DIV', className: 'form-check o_radio_item', id: '',
                      children: [input], getAttribute: () => null,
                      querySelectorAll: (s) => (/radio|checkbox/.test(s) ? [input] : [])};
        input.parentElement = wrap;
        inputs.push(input); wrappers.push(wrap);
      });
      const field = {tagName: 'DIV', className: 'o_field_widget', id: '',
                     children: wrappers, parentElement: null,
                     getAttribute: (k) => (k === 'name' ? 'role' : null),
                     querySelectorAll: () => inputs};
      wrappers.forEach(w => { w.parentElement = field; });
      const all = [field].concat(wrappers, inputs);
      function sel(el, s) {
        if (s === '[name="role"]') return el === field || inputs.includes(el);
        if (s === 'div.form-check.o_radio_item') return wrappers.includes(el);
        const m = s.match(/^input\\[type="radio"\\]\\[name="role"\\]\\[data-value="([a-z_]+)"\\]$/);
        if (m) return inputs.includes(el) && el.getAttribute('data-value') === m[1];
        return false;
      }
      global.document = {
        getElementById: () => null,
        querySelectorAll: (s) => all.filter(e => sel(e, s)),
        createElement: () => ({style: {}, setAttribute() {}, appendChild() {}}),
        documentElement: {appendChild() {}, hasAttribute: () => false,
                          removeAttribute() {}},
        addEventListener() {}, removeEventListener() {}
      };
      all.forEach(e => { e.matches = (s) => sel(e, s); });
      global.window = {};
      require(%s);
      const r = window.__Recorder;
      r.configure({}, {selector_only: ['click', 'wait_for', 'assert_exists',
                                       'assert_visible', 'assert_not_visible'],
                       selector_and_value: ['fill'], value_only: ['press'],
                       url_target: ['goto']});
      // Picking the wrapper, which is what clicking the label lands on.
      const described = r._describe(wrappers[2]);
      console.log(JSON.stringify({checkable: described.checkable,
                                  actions: r._actionsFor(described)}));
    """ % json.dumps(path)
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout.strip())

    # It found the input behind the label, and named which option it is.
    assert out["checkable"] == 'input[type="radio"][name="role"][data-value="ov_manager"]'
    offered = {(a["action"], a["selector"]) for a in out["actions"]}
    assert ("assert_exists", out["checkable"] + ":checked") in offered
    assert ("assert_exists", out["checkable"] + ":not(:checked)") in offered
    assert ("wait_for", out["checkable"] + ":checked") in offered
    # The wrapper holds the input, so it is still a thing of its own to click.
    assert ("click", "") in offered


# ------------------------------------------- what is nearby is not what is meant
# The rule above - "the only checkbox in some ancestor" - was too generous on its
# own. Every row of an Odoo list holds exactly one checkbox, its record selector,
# so picking any cell of any row came back as that row's checkbox: the four
# boolean entries were offered and `click`, the one thing wanted, was suppressed.
# What separates the two shapes is a label: an option is a control and its name
# drawn as one thing, and a record selector has no name at all.

def test_a_row_cell_is_not_the_rows_checkbox():
    """A cell in a list row is a cell, however many checkboxes the row holds."""
    node = _node()
    if not node:
        pytest.skip("no node available to run the recorder")
    out = _run(node, """
      var page = listRow();
      var described = r._describe(page.ttn);
      console.log(JSON.stringify({checkable: described.checkable,
                                  via: described.checkableVia,
                                  selector: described.selector,
                                  actions: r._actionsFor(described)}));
    """)
    assert out["checkable"] == ""
    assert out["via"] == ""
    # The cell describes itself, and it is the first thing offered.
    assert out["selector"] == "td.o_data_cell:nth-of-type(2)"
    assert out["actions"][0] == {"action": "click", "why": "click it", "selector": ""}
    assert not [a for a in out["actions"] if ":checked" in a["selector"]]


def test_a_label_beside_a_checkbox_still_finds_it():
    """The shape the ancestor search exists for, which has to keep working.

    The label is the input's sibling, and what was picked is the text inside that
    label - so neither the element nor anything under it is the checkbox, and only
    the `for` pointing back at it says the two belong together.
    """
    node = _node()
    if not node:
        pytest.skip("no node available to run the recorder")
    out = _run(node, """
      var box = mk('input', {id: 'agree', className: 'form-check-input',
                             attrs: {type: 'checkbox', value: 'yes'}});
      var label = mk('label', {className: 'form-check-label', attrs: {for: 'agree'}});
      var text = mk('span', {className: 'o_form_label'});
      adopt(label, [text]);
      var wrap = adopt(mk('div', {className: 'form-check'}), [box, label]);
      install(adopt(mk('div', {className: 'o_setting_box'}), [wrap]));
      var described = r._describe(text);
      console.log(JSON.stringify({checkable: described.checkable,
                                  via: described.checkableVia,
                                  actions: r._actionsFor(described)}));
    """)
    assert out["via"] == "near"
    assert out["checkable"] == 'input[type="checkbox"][value="yes"]'
    offered = {(a["action"], a["selector"]) for a in out["actions"]}
    assert ("assert_exists", out["checkable"] + ":checked") in offered
    assert ("click", out["checkable"]) in offered


def test_the_cell_a_checkbox_is_in_can_still_be_clicked_as_itself():
    """Two different steps, and the menu has to offer both.

    Clicking the cell a record selector sits in is not ticking the record
    selector, and picking the input itself is - which is the one case where a
    second `click` entry would be the same step twice.
    """
    node = _node()
    if not node:
        pytest.skip("no node available to run the recorder")
    out = _run(node, """
      var page = listRow();
      var cell = r._describe(page.cell);
      var box = r._describe(page.box);
      console.log(JSON.stringify({
        cell: {via: cell.checkableVia, checkable: cell.checkable,
               actions: r._actionsFor(cell)},
        box: {via: box.checkableVia, checkable: box.checkable,
              actions: r._actionsFor(box)}}));
    """)
    cell, box = out["cell"], out["box"]
    # Odoo numbers that checkbox by component, so the id is not a name to keep.
    assert cell["checkable"] == 'input[type="checkbox"]'
    assert cell["via"] == "inside"
    clicks = [(a["why"], a["selector"]) for a in cell["actions"] if a["action"] == "click"]
    assert clicks == [("select it", cell["checkable"]), ("click the td itself", "")]

    assert box["via"] == "self"
    assert [(a["why"], a["selector"]) for a in box["actions"] if a["action"] == "click"] \
        == [("select it", box["checkable"])]


# --------------------------------------------------------- "the third line"
# Counting is the only way to name a row that has nothing unique in it, and CSS
# cannot do the counting that is meant: :nth-of-type counts siblings of a tag
# inside one parent, so from a cell it counts the columns beside it. Playwright's
# :nth-match counts matches, which is the question actually being asked.

def test_a_row_can_be_named_by_its_number_among_rows():
    """The two counts differ, and the recording has to be able to say which.

    A grouped list is where they part company: the group header is a <tr> too, so
    the third data row is the fourth row of the table. Both selectors below find
    the same row - one by saying where it sits, one by saying which match it is -
    and only the second still means "the third line" once a group is collapsed.
    """
    node = _node()
    if not node:
        pytest.skip("no node available to run the recorder")
    out = _run(node, """
      var header = mk('tr', {className: 'o_group_header'});
      var rows = [];
      for (var i = 0; i < 3; i++) rows.push(mk('tr', {className: 'o_data_row'}));
      var table = adopt(mk('table', {className: 'o_list_table'}),
                        [header].concat(rows));
      install(adopt(mk('body'), [table]));
      var third = rows[2];
      var described = r._describe(third);
      // What the step would say with the count switched on, and without it.
      r._pickedEl = third;
      r._nth = r._nthFor(third);
      r._byNth = true;
      var counted = r._targetFor(described);
      r._byNth = false;
      console.log(JSON.stringify({structural: described.selector, nth: r._nth,
                                  counted: counted, plain: r._targetFor(described)}));
    """)
    # Where it sits: the fourth <tr>, because the group header is one as well.
    assert out["structural"] == "tr.o_data_row:nth-of-type(4)"
    # Which match it is: the third row. Same element, different question.
    assert out["nth"]["selector"] == ":nth-match(tr.o_data_row, 3)"
    assert out["nth"]["index"] == 3
    assert out["nth"]["total"] == 3
    # And the mode reaches the step that gets written down.
    assert out["counted"] == ":nth-match(tr.o_data_row, 3)"
    assert out["plain"] == "tr.o_data_row:nth-of-type(4)"


def test_counting_cells_is_not_counting_rows():
    """The trap the recorder used to leave you in, and the reason for the row above.

    `[name="shipment_number"]:nth-of-type(4)` was a real recording. It reads as
    "the fourth line" and means "the fourth column", which every line has - so it
    matched one cell per row and the step acted on the first.
    """
    node = _node()
    if not node:
        pytest.skip("no node available to run the recorder")
    out = _run(node, """
      var page = listRow();
      console.log(JSON.stringify({row: r._nthFor(page.rows[2]),
                                  cell: r._nthFor(page.rows[2].children[1]),
                                  alone: r._nthFor(page.table)}));
    """)
    assert out["row"]["selector"] == ":nth-match(tr.o_data_row, 3)"
    # The same pick one level in: six data cells over three rows, and the third
    # row's first one is the fifth of them. Nothing there says "row 3".
    assert out["cell"]["selector"] == ":nth-match(td.o_data_cell, 5)"
    # One of a kind has no position worth offering, so the menu does not offer it.
    assert out["alone"] is None


def test_a_row_leads_with_itself_and_the_cell_around_a_box_leads_with_the_box():
    """Widening out to a row finds its record selector again, two levels in.

    It is still a real thing to offer - ticking the row is an action - but it is
    not what the row IS, so the row's own click comes first. One level in is the
    other way round: a cell drawn around a control is that control, and leads
    with it.
    """
    node = _node()
    if not node:
        pytest.skip("no node available to run the recorder")
    out = _run(node, """
      var page = listRow();
      var row = r._describe(page.rows[0]);
      var cell = r._describe(page.cell);
      console.log(JSON.stringify({
        row: {via: row.checkableVia, depth: row.checkableDepth,
              actions: r._actionsFor(row).map(function (a) { return a.why; })},
        cell: {depth: cell.checkableDepth,
               actions: r._actionsFor(cell).map(function (a) { return a.why; })}}));
    """)
    assert out["row"]["via"] == "inside" and out["row"]["depth"] == 2
    assert out["row"]["actions"][0] == "click the tr itself"
    assert "select it" in out["row"]["actions"]        # still offered, just not first
    assert out["cell"]["depth"] == 1
    assert out["cell"]["actions"][0] == "check it IS selected"


def test_widening_walks_out_to_the_row_and_stops_at_the_page():
    """A cell is as close to the row as the pointer can get.

    Cells fill the row, so there is no point on screen where the picked element
    is the <tr> - without walking outwards a whole-line step cannot be recorded
    at all. Outwards ends at the page: <body> is not a thing to act on.
    """
    node = _node()
    if not node:
        pytest.skip("no node available to run the recorder")
    out = _run(node, """
      var page = listRow();
      console.log(JSON.stringify({
        fromCell: (r._around(page.ttn) || {}).tagName || null,
        fromRow: (r._around(page.rows[0]) || {}).tagName || null,
        fromTable: r._around(page.table),
        fromBody: r._around(page.body)}));
    """)
    assert out["fromCell"] == "TR"
    assert out["fromRow"] == "TABLE"
    assert out["fromTable"] is None      # its parent is <body>
    assert out["fromBody"] is None       # and <body> has no parent at all


# ----------------------------------------------------- fixing it as you go
# A bad capture is obvious while the page is still on screen and much less so
# afterwards, so the panel can delete, reorder and retarget. Python owns the
# list; the panel sends an intent and repaints from what comes back.

def _amend(rec, kind, **fields):
    adapter = FakeAdapter([_answer(events=[dict(fields, kind=kind)])])
    recorder._tick(adapter, rec, {}, {})
    return adapter


def _three(tree):
    rec = _recording(tree)
    rec.active = True
    for target in ("first", "second", "third"):
        rec.add({"action": "click", "target": target, "value": None})
    return rec


def test_a_step_can_be_deleted(tree):
    rec = _three(tree)
    _amend(rec, "delete", index=1)
    assert [s["target"] for s in rec.steps] == ["first", "third"]


def test_a_step_can_be_moved(tree):
    rec = _three(tree)
    _amend(rec, "move", index=0, delta=1)
    assert [s["target"] for s in rec.steps] == ["second", "first", "third"]
    _amend(rec, "move", index=2, delta=-1)
    assert [s["target"] for s in rec.steps] == ["second", "third", "first"]


def test_moving_off_either_end_does_nothing(tree):
    rec = _three(tree)
    _amend(rec, "move", index=0, delta=-1)
    _amend(rec, "move", index=2, delta=1)
    assert [s["target"] for s in rec.steps] == ["first", "second", "third"]


def test_a_step_can_be_retargeted(tree):
    rec = _three(tree)
    _amend(rec, "edit", index=1, target="menu_settings", value="typed")
    assert rec.steps[1]["target"] == "menu_settings"
    assert rec.steps[1]["value"] == "typed"


def test_clearing_a_value_means_no_value(tree):
    # Not the same as leaving it alone: an emptied box removes it from the step.
    rec = _three(tree)
    rec.steps[1]["value"] = "was here"
    _amend(rec, "edit", index=1, target="second", value="")
    assert rec.steps[1]["value"] is None


def test_an_empty_target_leaves_the_step_pointing_where_it_did(tree):
    rec = _three(tree)
    _amend(rec, "edit", index=1, target="", value=None)
    assert rec.steps[1]["target"] == "second"


def test_a_stale_index_is_ignored(tree):
    """The panel can be a repaint behind; a click on a row that moved must not
    delete whatever is there now."""
    rec = _three(tree)
    _amend(rec, "delete", index=9)
    _amend(rec, "delete", index=-1)
    assert len(rec.steps) == 3


def test_the_panel_is_repainted_after_an_amendment(tree):
    rec = _three(tree)
    adapter = _amend(rec, "delete", index=0)
    assert [s["target"] for s in adapter.rendered[-1]["steps"]] == ["second", "third"]


def test_what_is_saved_is_what_the_panel_last_showed(tree):
    rec = _three(tree)
    _amend(rec, "delete", index=1)
    _amend(rec, "edit", index=0, target="menu_settings", value=None)
    assert rec.save()["ok"]
    flow = loader.load_flow(rec.scenario_id)
    assert [compiler.parse_step(s).target for s in flow.steps] == [
        "menu_settings", "third"]


# ----------------------------------------------------------------- the step's doc
#
# What a run's guide says for a step (engine.livedoc). Written in the panel while
# the page it describes is still on screen; kept by Python like the step itself.

def test_a_doc_can_be_added_rewritten_and_deleted(tree):
    rec = _three(tree)
    _amend(rec, "doc", index=1, doc="  Press Settings  ")
    assert rec.steps[1]["doc"] == "Press Settings"
    _amend(rec, "doc", index=1, doc="Open the settings")
    assert rec.steps[1]["doc"] == "Open the settings"
    adapter = _amend(rec, "doc", index=1, doc="")
    # Deleted: no doc at all, and the step stays where it was.
    assert "doc" not in rec.steps[1]
    assert [s["target"] for s in rec.steps] == ["first", "second", "third"]
    assert adapter.rendered[-1]["steps"][1]["doc"] == ""


def test_the_panel_is_painted_with_the_doc(tree):
    rec = _three(tree)
    adapter = _amend(rec, "doc", index=0, doc="Open the menu")
    assert [s["doc"] for s in adapter.rendered[-1]["steps"]] == ["Open the menu", "", ""]


def test_a_doc_for_a_stale_index_is_ignored(tree):
    rec = _three(tree)
    _amend(rec, "doc", index=7, doc="lost")
    assert not any("doc" in step for step in rec.steps)


def test_the_step_editor_carries_the_doc_too(tree):
    rec = _three(tree)
    _amend(rec, "edit", index=2, target="third", value=None, doc="Check it")
    assert rec.steps[2]["doc"] == "Check it"
    _amend(rec, "edit", index=2, target="third", value=None)     # no doc in it
    assert rec.steps[2]["doc"] == "Check it"
    _amend(rec, "edit", index=2, target="third", value=None, doc=" ")
    assert "doc" not in rec.steps[2]


def test_a_recorded_doc_is_written_and_reads_back(tree):
    # Ukrainian, as a guide is usually written; escaped so the source stays English.
    sentence = "\u041d\u0430\u0442\u0438\u0441\u043d\u0456\u0442\u044c Settings"
    rec = _three(tree)
    _amend(rec, "doc", index=0, doc=sentence)
    assert rec.save()["ok"]
    described = flowfile.describe_flow(rec.scenario_id)
    assert [s.get("doc") for s in described["steps"]] == [sentence, None, None]


def test_continuing_keeps_every_doc_and_retry_the_file_had(tree):
    """Finish used to write back only action, target, value, timeout and state -
    every doc: and retry: of a scenario being continued was erased."""
    flowfile.save("wip", steps=[
        {"action": "click", "target": "menu_settings", "doc": "Open the settings"},
        {"action": "click", "target": "save_button",
         "retry": {"attempts": 3, "delay": 1}}])
    rec = recorder.Recording("s", "wip", None)
    rec.add({"action": "assert_visible", "target": "dashboard", "value": None})
    assert rec.save()["ok"]
    steps = flowfile.describe_flow("wip")["steps"]
    assert steps[0]["doc"] == "Open the settings"
    assert steps[1]["retry"] == {"attempts": 3, "delay": 1}
    assert [s["action"] for s in steps] == ["click", "click", "assert_visible"]


def test_the_panel_offers_and_sends_a_doc():
    """The row's marks and the doc editor queue the intents Python acts on."""
    node = _node()
    if not node:
        pytest.skip("no node available to run the recorder")
    path = os.path.join(os.path.dirname(os.path.abspath(recorder.__file__)),
                        "recorder.js")
    harness = r"""
      function Node(tag) {
        this.tagName = tag.toUpperCase(); this.children = []; this.className = '';
        this.textContent = ''; this.title = ''; this.value = ''; this.style = {};
        this.listeners = {}; this.parentNode = null;
      }
      Node.prototype.appendChild = function (c) { c.parentNode = this; this.children.push(c); return c; };
      Node.prototype.removeChild = function (c) {
        this.children = this.children.filter(function (x) { return x !== c; }); c.parentNode = null; };
      Node.prototype.addEventListener = function (k, f) { (this.listeners[k] = this.listeners[k] || []).push(f); };
      Node.prototype.fire = function (k, e) { (this.listeners[k] || []).forEach(function (f) { f(e); }); };
      Node.prototype.setAttribute = function () {};
      Node.prototype.focus = function () {}; Node.prototype.select = function () {};
      Node.prototype.getBoundingClientRect = function () { return {width: 300, height: 120}; };
      function all(node, out) { out = out || []; out.push(node);
        node.children.forEach(function (c) { all(c, out); }); return out; }
      function byClass(node, cls) { return all(node).filter(function (n) {
        return (' ' + n.className + ' ').indexOf(' ' + cls + ' ') >= 0; }); }
      var event = {stopPropagation() {}, preventDefault() {}};
      global.document = {
        getElementById: () => null, createElement: (t) => new Node(t),
        documentElement: {appendChild() {}, hasAttribute: () => false, removeAttribute() {}},
        addEventListener() {}, removeEventListener() {}
      };
      global.window = {innerWidth: 1200, innerHeight: 800};
      require(%s);
      const r = window.__Recorder;
      r._shadow = new Node('shadow');
      const out = {};
      const bare = r._stepRow({action: 'click', target: '#save', doc: ''}, 0, 1);
      out.bareTools = byClass(bare, 'step-btn').map((b) => b.title);
      out.bareDocLine = byClass(bare, 'step-doc').length;

      const said = 'Press Save';
      const row = r._stepRow({action: 'click', target: '#save', doc: said}, 0, 1);
      const line = byClass(row, 'step-doc')[0];
      out.docText = byClass(line, 'step-doc-t')[0].textContent;
      out.docTools = byClass(line, 'step-btn').map((b) => b.title);
      byClass(line, 'step-btn').filter((b) => b.title === 'Delete the doc')[0].fire('click', event);
      out.deleted = r.drain();

      byClass(line, 'step-btn').filter((b) => b.title === 'Edit the doc')[0].fire('click', event);
      const input = all(r._menu).filter((n) => n.tagName === 'INPUT')[0];
      out.editorValue = input.value;
      input.value = 'Press Save to keep it';
      input.fire('keydown', Object.assign({key: 'Enter'}, event));
      out.edited = r.drain();
      out.closed = r._menu === null;

      r._editStep({action: 'click', target: '#save', value: '', doc: said}, 0);
      const fields = all(r._menu).filter((n) => n.tagName === 'INPUT');
      out.stepFields = fields.length;
      fields[2].value = 'Keep it';
      fields[2].fire('keydown', Object.assign({key: 'Enter'}, event));
      out.stepEdit = r.drain();
      console.log(JSON.stringify(out));
    """ % json.dumps(path)
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout.strip())
    assert out["bareTools"] == ["Add a doc for the guide", "Edit this step",
                                "Delete this step"]
    assert out["bareDocLine"] == 0
    assert out["docText"] == "Press Save"
    assert out["docTools"] == ["Edit the doc", "Delete the doc"]
    assert out["deleted"] == [{"kind": "doc", "index": 0, "doc": ""}]
    assert out["editorValue"] == "Press Save"
    assert out["edited"] == [{"kind": "doc", "index": 0, "doc": "Press Save to keep it"}]
    assert out["closed"] is True
    assert out["stepFields"] == 3
    assert out["stepEdit"] == [{"kind": "edit", "index": 0, "target": "#save",
                                "value": "", "doc": "Keep it"}]
