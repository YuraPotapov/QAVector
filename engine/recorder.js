// Scenario Recorder - the part that runs inside the page.
//
// Injected over the launcher's existing CDP connection (engine/recorder.py), the
// same way the execution HUD is, and isolated the same way: one host element on
// <html>, everything inside a Shadow DOM, so the app's CSS and JS never see it.
//
// The panel appears as soon as the window is attached, and capture is still
// EXPLICIT. Moving the mouse, typing, focusing and every intermediate input
// event are ignored - none of them become a step. A step exists only when it is
// asked for: press Capture Step (or F2), hover until the element you want is
// outlined, click it, and choose from the actions that element can take. That is
// one step, and nothing else is.
//
// Python is the one that acts and the one that remembers. This side collects
// what the user picked into a queue and hands it over when drained; the steps
// shown in the panel are pushed back from Python, so the panel survives a
// navigation as soon as the next state arrives rather than keeping its own copy.
//
// Entry points, all on window.__Recorder: start(), stop(), render(state),
// drain(), pending() and armed().
(function () {
  "use strict";
  var HOST_ID = "__cms_recorder_host__";
  var COLLAPSED_KEY = "__cms_recorder_collapsed";

  var CSS = "" +
    ":host{all:initial}" +
    // The outline drawn over whatever the pointer is on. Never interactive: it
    // must not swallow the click that picks the element underneath it.
    ".hi{position:fixed;pointer-events:none;z-index:2147483646;box-sizing:border-box;" +
      "border:2px solid #4da3ff;border-radius:3px;background:rgba(77,163,255,0.16);" +
      "box-shadow:0 0 0 2px rgba(77,163,255,0.35)}" +
    ".hi-tag{position:absolute;top:-19px;left:-2px;padding:1px 6px;border-radius:3px;" +
      "background:#4da3ff;color:#0b1220;white-space:nowrap;max-width:60vw;" +
      "overflow:hidden;text-overflow:ellipsis;" +
      "font:600 11px/1.5 'Segoe UI',system-ui,-apple-system,sans-serif}" +
    ".wrap{position:fixed;top:14px;right:14px;width:380px;max-height:92vh;" +
      "display:flex;flex-direction:column;pointer-events:none;" +
      "font:12px/1.45 'Segoe UI',system-ui,-apple-system,sans-serif;color:#e8eaed;" +
      "z-index:2147483647}" +
    ".card{display:flex;flex-direction:column;min-height:0;max-height:92vh;" +
      "background:rgba(18,19,22,0.92);border:1px solid rgba(255,255,255,0.10);" +
      "border-radius:12px;box-shadow:0 10px 34px rgba(0,0,0,0.5);" +
      "backdrop-filter:blur(7px);-webkit-backdrop-filter:blur(7px);" +
      "pointer-events:auto;overflow:hidden}" +
    ".header{display:flex;align-items:center;gap:8px;padding:8px 10px;cursor:move;" +
      "user-select:none;border-bottom:1px solid rgba(255,255,255,0.08);" +
      "background:rgba(255,255,255,0.04);flex:0 0 auto}" +
    ".h-title{flex:1;font-weight:600;color:#fff;white-space:nowrap;overflow:hidden;" +
      "text-overflow:ellipsis}" +
    ".btn{width:22px;height:22px;display:flex;align-items:center;justify-content:center;" +
      "border-radius:5px;cursor:pointer;color:#cfd3d7;flex:0 0 auto;" +
      "background:rgba(255,255,255,0.06)}" +
    ".btn:hover{background:rgba(255,255,255,0.14);color:#fff}" +
    ".body{padding:8px 10px;overflow:auto;min-height:0;flex:1 1 auto}" +
    ".bar{display:flex;gap:6px;padding:8px 10px;border-bottom:1px solid rgba(255,255,255,0.08);" +
      "flex:0 0 auto}" +
    ".action{flex:1;text-align:center;padding:6px 8px;border-radius:6px;cursor:pointer;" +
      "background:#2f6fd0;color:#fff;font-weight:600}" +
    ".action:hover{background:#3f80e4}" +
    ".action.armed{background:#c9770f}" +
    ".ghost-btn{padding:6px 10px;border-radius:6px;cursor:pointer;" +
      "background:rgba(255,255,255,0.08);color:#e8eaed}" +
    ".ghost-btn:hover{background:rgba(255,255,255,0.16)}" +
    // Collapsed: the header alone, and faded, because a panel you are not using
    // is still sitting on top of the app you are trying to look at. Hovering
    // brings it back to full strength so it can be reopened without hunting.
    ".card.collapsed .body,.card.collapsed .bar,.card.collapsed .hint{display:none}" +
    ".card.collapsed{opacity:0.35;transition:opacity .12s ease}" +
    ".card.collapsed:hover{opacity:1}" +
    ".hint{padding:6px 10px;color:#9aa0a6;border-bottom:1px solid rgba(255,255,255,0.08)}" +
    ".step{display:flex;flex-wrap:wrap;gap:0 8px;padding:4px 2px;align-items:baseline;" +
      "border-bottom:1px solid rgba(255,255,255,0.05)}" +
    ".step-n{color:#6b7076;min-width:18px;text-align:right;flex:0 0 auto}" +
    ".step-a{color:#8ab4f8;font-weight:600;flex:0 0 auto}" +
    ".step-t{color:#cfd3d7;word-break:break-all;flex:1 1 auto;min-width:0}" +
    // Shown on hover: four marks on every row would be louder than the steps.
    ".step-tools{display:flex;gap:2px;flex:0 0 auto;opacity:0}" +
    // The step's marks, not while the pointer is on its doc: that line has its
    // own, and two sets at once would leave it unclear which one acts on what.
    ".step:hover:not(:has(.step-doc:hover))>.step-tools{opacity:1}" +
    // The step's doc: what the run's guide will say for it, on a line of its
    // own under the step, with its own two marks - edit and delete.
    ".step-doc{flex:1 0 100%;box-sizing:border-box;display:flex;gap:6px;" +
      "align-items:baseline;padding:2px 0 1px 26px;color:#c3c7cb;font-style:italic}" +
    ".step-doc-t{flex:1 1 auto;min-width:0;white-space:pre-wrap;overflow-wrap:anywhere}" +
    ".step-doc .step-tools{font-style:normal}" +
    ".step-doc:hover .step-tools{opacity:1}" +
    ".step-btn{width:18px;height:18px;display:flex;align-items:center;" +
      "justify-content:center;border-radius:4px;cursor:pointer;color:#9aa0a6;" +
      "background:rgba(255,255,255,0.06);font-size:11px}" +
    ".step-btn:hover{background:rgba(255,255,255,0.18);color:#fff}" +
    ".empty{color:#9aa0a6;padding:6px 2px}" +
    // The action menu that opens where the element was clicked.
    ".menu{position:fixed;z-index:2147483647;min-width:230px;max-height:60vh;overflow:auto;" +
      "background:rgba(18,19,22,0.97);border:1px solid rgba(255,255,255,0.14);" +
      "border-radius:10px;box-shadow:0 10px 34px rgba(0,0,0,0.6);pointer-events:auto;" +
      "font:12px/1.45 'Segoe UI',system-ui,-apple-system,sans-serif;color:#e8eaed}" +
    ".menu-head{padding:8px 10px;border-bottom:1px solid rgba(255,255,255,0.10);" +
      "color:#9aa0a6;word-break:break-all}" +
    ".menu-sel{color:#8ab4f8;display:block;margin-top:3px}" +
    ".menu-item{padding:7px 10px;cursor:pointer;display:flex;gap:8px;align-items:baseline}" +
    ".menu-item:hover{background:rgba(255,255,255,0.10)}" +
    ".menu-item.on{background:rgba(77,163,255,0.22)}" +
    ".menu-item .key{color:#6b7076;min-width:12px}" +
    ".menu-item .why{color:#9aa0a6;font-size:11px;margin-left:auto}" +
    ".menu-foot{padding:6px 10px;color:#6b7076;font-size:11px;" +
      "border-top:1px solid rgba(255,255,255,0.10)}" +
    ".menu-warn{color:#ffab40;margin-top:4px}" +
    ".menu-toggle{color:#9ad1ff;border-top:1px solid rgba(255,255,255,0.10)}" +
    ".menu-label{color:#9aa0a6;font-size:11px;margin-bottom:3px}" +
    ".menu-value{padding:8px 10px}" +
    ".menu-value input{width:100%;box-sizing:border-box;padding:6px 8px;" +
      "border-radius:6px;border:1px solid rgba(255,255,255,0.22);" +
      "background:rgba(255,255,255,0.06);color:#e8eaed;" +
      "font:12px/1.45 'Segoe UI',system-ui,-apple-system,sans-serif;outline:none}" +
    ".menu-value input:focus{border-color:#4da3ff}" +
    ".menu-cancel{color:#ffab40}";

  function elem(tag, cls) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    return n;
  }

  // ---------------------------------------------------------------- selectors
  //
  // Turning an element into something a flow can name. In order of preference:
  //
  //   1. a name already in selectors.yaml whose selector matches this element -
  //      by far the best outcome, because the recording then reads like the
  //      flows next to it and follows the tree when that name is re-pointed;
  //   2. a structural attribute the app chose deliberately: data-menu-xmlid,
  //      name, data-testid, a stable id;
  //   3. a tag + class path, scoped to the nearest identifiable ancestor.
  //
  // Never text. A selector keyed on a visible label is the one thing guaranteed
  // to break here: the same app renders Ukrainian on one environment and English
  // on another, so :has-text("Save") records green and fails everywhere else.
  // `comp-2` is Owl counting its components: checkbox-comp-2 is checkbox-comp-3
  // on the next render, which is a name only until the page is reloaded.
  var UNSTABLE = /(^|[-_])(ng|css|sc|jsx|emotion)[-_]?[a-z0-9]{4,}|\d{4,}|^o_[a-z]+_\d+$|[-_]comp[-_]\d+/i;

  function stableClasses(el) {
    var out = [];
    var list = (el.className && el.className.baseVal !== undefined)
      ? el.className.baseVal.split(/\s+/)          // SVG
      : String(el.className || "").split(/\s+/);
    for (var i = 0; i < list.length; i++) {
      var c = list[i].trim();
      // A generated class is worse than no class: it changes on the next build
      // and takes the recording with it.
      if (c && !UNSTABLE.test(c)) out.push(c);
    }
    return out;
  }

  function cssEscape(value) {
    return String(value).replace(/["\\]/g, "\\$&");
  }

  function attributeSelector(el) {
    // Attributes an application sets on purpose, most specific first. These are
    // the ones that survive a restyle, a re-render and a translation.
    var named = ["data-menu-xmlid", "data-testid", "data-test", "data-qa", "data-cy",
                 // data-value is how a radio in a group says which option it is;
                 // without it every option in the group looks identical.
                 "data-value", "name", "data-field", "data-name", "aria-controls"];
    for (var i = 0; i < named.length; i++) {
      var value = el.getAttribute && el.getAttribute(named[i]);
      if (value && !UNSTABLE.test(value)) {
        return "[" + named[i] + '="' + cssEscape(value) + '"]';
      }
    }
    if (el.id && !UNSTABLE.test(el.id) && !isCounter(el.id)) return idSelector(el.id);
    return "";
  }

  function isCounter(value) {
    // A number is not a name. Owl hands ids out in order, so the rows of Odoo's
    // search dropdown are 49 and 65 on this render and something else on the
    // next one - a recording keyed on one of those is green once, by luck.
    //
    // Ids only. A numeric `value` or `data-value` is the real answer to "which
    // option is this?", and dropping those would leave every radio in a group
    // looking identical again.
    return /^\d+$/.test(String(value));
  }

  function idSelector(value) {
    // "#" is followed by an identifier, and an id can legally hold things an
    // identifier cannot - a leading digit above all. Those have to be written
    // the long way round: '#' + '[id="49"]' is not a fussier selector, it is one
    // no browser will parse, and it reaches the runner as a recorded step.
    return /^[A-Za-z_][\w-]*$/.test(value)
      ? "#" + value
      : '[id="' + cssEscape(value) + '"]';
  }

  function matchesUniquely(selector, el) {
    try {
      var found = document.querySelectorAll(selector);
      return found.length === 1 && found[0] === el;
    } catch (e) { return false; }
  }

  function matchesAtAll(selector, el) {
    try {
      return el.matches(selector);
    } catch (e) { return false; }
  }

  function namedSelector(el, selectors) {
    // Prefer a name the tree already has. Playwright syntax (:has-text,
    // :nth-match) is not parseable by the browser, so each one is tried and the
    // ones that throw are simply not candidates - Python still resolves them.
    var best = null;
    for (var name in selectors) {
      if (!Object.prototype.hasOwnProperty.call(selectors, name)) continue;
      if (!matchesAtAll(selectors[name], el)) continue;
      // A name that picks out exactly this element beats one that also matches
      // its siblings.
      if (matchesUniquely(selectors[name], el)) return name;
      if (!best) best = name;
    }
    return best;
  }

  function ownSelector(el) {
    var own = attributeSelector(el);
    if (own) return own;
    var tag = (el.tagName || "").toLowerCase();
    var classes = stableClasses(el);
    return tag + (classes.length ? "." + classes.slice(0, 2).join(".") : "");
  }

  function isBareTag(selector) {
    // "a", "div", "span" - true of an ancestor that describes nothing, and of a
    // target that would match every link on the page.
    return /^[a-z][a-z0-9]*$/.test(selector);
  }

  function nthOfType(el) {
    var parent = el.parentElement;
    if (!parent) return "";
    var same = [];
    for (var i = 0; i < parent.children.length; i++) {
      if (parent.children[i].tagName === el.tagName) same.push(parent.children[i]);
    }
    if (same.length < 2) return "";
    return ":nth-of-type(" + (same.indexOf(el) + 1) + ")";
  }

  function structuralSelector(el) {
    // Walk outwards until the selector picks out this element and nothing else.
    //
    // The stopping condition matters more than the order. An earlier version
    // gave up after four ancestors and returned whatever it had, which for a
    // plain <a> inside an unremarkable list is the selector "a" - every link on
    // the page, recorded as a step. Anything is better than that, including
    // counting position among siblings.
    var base = ownSelector(el);
    if (matchesUniquely(base, el)) return base;

    var scoped = scopeUntilUnique(el, base);
    if (scoped) return scoped;

    // Still ambiguous, so pin it among its siblings and try the ancestors again.
    var positioned = base + nthOfType(el);
    if (positioned !== base && matchesUniquely(positioned, el)) return positioned;
    scoped = scopeUntilUnique(el, positioned);
    if (scoped) return scoped;

    // Nothing is unique. Return the most specific thing we built rather than the
    // vaguest, and the menu says it matches more than one.
    return isBareTag(positioned) ? positioned + nthOfType(el) : positioned;
  }

  function scopeUntilUnique(el, base) {
    // Prepend each ancestor that describes something, nearest first, until the
    // pair is unique. Bare-tag ancestors are skipped: "div a" is no better than
    // "a", and it makes the selector longer for nothing.
    var node = el.parentElement;
    for (var depth = 0; node && depth < 8; depth++) {
      var scope = ownSelector(node);
      if (scope && !isBareTag(scope)) {
        var candidate = scope + " " + base;
        if (matchesUniquely(candidate, el)) return candidate;
      }
      node = node.parentElement;
    }
    return "";
  }

  function around(el) {
    // The element a widen moves out to, or null when there is nothing useful
    // outside this one. The page itself is not a thing to record a step about.
    var parent = el && el.parentElement;
    if (!parent) return null;
    var tag = (parent.tagName || "").toLowerCase();
    return (tag === "body" || tag === "html") ? null : parent;
  }

  function ordinal(n) {
    var rest = n % 100;
    if (rest >= 11 && rest <= 13) return n + "th";
    return n + ["th", "st", "nd", "rd"][n % 10 < 4 ? n % 10 : 0];
  }

  function nthSelector(el) {
    // "the third one of these", which no CSS selector can say.
    //
    // :nth-of-type counts siblings of a tag inside one parent, so from a cell it
    // counts the columns beside it - a recording that means "the third line"
    // comes out meaning "the third field of every line". Playwright's own
    // :nth-match counts MATCHES, across the page, which is the question actually
    // being asked. It is not CSS and the browser cannot parse it, so it is built
    // here and resolved on the Python side like the other Playwright forms.
    //
    // null when there is nothing to count: one match is already unique, and a
    // position among one is not an idea worth offering.
    var base = ownSelector(el);
    if (isBareTag(base)) {
      // A bare tag counts every one of them in the document. Put a describing
      // ancestor in front so the count is over the list, not over the page.
      var node = el.parentElement;
      for (var depth = 0; node && depth < 4; depth++) {
        var scope = ownSelector(node);
        if (scope && !isBareTag(scope)) { base = scope + " " + base; break; }
        node = node.parentElement;
      }
    }
    var found;
    try { found = document.querySelectorAll(base); } catch (e) { return null; }
    if (found.length < 2) return null;
    for (var i = 0; i < found.length; i++) {
      if (found[i] !== el) continue;
      return { selector: ":nth-match(" + base + ", " + (i + 1) + ")",
               base: base, index: i + 1, total: found.length };
    }
    return null;
  }

  function describeTarget(el, selectors) {
    var name = namedSelector(el, selectors || {});
    var selector = structuralSelector(el);
    var found = checkableWithin(el);
    return {
      name: name || "",
      selector: selector,
      unique: name ? matchesUniquely((selectors || {})[name], el)
                   : matchesUniquely(selector, el),
      tag: (el.tagName || "").toLowerCase(),
      type: (el.getAttribute && el.getAttribute("type")) || "",
      role: (el.getAttribute && el.getAttribute("role")) || "",
      editable: isEditable(el),
      // "" when nothing here can be checked, which is most things.
      checkable: found ? checkableSelector(found.input) : "",
      // Where that input was found: "self", "inside", "label", "near" - or "".
      // The menu needs it to tell "you picked the checkbox" from "the checkbox is
      // somewhere about", because only the first of those has nothing else to click.
      checkableVia: found ? found.via : "",
      // How far inside it sits, when it is inside: 1 is the cell drawn around a
      // control, more than that is a container the control merely lives in.
      checkableDepth: (found && found.depth) || 0,
      value: readValue(el),
      // Only ever shown in the menu so a person can tell which element they
      // picked; never put into a selector.
      text: (el.textContent || "").trim().slice(0, 60)
    };
  }

  var CHECKABLE = "input[type=radio],input[type=checkbox]";

  function onlyCheckableIn(el) {
    if (!el || !el.querySelectorAll) return null;
    try {
      var found = el.querySelectorAll(CHECKABLE);
      // Exactly one, or there is no way to tell which was meant. This is also
      // what stops the search: the whole radio group holds several.
      return found.length === 1 ? found[0] : null;
    } catch (e) { return null; }
  }

  function generations(ancestor, node, max) {
    // How far `node` sits below `ancestor`, or -1 if not below it within `max`.
    // Walked over parentElement rather than Node.contains: the same walk answers
    // both questions, and the tests drive this with a DOM they build by hand.
    var up = node && node.parentElement;
    for (var depth = 1; up && depth <= max; depth++) {
      if (up === ancestor) return depth;
      up = up.parentElement;
    }
    return -1;
  }

  function related(a, b) {
    // One of them is the other, or holds it. Either way they are about the same
    // thing on screen.
    return a === b || generations(a, b, 6) > 0 || generations(b, a, 6) > 0;
  }

  function labelsIt(el, input) {
    // Does `el` label `input`? The question the ancestor search below turns on.
    //
    // An option is a label and a control drawn as one thing, and the label is
    // what carries its name: `for`, or a <label> wrapped around both, or
    // aria-labelledby. A table row is not - the cells beside its record selector
    // say nothing about it, which is exactly what has to be told apart here.
    var labels = [];
    var id = input.getAttribute && input.getAttribute("id");
    if (id) {
      try {
        var each = document.querySelectorAll('label[for="' + cssEscape(id) + '"]');
        for (var i = 0; i < each.length; i++) labels.push(each[i]);
      } catch (e) { /* an id a selector cannot express */ }
    }
    var node = input.parentElement;
    for (var depth = 0; node && depth < 4; depth++) {
      if ((node.tagName || "").toLowerCase() === "label") { labels.push(node); break; }
      node = node.parentElement;
    }
    var named = (input.getAttribute && input.getAttribute("aria-labelledby")) || "";
    named.split(/\s+/).forEach(function (ref) {
      if (!ref) return;
      try {
        var found = document.getElementById(ref);
        if (found) labels.push(found);
      } catch (e) { /* not a lookup worth failing over */ }
    });
    for (var j = 0; j < labels.length; j++) {
      if (related(el, labels[j])) return true;
    }
    return false;
  }

  function checkableWithin(el) {
    // The input a "is this selected?" check would be about, and how it was found.
    //
    // Rarely the element under the pointer. A radio is a 13px circle and people
    // click the label beside it or the row around it, and in most frameworks -
    // Odoo included - the label is the input's *sibling*, not its parent. So
    // this looks at the element, then inside it, then at what a label points to,
    // and finally at the option it is drawn as part of.
    var type = ((el.getAttribute && el.getAttribute("type")) || "").toLowerCase();
    if ((el.tagName || "").toLowerCase() === "input"
        && (type === "radio" || type === "checkbox")) {
      return { input: el, via: "self" };
    }
    // Inside, but close by. A cell around a checkbox is that checkbox; a panel
    // that happens to hold one somewhere is not, and taking it would be the same
    // mistake as below, one level in.
    var inside = onlyCheckableIn(el);
    var depth = inside ? generations(el, inside, 3) : -1;
    if (depth > 0) return { input: inside, via: "inside", depth: depth };

    if ((el.tagName || "").toLowerCase() === "label") {
      var target = el.getAttribute && el.getAttribute("for");
      if (target) {
        try {
          var referenced = document.getElementById(target);
          if (referenced && referenced.matches && referenced.matches(CHECKABLE)) {
            return { input: referenced, via: "label" };
          }
        } catch (e) { /* an id a selector cannot express */ }
      }
    }
    // The option this is drawn as part of - the label beside the radio, or the
    // wrapper around the pair.
    //
    // "the only checkbox in some ancestor" is not enough on its own, and used to
    // be: every row of an Odoo list holds exactly one, its record selector, so
    // picking any cell of any row came back as that row's checkbox and the cell
    // itself could not even be clicked. So the input has to be one this element
    // labels. Unlabelled ones - a record selector, a toolbar toggle - are then
    // reachable the honest way, by picking the input.
    var node = el.parentElement;
    for (var up = 0; node && up < 3; up++) {
      var sibling = onlyCheckableIn(node);
      if (sibling && labelsIt(el, sibling)) return { input: sibling, via: "near" };
      node = node.parentElement;
    }
    return null;
  }

  function checkableSelector(el) {
    // name alone is shared by every radio in a group, so it needs whichever
    // attribute says which option this one is.
    var base = (el.tagName || "").toLowerCase();
    var type = (el.getAttribute("type") || "").toLowerCase();
    base += '[type="' + type + '"]';
    var which = "";
    var candidates = ["data-value", "value", "id"];
    for (var i = 0; i < candidates.length; i++) {
      var value = el.getAttribute(candidates[i]);
      // The id is the last resort and the only one a render counter can reach:
      // a radio numbered by its position says nothing about which option it is.
      if (value && !UNSTABLE.test(value)
          && !(candidates[i] === "id" && isCounter(value))) {
        which = "[" + candidates[i] + '="' + cssEscape(value) + '"]';
        break;
      }
    }
    var group = el.getAttribute("name");
    var selector = base + (group ? '[name="' + cssEscape(group) + '"]' : "") + which;
    if (matchesUniquely(selector, el)) return selector;
    var scoped = scopeUntilUnique(el, selector);
    return scoped || (selector + nthOfType(el));
  }

  function isEditable(el) {
    var tag = (el.tagName || "").toLowerCase();
    if (tag === "textarea") return true;
    if (tag === "input") {
      var t = (el.getAttribute("type") || "text").toLowerCase();
      return ["button", "submit", "reset", "checkbox", "radio", "file",
              "image", "hidden"].indexOf(t) < 0;
    }
    return !!el.isContentEditable;
  }

  function readValue(el) {
    try { return typeof el.value === "string" ? el.value : ""; } catch (e) { return ""; }
  }

  // ------------------------------------------------------------ action menu
  //
  // What a given element can sensibly be asked to do. A filter over the
  // compiler's own vocabulary rather than a list of its own: Python passes the
  // grammar in, so this cannot drift from what a scenario may contain.
  function actionsFor(target, grammar) {
    var known = {};
    ["selector_only", "selector_and_value", "value_only", "url_target"].forEach(
      function (group) {
        (grammar[group] || []).forEach(function (a) { known[a] = group; });
      });
    var out = [];
    function offer(action, why, selector) {
      if (known[action]) out.push({ action: action, why: why || "",
                                    selector: selector || "" });
    }
    // What the element itself does. Whatever was picked can be clicked as
    // itself; only the input has nothing to add, because "select it" below is
    // already that click. For a cell, a label or a row the two are different
    // steps and both are wanted - clicking the row that holds a record selector
    // is not ticking the record selector.
    function itself() {
      if (target.tag === "select") {
        offer("select", "choose an option by value");
      } else if (target.editable) {
        offer("fill", "type into it");
        offer("click", "focus it");
      } else if (target.checkableVia !== "self") {
        offer("click", target.checkable ? "click the " + target.tag + " itself"
                                        : "click it");
      }
    }
    // Whether the control is what this element is, or something it merely
    // contains. A cell drawn around a checkbox is that checkbox and leads with
    // it; a row two levels out is a row, and leads with itself.
    var incidental = target.checkableVia === "inside" && target.checkableDepth > 1;
    if (incidental) itself();
    // A radio or a checkbox has a question of its own that none of the ordinary
    // assertions answer: is it the one that is on? CSS says it exactly, so this
    // needs no new step type - :checked on the selector, and assert_exists.
    if (target.checkable) {
      offer("assert_exists", "check it IS selected", target.checkable + ":checked");
      offer("assert_exists", "check it is NOT selected",
            target.checkable + ":not(:checked)");
      offer("wait_for", "wait until it becomes selected",
            target.checkable + ":checked");
      offer("click", "select it", target.checkable);
    }
    if (!incidental) itself();
    offer("assert_visible", "check it is on screen");
    offer("assert_not_visible", "check it is gone");
    offer("assert_exists", "check it is in the DOM");
    offer("assert_text_contains", "check what it says");
    offer("wait_for", "wait until it appears");
    return out;
  }

  var Recorder = {
    _shadow: null, _card: null, _body: null, _titleEl: null, _hi: null,
    _armBtn: null, _hintEl: null, _menu: null,
    _armed: false, _running: false, _collapsed: false, _busy: false,
    _queue: [],            // events waiting for Python to drain
    _state: { steps: [], scenario: "", status: "idle" },
    _selectors: {}, _grammar: {},
    _drag: null,
    _hover: null,
    // The element the open menu is about, and the ones widening walked up from.
    // Kept as elements rather than descriptions because widening re-describes:
    // what the menu offers is a property of the element, not of the pick.
    _pickedEl: null,
    _narrower: [],
    // ":nth-match(...)" for the element the menu is about, and whether the step
    // is being written that way. null when there is only one of it.
    _nth: null, _byNth: false,

    // Exposed for the tests: selector synthesis is the one piece here with
    // enough logic to be worth checking without a browser.
    _describe: function (el) {
      return describeTarget(el, this._selectors);
    },

    _actionsFor: function (target) {
      return actionsFor(target, this._grammar);
    },

    _nthFor: function (el) {
      return nthSelector(el);
    },

    _around: function (el) {
      return around(el);
    },

    // ------------------------------------------------------------- lifecycle
    configure: function (selectors, grammar) {
      this._selectors = selectors || {};
      this._grammar = grammar || {};
    },

    start: function () {
      this._running = true;
      this._bindKeys();
      this.ensure();
      this._push({ kind: "started", url: location.href });
      this.paint();
    },

    stop: function () {
      this._running = false;
      this._unbindKeys();
      this.disarm();
      var host = document.getElementById(HOST_ID);
      if (host && host.parentNode) host.parentNode.removeChild(host);
      this._shadow = null;
    },

    running: function () { return !!this._running; },
    armed: function () { return !!this._armed; },

    // --------------------------------------------------------------- python
    drain: function () {
      var out = this._queue;
      this._queue = [];
      return out;
    },

    pending: function () { return this._queue.length; },

    _push: function (event) {
      this._queue.push(event);
      // A page can be navigated away from at any moment and the queue goes with
      // it, so keep it small and let Python be the memory.
      if (this._queue.length > 200) this._queue.shift();
    },

    render: function (state) {
      try {
        this._state = state || this._state;
        // Python repaints only after the step has been performed and written
        // down, so this is the acknowledgement the hold was waiting for.
        this._setBusy(false);
        if (this._running) { this.ensure(); this.paint(); }
      } catch (e) { /* never throw into the page */ }
    },

    // ------------------------------------------------------------------ DOM
    ensure: function () {
      var host = document.getElementById(HOST_ID);
      if (host && this._shadow) return;
      if (!host) {
        host = elem("div");
        host.id = HOST_ID;
        host.setAttribute("aria-hidden", "true");
        (document.documentElement || document.body).appendChild(host);
      }
      var shadow = host.shadowRoot || host.attachShadow({ mode: "open" });
      shadow.innerHTML = "";
      var style = document.createElement("style");
      style.textContent = CSS;
      shadow.appendChild(style);

      var wrap = elem("div", "wrap");
      var card = elem("div", "card");

      var header = elem("div", "header");
      var title = elem("div", "h-title");
      title.textContent = "Recording";
      header.appendChild(title);
      this._collapseBtn = this._makeBtn("–", "Collapse", this._toggleMinimize);
      header.appendChild(this._collapseBtn);
      card.appendChild(header);

      var bar = elem("div", "bar");
      var arm = elem("div", "action");
      arm.textContent = "Capture Step";
      var self = this;
      arm.addEventListener("click", function (e) {
        e.stopPropagation();
        self.toggleArm();
      });
      bar.appendChild(arm);
      var finish = elem("div", "ghost-btn");
      finish.textContent = "Finish";
      finish.title = "Stop recording and save what has been captured";
      finish.addEventListener("click", function (e) {
        e.stopPropagation();
        self._push({ kind: "finish" });
      });
      bar.appendChild(finish);
      card.appendChild(bar);

      var hint = elem("div", "hint");
      card.appendChild(hint);

      var body = elem("div", "body");
      card.appendChild(body);
      wrap.appendChild(card);
      shadow.appendChild(wrap);

      this._shadow = shadow;
      this._card = card;
      this._body = body;
      this._titleEl = title;
      this._armBtn = arm;
      this._hintEl = hint;
      this._wireDrag(header);
      // A navigation rebuilds all of this, so the choice is read back rather
      // than remembered in the object that was just replaced.
      this._collapsed = this._readCollapsed();
      this._applyCollapsed();
    },

    _makeBtn: function (glyph, tip, handler) {
      var b = elem("div", "btn");
      b.textContent = glyph;
      b.title = tip;
      var self = this;
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        handler.call(self, b);
      });
      return b;
    },

    _toggleMinimize: function () {
      this._collapsed = !this._collapsed;
      // Remembered outside this object, because a navigation replaces it: the
      // init script runs again on the new document and everything here starts
      // over. sessionStorage is scoped to this tab and this origin, which is
      // exactly the life of a recording (the auto-login extension leans on it
      // for the same reason).
      try {
        sessionStorage.setItem(COLLAPSED_KEY, this._collapsed ? "1" : "");
      } catch (e) { /* storage denied: it just will not be remembered */ }
      this._applyCollapsed();
    },

    _readCollapsed: function () {
      try {
        return sessionStorage.getItem(COLLAPSED_KEY) === "1";
      } catch (e) { return false; }
    },

    _applyCollapsed: function () {
      if (!this._card) return;
      this._card.className = this._collapsed ? "card collapsed" : "card";
      if (this._collapseBtn) {
        this._collapseBtn.textContent = this._collapsed ? "+" : "–";
        this._collapseBtn.title = this._collapsed ? "Expand" : "Collapse";
      }
    },

    _wireDrag: function (handle) {
      var self = this;
      handle.addEventListener("mousedown", function (e) {
        if (e.button !== 0) return;
        var wrap = self._card.parentNode;
        var rect = wrap.getBoundingClientRect();
        self._drag = { dx: e.clientX - rect.left, dy: e.clientY - rect.top };
        e.preventDefault();
      });
      document.addEventListener("mousemove", function (e) {
        if (!self._drag) return;
        var wrap = self._card.parentNode;
        wrap.style.left = (e.clientX - self._drag.dx) + "px";
        wrap.style.top = (e.clientY - self._drag.dy) + "px";
        wrap.style.right = "auto";
      });
      document.addEventListener("mouseup", function () { self._drag = null; });
    },

    paint: function () {
      if (!this._shadow) return;
      var state = this._state || {};
      this._titleEl.textContent = "Recording" +
        (state.scenario ? " · " + state.scenario : "");
      this._armBtn.textContent = this._armed ? "Pick an element… (Esc)"
                                             : "Capture Step  (F2)";
      this._armBtn.className = this._armed ? "action armed" : "action";
      this._hintEl.textContent = this._busy
        ? "Performing the step…"
        : (this._armed
           ? "Hover to outline, click to pick. Menus stay open while capturing."
           : "Nothing is recorded until you press Capture Step. Press F2 instead "
             + "to keep a dropdown open.");

      var steps = state.steps || [];
      this._body.innerHTML = "";
      if (!steps.length) {
        var empty = elem("div", "empty");
        empty.textContent = "No steps yet.";
        this._body.appendChild(empty);
        return;
      }
      for (var i = 0; i < steps.length; i++) {
        this._body.appendChild(this._stepRow(steps[i], i, steps.length));
      }
      this._body.scrollTop = this._body.scrollHeight;
    },

    // A step, and the four things worth doing to one without leaving the page.
    //
    // Fixing a bad capture belongs here rather than only on the Scenarios page:
    // the moment you can still see what you meant to point at is the moment the
    // mistake is obvious. Python owns the list, so these send an intent and
    // repaint from what comes back - the row indices are always its indices.
    _stepRow: function (step, index, total) {
      var self = this;
      var row = elem("div", "step");
      var n = elem("div", "step-n");
      n.textContent = String(index + 1);
      var a = elem("div", "step-a");
      a.textContent = step.action || "";
      var t = elem("div", "step-t");
      t.textContent = step.target || "";
      if (step.value) t.textContent += ' = "' + step.value + '"';

      var tools = elem("div", "step-tools");
      if (index > 0) {
        tools.appendChild(this._stepBtn("↑", "Move up", function () {
          self._push({ kind: "move", index: index, delta: -1 });
        }));
      }
      if (index < total - 1) {
        tools.appendChild(this._stepBtn("↓", "Move down", function () {
          self._push({ kind: "move", index: index, delta: 1 });
        }));
      }
      if (!step.doc) {
        tools.appendChild(this._stepBtn("¶", "Add a doc for the guide", function () {
          self._editDoc(step, index);
        }));
      }
      tools.appendChild(this._stepBtn("✎", "Edit this step", function () {
        self._editStep(step, index);
      }));
      tools.appendChild(this._stepBtn("✕", "Delete this step", function () {
        self._push({ kind: "delete", index: index });
      }));

      row.appendChild(n);
      row.appendChild(a);
      row.appendChild(t);
      row.appendChild(tools);
      if (step.doc) row.appendChild(this._docLine(step, index));
      return row;
    },

    // The sentence a run's guide shows for the step (engine/livedoc.py). Python
    // keeps it, like everything else about a step: these marks only ask.
    _docLine: function (step, index) {
      var self = this;
      var line = elem("div", "step-doc");
      var text = elem("div", "step-doc-t");
      text.textContent = step.doc;
      var tools = elem("div", "step-tools");
      tools.appendChild(this._stepBtn("✎", "Edit the doc", function () {
        self._editDoc(step, index);
      }));
      tools.appendChild(this._stepBtn("✕", "Delete the doc", function () {
        self._push({ kind: "doc", index: index, doc: "" });
      }));
      line.appendChild(text);
      line.appendChild(tools);
      return line;
    },

    _editDoc: function (step, index) {
      var self = this;
      var box = this._editor("step " + (index + 1) + " · doc");
      var doc = this._field(box, "Doc - what the guide says for this step", step.doc || "");
      this._editorFoot(box, "Enter saves · empty deletes · Esc cancels");
      this._editorKeys([doc], function () {
        self._push({ kind: "doc", index: index, doc: doc.value });
      });
      doc.focus();
      doc.select();
    },

    // A box beside the panel for changing one step: the step editor's and the
    // doc editor's. Beside the panel rather than at the pointer: the row being
    // edited is over there, and the pointer is wherever it happens to be.
    _editor: function (title) {
      this._closeMenu();
      var box = elem("div", "menu");
      var head = elem("div", "menu-head");
      head.textContent = title;
      box.appendChild(head);
      return box;
    },

    _editorFoot: function (box, text) {
      var foot = elem("div", "menu-foot");
      foot.textContent = text;
      box.appendChild(foot);
      this._shadow.appendChild(box);
      this._menu = box;
      this._menuAt = { x: Math.max(8, window.innerWidth - 780), y: 90 };
      this._placeMenu(box);
    },

    // The inputs own the keyboard while the box is open - text, digits, a
    // Ukrainian letter - and only Enter and Esc mean anything else.
    _editorKeys: function (inputs, commit) {
      var self = this;
      this._valueInput = inputs[0];
      inputs.forEach(function (input) {
        input.addEventListener("keydown", function (e) {
          e.stopPropagation();
          if (e.key === "Enter") {
            e.preventDefault();
            self._closeMenu();
            commit();
          } else if (e.key === "Escape") {
            e.preventDefault();
            self._closeMenu();
          }
        });
      });
    },

    _stepBtn: function (glyph, tip, handler) {
      var b = elem("div", "step-btn");
      b.textContent = glyph;
      b.title = tip;
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        handler();
      });
      return b;
    },

    _editStep: function (step, index) {
      var self = this;
      var box = this._editor("step " + (index + 1) + " · " + (step.action || ""));
      var target = this._field(box, "Target", step.target || "");
      var value = this._field(box, "Value", step.value || "");
      var doc = this._field(box, "Doc", step.doc || "");
      this._editorFoot(box, "Enter saves · Esc cancels");
      this._editorKeys([target, value, doc], function () {
        self._push({ kind: "edit", index: index, target: target.value,
                     value: value.value, doc: doc.value });
      });
      target.focus();
      target.select();
    },

    _field: function (box, label, initial) {
      var row = elem("div", "menu-value");
      var caption = elem("div", "menu-label");
      caption.textContent = label;
      var input = elem("input");
      input.type = "text";
      input.value = initial;
      row.appendChild(caption);
      row.appendChild(input);
      box.appendChild(row);
      return input;
    },

    // ------------------------------------------------------------- capturing
    toggleArm: function () {
      if (this._armed) this.disarm(); else this.arm();
    },

    arm: function () {
      if (this._armed) return;
      this._armed = true;
      this._bind();
      this.paint();
    },

    disarm: function () {
      this._armed = false;
      this._unbind();
      this._clearHighlight();
      // Forget what was under the pointer, or the next arming skips the first
      // highlight: the pointer has not moved, so _track sees no change.
      this._hover = null;
      this._closeMenu();
      this.paint();
    },

    // Keys work while the recorder is running, not only while it is armed - the
    // whole point being that arming must not require a click. Clicking anywhere,
    // including on this panel, dismisses whatever transient thing the app has
    // open: a dropdown, a popover, an autocomplete list. Those are exactly the
    // things worth recording, so F2 arms instead.
    _bindKeys: function () {
      if (this._onKey) return;
      var self = this;
      this._onKey = function (e) { self._key(e); };
      document.addEventListener("keydown", this._onKey, true);
    },

    _unbindKeys: function () {
      if (this._onKey) document.removeEventListener("keydown", this._onKey, true);
      this._onKey = null;
    },

    _key: function (e) {
      if (e.key === "F2") {
        this.toggleArm();
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      if (e.key === "Escape" && (this._armed || this._menu)) {
        this.disarm();
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      // While a value is being typed the input owns the keyboard: digits are
      // text there, not menu shortcuts. Its own handler deals with Enter/Esc.
      if (this._valueInput) return;
      // With the action menu open, choosing by keyboard is the only way that
      // leaves the app's own popup standing - a click on this panel is an
      // outside-click as far as the app is concerned.
      if (!this._menu) return;
      // The letters change what the step is about; the digits and arrows below
      // choose what it does.
      var scoping = { t: this.toggleByText, n: this.toggleByNth,
                      p: this.widen, c: this.narrow };
      var letter = scoping[String(e.key).toLowerCase()];
      if (letter) {
        letter.call(this);
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      var items = this._menuItems || [];
      if (/^[1-9]$/.test(e.key)) {
        var index = parseInt(e.key, 10) - 1;
        if (items[index]) {
          e.preventDefault();
          e.stopPropagation();
          this._choose(items[index].action, this._pickedTarget,
                       items[index].selector);
        }
        return;
      }
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        this._moveMenu(e.key === "ArrowDown" ? 1 : -1);
        e.preventDefault();
        e.stopPropagation();
      } else if (e.key === "Enter" && items[this._menuIndex]) {
        e.preventDefault();
        e.stopPropagation();
        this._choose(items[this._menuIndex].action, this._pickedTarget,
                     items[this._menuIndex].selector);
      }
    },

    _bind: function () {
      var self = this;
      this._onMove = function (e) { self._track(e); };
      // Capture phase, so the pick is taken before the app sees it. All three
      // matter: an app closes its dropdown on mousedown, so intercepting only
      // the click would let the menu shut before the element could be picked
      // out of it.
      this._onClick = function (e) { self._pick(e); };
      this._onDown = function (e) { self._swallow(e); };
      document.addEventListener("mousemove", this._onMove, true);
      document.addEventListener("click", this._onClick, true);
      document.addEventListener("mousedown", this._onDown, true);
      document.addEventListener("pointerdown", this._onDown, true);
      document.addEventListener("mouseup", this._onDown, true);
    },

    _unbind: function () {
      if (this._onMove) document.removeEventListener("mousemove", this._onMove, true);
      if (this._onClick) document.removeEventListener("click", this._onClick, true);
      if (this._onDown) {
        document.removeEventListener("mousedown", this._onDown, true);
        document.removeEventListener("pointerdown", this._onDown, true);
        document.removeEventListener("mouseup", this._onDown, true);
      }
      this._onMove = this._onClick = this._onDown = null;
    },

    _swallow: function (e) {
      // Everything except the click itself, which _pick handles. Taking these
      // away from the app is what keeps a dropdown open long enough to point at
      // something inside it.
      if (this._ours(e)) return;
      e.preventDefault();
      e.stopPropagation();
    },

    _inPanel: function (el) {
      var host = document.getElementById(HOST_ID);
      return !!(host && (el === host || host.contains(el)));
    },

    // Anything the panel receives belongs to the panel. Events from inside a
    // shadow tree are retargeted to the host on the way out, so a click on a
    // menu item arrives here as the host - which is exactly what has to be let
    // through, or arming would swallow the very menu it just opened.
    //
    // Nothing needs to see *through* the panel: .wrap is pointer-events:none and
    // the highlight box is too, so only the card itself is ever a target.
    _ours: function (e) {
      var el = e.target;
      return !el || this._inPanel(el) || el.id === HOST_ID;
    },

    _track: function (e) {
      // With a menu open the highlight belongs to it: it shows the element the
      // menu is about, which is the only way to see what P and C just did.
      if (this._menu) return;
      if (this._ours(e)) { this._clearHighlight(); return; }
      var el = e.target;
      if (el === this._hover) return;
      this._hover = el;
      this._highlight(el);
    },

    _highlight: function (el) {
      if (!this._shadow) return;
      this._clearHighlight();
      var rect = el.getBoundingClientRect();
      if (!rect || (!rect.width && !rect.height)) return;
      var box = elem("div", "hi");
      box.style.left = rect.left + "px";
      box.style.top = rect.top + "px";
      box.style.width = rect.width + "px";
      box.style.height = rect.height + "px";
      var tag = elem("div", "hi-tag");
      var described = describeTarget(el, this._selectors);
      tag.textContent = described.name || described.selector;
      if (rect.top < 22) tag.style.top = "0";
      box.appendChild(tag);
      this._shadow.appendChild(box);
      this._hi = box;
    },

    _clearHighlight: function () {
      if (this._hi && this._hi.parentNode) this._hi.parentNode.removeChild(this._hi);
      this._hi = null;
    },

    _pick: function (e) {
      if (this._ours(e)) return;              // the panel's own clicks are its own
      // Take the click away from the app: while armed, clicking chooses an
      // element rather than pressing what is under the pointer.
      e.preventDefault();
      e.stopPropagation();
      this._openMenu(e.clientX, e.clientY, e.target);
    },

    // ------------------------------------------------------------- the menu
    // A selector that finds this element by the text it holds.
    //
    // Normally the worst thing a recording can do: the same app renders
    // Ukrainian on one environment and English on another, so a step keyed on a
    // label breaks the moment it moves. Data is the exception - a customer name,
    // a reference, an amount - because that is the same wherever the app runs,
    // and picking one row out of a list is otherwise impossible. Offered, never
    // chosen automatically, and Playwright's :text-is() is an exact match.
    _textSelector: function (target) {
      var text = (target.text || "").trim();
      if (!text) return target.selector;
      return target.selector + ':text-is("' + text.replace(/"/g, '\\"') + '")';
    },

    _openMenu: function (x, y, el) {
      this._closeMenu();
      this._highlight(el);
      this._menuAt = { x: x, y: y };
      this._byText = false;
      this._byNth = false;
      this._pickedEl = el;
      this._narrower = [];
      this._renderMenu();
    },

    // Out to the element around this one, and back in again.
    //
    // A pick is whatever was under the pointer, and in a table that is always a
    // cell: cells fill the row, so no point exists where the pointer is on the
    // <tr> itself. Without a way to walk outwards, a step about a whole row
    // cannot be recorded at all - only hand-written afterwards.
    widen: function () {
      if (!this._menu || !this._pickedEl) return;
      var parent = around(this._pickedEl);
      if (!parent || this._inPanel(parent)) return;
      this._narrower.push(this._pickedEl);
      this._pickedEl = parent;
      this._rescope();
    },

    narrow: function () {
      if (!this._menu || !this._narrower.length) return;
      this._pickedEl = this._narrower.pop();
      this._rescope();
    },

    _rescope: function () {
      // The text of a container is every label inside it, which is nobody's idea
      // of a selector, so widening drops back to matching on what it is.
      this._byText = false;
      this._highlight(this._pickedEl);
      this._renderMenu();
    },

    toggleByNth: function () {
      if (!this._menu || !this._nth) return;
      this._byNth = !this._byNth;
      this._renderMenu();
    },

    // What the step will actually say, in whichever of the three ways the menu
    // is currently set to.
    _selectorFor: function (target) {
      if (this._byText) return this._textSelector(target);
      if (this._byNth && this._nth) return this._nth.selector;
      return target.selector;
    },

    _renderMenu: function () {
      var wasOpen = !!this._menu;
      if (wasOpen && this._menu.parentNode) this._menu.parentNode.removeChild(this._menu);
      var target = describeTarget(this._pickedEl, this._selectors);
      this._pickedTarget = target;
      this._nth = nthSelector(this._pickedEl);
      if (!this._nth) this._byNth = false;    // nothing to count after a widen
      var byNth = this._byNth && !!this._nth;
      var menu = elem("div", "menu");

      var head = elem("div", "menu-head");
      head.textContent = target.tag + (target.text ? ' "' + target.text + '"' : "");
      var sel = elem("span", "menu-sel");
      sel.textContent = (this._byText || byNth)
        ? this._selectorFor(target)
        : (target.name ? target.name + "  (selectors.yaml)" : target.selector);
      head.appendChild(sel);
      this._menuSelEl = sel;
      this._menuBaseSel = sel.textContent;
      if (!this._byText && !byNth && !target.unique) {
        // Loud, because a step that matches several elements acts on whichever
        // Playwright reaches first - which is not a thing to discover later.
        var narrows = [];
        if ((target.text || "").trim()) narrows.push("T narrows it by text");
        if (this._nth) narrows.push((narrows.length ? "N" : "N narrows it") + " by count");
        var warn = elem("div", "menu-warn");
        warn.textContent = "matches more than one element"
          + (narrows.length ? " - " + narrows.join(", ") : "");
        head.appendChild(warn);
        this._menuWarnEl = warn;
      }
      menu.appendChild(head);

      var self = this;
      var choices = actionsFor(target, this._grammar);
      this._menuItems = choices;
      this._menuIndex = 0;
      this._pickedTarget = target;
      this._menuEls = [];
      choices.forEach(function (choice, index) {
        var item = elem("div", "menu-item");
        var key = elem("span", "key");
        key.textContent = index < 9 ? String(index + 1) : " ";
        var label = elem("span");
        label.textContent = choice.action;
        var why = elem("span", "why");
        why.textContent = choice.why;
        item.appendChild(key);
        item.appendChild(label);
        item.appendChild(why);
        item.addEventListener("click", function (ev) {
          ev.stopPropagation();
          self._choose(choice.action, target, choice.selector);
        });
        menu.appendChild(item);
        self._menuEls.push(item);
      });

      // Everything below the actions changes what the step is about rather than
      // what it does, so none of it joins _menuEls: 1-9 and the arrows stay on
      // the actions, and these keep their own letters.
      var outer = around(this._pickedEl);
      if (outer && !this._inPanel(outer)) {
        this._toggleRow(menu, "P",
                        "select the " + (outer.tagName || "").toLowerCase()
                          + " around it",
                        "the parent", this.widen);
      }
      if (this._narrower.length) {
        var back = this._narrower[this._narrower.length - 1];
        this._toggleRow(menu, "C",
                        "back to the " + (back.tagName || "").toLowerCase(),
                        "the child again", this.narrow);
      }
      if (this._nth) {
        this._toggleRow(menu, "N",
                        byNth ? "match by what it is instead" : "match by its position",
                        byNth ? "back to the structural selector"
                              : "the " + ordinal(this._nth.index) + " of "
                                + this._nth.total,
                        this.toggleByNth);
      }
      if ((target.text || "").trim()) {
        this._toggleRow(menu, "T",
                        this._byText ? "match by position instead"
                                     : "match by its exact text",
                        this._byText ? "back to the structural selector"
                                     : "for a data value, not a label",
                        this.toggleByText);
      }

      var cancel = elem("div", "menu-item menu-cancel");
      cancel.textContent = "Cancel";
      cancel.addEventListener("click", function (ev) {
        ev.stopPropagation();
        self.disarm();
      });
      menu.appendChild(cancel);

      var foot = elem("div", "menu-foot");
      foot.textContent = "1-9 or ↑↓ + Enter · P parent · N count · T text · Esc cancels";
      menu.appendChild(foot);

      this._shadow.appendChild(menu);
      this._menu = menu;
      this._placeMenu(menu);
      this._moveMenu(0);
    },

    // One row of the menu that changes what the step is about. Same shape as an
    // action row and deliberately not one: it re-renders instead of recording.
    _toggleRow: function (menu, key, label, why, fn) {
      var self = this;
      var row = elem("div", "menu-item menu-toggle");
      var k = elem("span", "key"); k.textContent = key;
      var l = elem("span"); l.textContent = label;
      var w = elem("span", "why"); w.textContent = why;
      row.appendChild(k);
      row.appendChild(l);
      row.appendChild(w);
      row.addEventListener("click", function (ev) {
        ev.stopPropagation();
        fn.call(self);
      });
      menu.appendChild(row);
    },

    toggleByText: function () {
      if (!this._menu || !this._pickedEl) return;
      this._byText = !this._byText;
      this._renderMenu();
    },

    // Put the menu where it fits, which is not always where it was asked for.
    //
    // It opens at the pointer, and an element near the bottom of the window puts
    // the pointer there too - so the menu ran off the fold and the last actions
    // could not be read or reached. Measuring first is the only way: how tall it
    // is depends on what the element offers, and a radio offers four more rows
    // than a div does.
    _placeMenu: function (box) {
      var margin = 8;
      var room = window.innerHeight - margin * 2;
      box.style.maxHeight = room + "px";      // taller than the window: scroll
      var rect = box.getBoundingClientRect();
      var x = this._menuAt.x;
      var y = this._menuAt.y;

      if (y + rect.height > window.innerHeight - margin) {
        // Above the pointer reads better than pinned to the edge, when it fits.
        var above = y - rect.height;
        y = above >= margin
          ? above
          : Math.max(margin, window.innerHeight - rect.height - margin);
      }
      if (x + rect.width > window.innerWidth - margin) {
        x = Math.max(margin, window.innerWidth - rect.width - margin);
      }
      box.style.left = Math.max(margin, x) + "px";
      box.style.top = Math.max(margin, y) + "px";
    },

    _moveMenu: function (delta) {
      var items = this._menuEls || [];
      if (!items.length) return;
      this._menuIndex = (this._menuIndex + delta + items.length) % items.length;
      for (var i = 0; i < items.length; i++) {
        items[i].className = "menu-item" + (i === this._menuIndex ? " on" : "");
      }
      var chosen = items[this._menuIndex];
      if (chosen && chosen.scrollIntoView) chosen.scrollIntoView({ block: "nearest" });

      // The head follows the highlighted entry, because an entry can carry a
      // selector of its own - a radio's :checked form is about the input, not
      // about the label that was clicked - and showing the picked element's
      // instead means the line above the list is not what gets recorded.
      var entry = (this._menuItems || [])[this._menuIndex];
      if (this._menuSelEl) {
        this._menuSelEl.textContent = (entry && entry.selector)
          || this._menuBaseSel;
      }
      if (this._menuWarnEl) {
        // An entry with its own selector has already narrowed it.
        this._menuWarnEl.style.display = (entry && entry.selector) ? "none" : "";
      }
    },

    _closeMenu: function () {
      if (this._menu && this._menu.parentNode) this._menu.parentNode.removeChild(this._menu);
      this._menu = null;
      this._menuItems = null;
      this._menuEls = null;
      this._menuSelEl = null;
      this._menuWarnEl = null;
      this._valueInput = null;
      this._menuIndex = 0;
      this._pickedEl = null;
      this._narrower = [];
      this._nth = null;
      this._byNth = false;
    },

    //: Actions that need a value the element cannot answer for itself.
    _NEEDS_VALUE: { fill: "Text to type", select: "Option value to select",
                    assert_text_contains: "Text it should contain" },

    _choose: function (action, target, override) {
      if (this._NEEDS_VALUE[action]) {
        this._askValue(action, target, override);
        return;
      }
      this._commit(action, target, undefined, override);
    },

    // Asking happens in this panel, never with window.prompt.
    //
    // The recorder is driven over CDP, and Playwright dismisses a page's dialogs
    // by default - so prompt() returns null, the step is silently dropped, and
    // `fill` looks like it simply does not work. Which is exactly what it did.
    _askValue: function (action, target, override) {
      var self = this;
      var suggested = action === "assert_text_contains"
        ? (target.text || "") : (target.value || "");
      this._closeMenu();
      var box = elem("div", "menu");

      var head = elem("div", "menu-head");
      head.textContent = action + " · " + this._NEEDS_VALUE[action];
      var sel = elem("span", "menu-sel");
      sel.textContent = override || this._targetFor(target);
      head.appendChild(sel);
      box.appendChild(head);

      var row = elem("div", "menu-value");
      var input = elem("input");
      input.type = "text";
      input.value = suggested;
      row.appendChild(input);
      box.appendChild(row);

      var foot = elem("div", "menu-foot");
      foot.textContent = "Enter saves the step · Esc cancels";
      box.appendChild(foot);

      this._shadow.appendChild(box);
      this._menu = box;
      this._placeMenu(box);
      this._valueInput = input;
      input.addEventListener("keydown", function (e) {
        // Handled here rather than in the global listener: while a value is
        // being typed, digits are text, not menu shortcuts.
        e.stopPropagation();
        if (e.key === "Enter") {
          e.preventDefault();
          self._commit(action, target, input.value, override);
        } else if (e.key === "Escape") {
          e.preventDefault();
          self.disarm();
        }
      });
      // The page still has focus until something takes it; without this the
      // first keystroke goes to the app.
      input.focus();
      input.select();
    },

    _targetFor: function (target) {
      // A name the tree already has, so the recording reads like the flows
      // beside it - unless this step is being matched on a data value or a
      // position, which no name can stand for.
      if (this._byText || (this._byNth && this._nth)) return this._selectorFor(target);
      return target.name || target.selector;
    },

    _commit: function (action, target, value, override) {
      // An override is a selector the chosen entry built for itself - a radio's
      // :checked form, say - and it is both what gets recorded and what gets
      // acted on, because it describes the thing the entry is about.
      var step = {
        action: action,
        target: override || this._targetFor(target),
        // What Python performs against: the exact element that was picked.
        selector: override || this._selectorFor(target),
        named: !!target.name && !this._byText && !(this._byNth && this._nth) && !override
      };
      if (value !== undefined) step.value = value;
      // Disarm first: performing the step may navigate, and an armed recorder
      // would then eat the first click on the next page.
      this.disarm();
      // Say that the step is in flight. It is performed a poll later, by
      // Playwright, and there is deliberately no attempt to hold the page shut
      // in the meantime: the recorder cannot tell its own click from a person's
      // - both are trusted input - so blocking one blocks the other, and the
      // step never lands. Saying so is the honest half of that.
      this._setBusy(true);
      this._push({ kind: "step", step: step });
    },

    _setBusy: function (busy) {
      if (busy === this._busy) return;
      this._busy = busy;
      if (busy) {
        // Cleared by Python's next repaint; this only covers the case where it
        // never comes, so the panel does not sit saying "performing" for ever.
        var self = this;
        this._busyTimer = setTimeout(function () { self._setBusy(false); }, 5000);
      } else if (this._busyTimer) {
        clearTimeout(this._busyTimer);
        this._busyTimer = null;
      }
      this.paint();
    }
  };

  window.__Recorder = Recorder;
})();
