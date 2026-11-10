// Execution Overlay (HUD) renderer - injected into the automated Chrome window.
//
// This runs INSIDE the page (over the existing CDP/Playwright connection) but is
// fully isolated: a single host element attached to <html>, its content in a
// Shadow DOM so page CSS/JS never sees it and it never touches the app's DOM.
//
// Interaction model: the empty space around the card is pointer-events:none
// (clicks pass through to the page), but the card itself is interactive so you
// can SCROLL a long tree, COLLAPSE branches, DRAG the HUD, and MINIMIZE it.
// Because an interactive card can sit over an element the automation needs to
// click, the header has a "pass-through" toggle (▣/▢) that turns the whole card
// click-through again - and dragging/minimizing gets it out of the way too.
// And while the automation itself clicks (state.acting), the whole HUD lets the
// mouse through on its own: a card over the target swallowed the click, and the
// step failed for a reason that had nothing to do with the page.
// When every step is photographed (state.collapsed) it starts folded down to its
// header, whose background fills left to right with the progress: the pictures
// keep a strip, not a card over the page - and nothing is hidden for the capture,
// which would make the HUD blink in the window. Folding or unfolding it by hand
// wins over that default.
//
// State lives in Python (engine/overlay.py); this is a pure renderer. It exposes
// window.__ExecHud.render(state) / .remove(). render() is a diff: it rebuilds the
// tree DOM only when state.treeVersion changes (once per scenario), and otherwise
// only updates node classes / text - so 1000-node trees stay cheap.
//
// Stepping (engine/stepping.py), when somebody can pause the run: a bar under
// the header - pause / continue, one step, back, forward - and its keys, a dot
// in front of each step to stop before it, a click on a step taken to look at
// the page it left. What is asked goes into a queue Python takes from
// (takeCommands); nothing here changes the run by itself. Looking at an earlier
// step lays a copy of that page over the live one (showCopy): the live page is
// left exactly as the run left it, underneath.
(function () {
  "use strict";
  var HOST_ID = "__exec_hud_host__";

  var ICON = { pending: "○", running: "▶", success: "✔",
               failed: "✖", skipped: "⊘" };
  // pending gray / running blue / success green / failed red / skipped orange
  var COLOR = { pending: "#9aa0a6", running: "#4f9dff", success: "#3fbf5f",
                failed: "#ff5c5c", skipped: "#ffab40" };

  var CSS = "" +
    ":host{all:initial}" +
    // Step marker: a fixed box drawn OVER the element a step acted on. Never
    // interactive (pointer-events:none) so it cannot swallow a later click, and
    // it fades itself out so screenshots taken after the flash are clean.
    ".mark{position:fixed;pointer-events:none;z-index:2147483646;box-sizing:border-box;" +
      "border:2px solid #4da3ff;border-radius:3px;background:rgba(77,163,255,0.16);" +
      "box-shadow:0 0 0 2px rgba(77,163,255,0.35),0 0 12px rgba(77,163,255,0.55);" +
      "animation:__hud_mark 1.4s ease-out forwards}" +
    "@keyframes __hud_mark{0%{opacity:0;transform:scale(1.06)}" +
      "12%{opacity:1;transform:scale(1)}70%{opacity:1}100%{opacity:0}}" +
    ".mark-tag{position:absolute;top:-19px;left:-2px;padding:1px 6px;border-radius:3px;" +
      "background:#4da3ff;color:#0b1220;white-space:nowrap;" +
      "font:600 11px/1.5 'Segoe UI',system-ui,-apple-system,sans-serif}" +
    ".wrap{position:fixed;top:14px;right:14px;width:360px;max-height:92vh;" +
      "display:flex;flex-direction:column;pointer-events:none;" +
      "font:12px/1.45 'Segoe UI',system-ui,-apple-system,sans-serif;color:#e8eaed;" +
      "z-index:2147483647}" +
    // the interactive card
    ".card{display:flex;flex-direction:column;min-height:0;max-height:92vh;" +
      "background:rgba(18,19,22,0.90);border:1px solid rgba(255,255,255,0.10);" +
      "border-radius:12px;box-shadow:0 10px 34px rgba(0,0,0,0.5);" +
      "backdrop-filter:blur(7px);-webkit-backdrop-filter:blur(7px);" +
      "pointer-events:auto;overflow:hidden}" +
    // pass-through mode: body lets the mouse reach the page, header stays live
    // (so you can always toggle it back / drag / restore)
    ".card.ghost{pointer-events:none;opacity:.85}" +
    ".card.ghost .header{pointer-events:auto}" +
    // automation clicking: nothing in the HUD may take the click, header included.
    // Every element, because a child's pointer-events:auto overrides its parent's none.
    ".wrap.acting,.wrap.acting *{pointer-events:none !important}" +
    // header / drag handle
    ".header{display:flex;align-items:center;gap:8px;padding:8px 10px;cursor:move;" +
      "user-select:none;border-bottom:1px solid rgba(255,255,255,0.08);" +
      "background:rgba(255,255,255,0.04);flex:0 0 auto}" +
    ".h-title{flex:1;font-weight:600;color:#fff;white-space:nowrap;overflow:hidden;" +
      "text-overflow:ellipsis}" +
    ".btn{cursor:pointer;width:22px;height:22px;flex:0 0 auto;display:flex;" +
      "align-items:center;justify-content:center;border-radius:6px;color:#c8ccd2;" +
      "font-size:13px;line-height:1}" +
    ".btn:hover{background:rgba(255,255,255,0.12);color:#fff}" +
    // scrollable body
    ".body{display:flex;flex-direction:column;gap:10px;padding:10px;overflow:auto;" +
      "min-height:0}" +
    ".card.min .body{display:none}" +
    // folded: the header alone, its background the progress, left to right
    // folded, the title is the step under way: up to two lines of it
    ".card.min .h-title{white-space:normal;word-break:break-word;display:-webkit-box;" +
      "-webkit-line-clamp:2;-webkit-box-orient:vertical}" +
    ".card.min .header{border-bottom:none;background:linear-gradient(90deg," +
      "rgba(77,163,255,0.42) var(--p,0%),rgba(255,255,255,0.04) var(--p,0%))}" +
    ".panel{background:rgba(255,255,255,0.035);border:1px solid rgba(255,255,255,0.06);" +
      "border-radius:10px;padding:10px 12px}" +
    ".title{font-size:11px;letter-spacing:.08em;text-transform:uppercase;" +
      "color:#aab0b8;margin:0 0 8px}" +
    // progress
    ".bar{height:8px;border-radius:6px;background:rgba(255,255,255,0.10);overflow:hidden}" +
    ".bar>i{display:block;height:100%;width:0;border-radius:6px;background:#4f9dff;" +
      "transition:width .18s ease}" +
    ".pmeta{display:flex;justify-content:space-between;margin-top:6px;color:#c8ccd2}" +
    ".bar>i.back{background:#ffd54a}" +
    ".pct{font-weight:600;color:#fff}" +
    // status grid
    ".grid{display:grid;grid-template-columns:auto 1fr;gap:3px 10px}" +
    ".grid b{color:#9aa0a6;font-weight:500}" +
    ".grid span{color:#e8eaed;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}" +
    ".grid span.full{white-space:normal;word-break:break-word;overflow:visible}" +
    // tree
    ".tree{overflow:auto;max-height:46vh}" +
    ".node{display:block}" +
    ".node.collapsed>.kids{display:none}" +
    ".row{display:flex;align-items:center;gap:6px;padding:2px 4px;border-radius:6px;" +
      "white-space:nowrap}" +
    ".row.group{cursor:pointer}" +
    ".row.group:hover{background:rgba(255,255,255,0.06)}" +
    ".caret{width:12px;flex:0 0 auto;text-align:center;color:#8b9099;font-size:10px;" +
      "transition:transform .12s ease}" +
    ".node.collapsed>.row .caret{transform:rotate(-90deg)}" +
    ".row .ico{width:14px;text-align:center;flex:0 0 auto}" +
    ".row .lbl{overflow:hidden;text-overflow:ellipsis}" +
    // The step under way, and one that failed, wrap instead of being cut off:
    // the card has a fixed width, and a long selector ended in "..." exactly
    // where it said what was being done. Every label keeps a tooltip as well.
    ".row.running,.row.failed{white-space:normal;align-items:flex-start}" +
    ".row.running .lbl,.row.failed .lbl{overflow:visible;text-overflow:clip;" +
      "word-break:break-word}" +
    ".row.running{background:rgba(79,157,255,0.16)}" +
    ".row.running .ico{animation:hudpulse 1s ease-in-out infinite}" +
    ".row.failed{background:rgba(255,92,92,0.14)}" +
    ".kids{margin-left:13px;border-left:1px solid rgba(255,255,255,0.08);padding-left:3px}" +
    "@keyframes hudpulse{0%,100%{opacity:1}50%{opacity:.35}}" +
    // logs
    ".logs{overflow:auto;max-height:150px;font-family:ui-monospace,Menlo,Consolas,monospace;" +
      "font-size:11px;line-height:1.5}" +
    ".log{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}" +
    ".log .lv{font-weight:700;margin-right:6px}" +
    ".lv-INFO{color:#8ab4f8}.lv-WARN{color:#ffab40}.lv-ERROR{color:#ff5c5c}" +
    // banner
    ".banner{font-weight:600}" +
    ".banner.success{color:#3fbf5f}.banner.failed{color:#ff5c5c}" +
    ".banner .sub{display:block;color:#c8ccd2;font-weight:400;margin-top:3px}" +
    // stepping bar: outside the body, so a folded card keeps it
    ".steps{display:flex;align-items:center;gap:2px;padding:5px 8px;flex:0 0 auto;" +
      "border-bottom:1px solid rgba(255,255,255,0.08)}" +
    ".steps.paused{background:rgba(255,213,74,0.10)}" +
    ".steps.failure{background:rgba(255,92,92,0.14)}" +
    ".card.min .steps{border-bottom:none}" +
    ".sbtn{cursor:pointer;width:26px;height:24px;flex:0 0 auto;display:flex;" +
      "align-items:center;justify-content:center;border-radius:6px;color:#e8eaed}" +
    ".sbtn:hover{background:rgba(255,255,255,0.12)}" +
    ".sbtn.main{background:rgba(79,157,255,0.26);color:#fff}" +
    ".sbtn.main:hover{background:rgba(79,157,255,0.40)}" +
    ".sbtn.off{opacity:.3;pointer-events:none}" +
    ".slabel{flex:1;min-width:0;margin-left:8px;color:#c8ccd2;font-size:11px;line-height:1.3}" +
    ".slabel b{display:block;color:#fff;font-weight:600;white-space:nowrap;overflow:hidden;" +
      "text-overflow:ellipsis}" +
    // What going on will do is the line to read whole: it wraps.
    ".slabel i{font-style:normal;display:block}" +
    // breakpoints and the step being looked at
    ".bp{width:10px;height:10px;flex:0 0 auto;border-radius:50%;box-sizing:border-box;" +
      "border:1px solid transparent}" +
    ".tree:not(.stepping) .bp{display:none}" +
    ".tree.stepping .row.leaf .bp{cursor:pointer}" +
    ".tree.stepping .row.leaf:hover .bp{border-color:rgba(255,92,92,0.65)}" +
    ".bp.on,.tree.stepping .row.leaf:hover .bp.on{background:#ff5c5c;border-color:#ff5c5c}" +
    ".tree.paused .row.leaf.taken{cursor:pointer}" +
    ".tree.paused .row.leaf.taken:hover{background:rgba(255,255,255,0.06)}" +
    ".row.cursor,.tree.paused .row.cursor:hover{background:rgba(255,213,74,0.18);" +
      "box-shadow:inset 0 0 0 1px rgba(255,213,74,0.7)}" +
    ".row.next{background:rgba(255,213,74,0.08)}" +
    ".row.next .lbl{color:#ffe08a}" +
    // the copy of an earlier page, over the live one
    ".copy{position:fixed;inset:0;z-index:2147483645;display:flex;flex-direction:column;" +
      "background:#fff;pointer-events:auto}" +
    ".copy-strip{flex:0 0 auto;padding:5px 12px;background:#ffd54a;color:#1d1d1f;" +
      "font:600 12px/1.45 'Segoe UI',system-ui,-apple-system,sans-serif;" +
      "box-shadow:0 1px 4px rgba(0,0,0,0.25)}" +
    ".copy-strip span{font-weight:400;margin-left:10px;opacity:.8}" +
    // The frame takes no pointer, so focus never goes into it and the keys
    // always reach the HUD; the layer scrolls it for the wheel instead.
    ".copy iframe{flex:1 1 auto;width:100%;border:0;background:#fff;pointer-events:none}";

  // Stepping icons: drawn, not glyphs, so every font draws them the same.
  var SVG = function (body) {
    return '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">' + body + "</svg>";
  };
  var ICONS = {
    pause: SVG('<rect x="3" y="2.5" width="3.5" height="11" rx="1" fill="currentColor"/>' +
               '<rect x="9.5" y="2.5" width="3.5" height="11" rx="1" fill="currentColor"/>'),
    play: SVG('<path d="M4 2.5v11l9.5-5.5z" fill="currentColor"/>'),
    step: SVG('<path d="M2.5 2.5v11l7.5-5.5z" fill="currentColor"/>' +
              '<rect x="11" y="2.5" width="2.5" height="11" rx="1" fill="currentColor"/>'),
    back: SVG('<path d="M10.5 3 5.5 8l5 5" stroke="currentColor" stroke-width="2" fill="none"' +
              ' stroke-linecap="round" stroke-linejoin="round"/>'),
    forward: SVG('<path d="M5.5 3l5 5-5 5" stroke="currentColor" stroke-width="2" fill="none"' +
                 ' stroke-linecap="round" stroke-linejoin="round"/>')
  };

  // One listener per document, whatever happens to the Hud object: it asks
  // whichever renderer the window holds now.
  function onKey(e) {
    if (window.__ExecHud && window.__ExecHud._onKey) window.__ExecHud._onKey(e);
  }

  function elem(tag, cls) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    return n;
  }

  var Hud = {
    _shadow: null,
    _wrap: null, _card: null, _body: null, _titleEl: null,
    _treeVersion: null,
    _nodeEls: {},     // node id -> {row, ico}
    _groups: [],      // group node wrappers, for collapse-all
    _order: [],       // leaf ids in flat order (for group state calc)
    _parents: {},     // node id -> parent group id
    _els: {},         // named panel sub-elements
    _timer: null,
    _startedAt: null,
    _elapsedCell: null,
    _allCollapsed: false,
    _activeNode: null,
    _userScrolledAt: 0,
    _autoScrolling: false,
    _drag: null,
    _stepping: {},    // state.stepping as last rendered
    _commands: [],    // what was asked of the run, for Python to take
    _copy: null,      // the copy of an earlier page, while one is shown
    _res: {},         // a copy's resources by hash -> blob: address, kept
    _pending: null,   // where the person went, until Python has caught up
    _total: 0,
    _progress: null,

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
      var title = elem("div", "h-title"); title.textContent = "Execution";
      header.appendChild(title);
      header.appendChild(this._makeBtn("▣", "Toggle click-through (let the mouse pass to the page)", this._togglePassthrough));
      header.appendChild(this._makeBtn("⊟", "Collapse / expand all branches", this._toggleCollapseAll));
      header.appendChild(this._makeBtn("–", "Minimize", this._toggleMinimize));
      var steps = this._buildSteps();
      var body = elem("div", "body");
      card.appendChild(header);
      card.appendChild(steps);
      card.appendChild(body);
      wrap.appendChild(card);
      shadow.appendChild(wrap);

      this._shadow = shadow;
      this._wrap = wrap; this._card = card; this._body = body; this._titleEl = title;
      this._header = header; this._minBtn = header.lastChild;
      this._els = {}; this._treeVersion = null; this._nodeEls = {};
      this._copy = null;
      this._wireDrag(header);
      if (!window.__ExecHudKeys) {
        window.__ExecHudKeys = true;
        window.addEventListener("keydown", onKey, true);
      }
      var self = this;
      body.addEventListener("scroll", function () {
        if (!self._autoScrolling) self._userScrolledAt = Date.now();
      });
    },

    _makeBtn: function (glyph, tip, handler) {
      var b = elem("div", "btn");
      b.textContent = glyph;
      b.title = tip;
      var self = this;
      // pointer-down stops the drag handler from also firing on the header
      b.addEventListener("mousedown", function (e) { e.stopPropagation(); });
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        handler.call(self, b);
      });
      return b;
    },

    // --- header actions --------------------------------------------------
    _togglePassthrough: function (btn) {
      var on = this._card.classList.toggle("ghost");
      btn.textContent = on ? "▢" : "▣";
      btn.title = on ? "Click-through ON - click to make the HUD interactive again"
                     : "Toggle click-through (let the mouse pass to the page)";
    },

    _toggleMinimize: function (btn) {
      this._userMin = !this._card.classList.contains("min");   // a person's choice
      this._setMin(this._userMin);
    },

    _setMin: function (min) {
      this._card.classList.toggle("min", min);
      if (this._minBtn) {
        this._minBtn.textContent = min ? "▢" : "–";
        this._minBtn.title = min ? "Restore" : "Minimize";
      }
    },

    _toggleCollapseAll: function (btn) {
      this._allCollapsed = !this._allCollapsed;
      var collapsed = this._allCollapsed;
      this._groups.forEach(function (g) {
        // never collapse the root, so the tree never fully disappears
        if (g.getAttribute("data-root") === "1") return;
        g.classList.toggle("collapsed", collapsed);
      });
      btn.textContent = collapsed ? "⊞" : "⊟";
    },

    // --- stepping --------------------------------------------------------
    _buildSteps: function () {
      var bar = elem("div", "steps");
      bar.style.display = "none";
      var self = this;
      var make = function (name, handler) {
        var b = elem("div", "sbtn");
        b.innerHTML = ICONS[name];
        b.addEventListener("mousedown", function (e) { e.stopPropagation(); });
        b.addEventListener("click", function (e) { e.stopPropagation(); handler.call(self); });
        bar.appendChild(b);
        return b;
      };
      this._sb = {
        back: make("back", function () { this._move(-1); }),
        play: make("pause", function () {
          this._ask(this._stepping.paused ? "continue" : "pause");
        }),
        step: make("step", function () { this._ask("step"); }),
        forward: make("forward", function () { this._move(1); })
      };
      var label = elem("div", "slabel");
      label.appendChild(elem("b"));
      label.appendChild(elem("i"));
      bar.appendChild(label);
      this._sLabel = label;
      this._stepsBar = bar;
      return bar;
    },

    _ask: function (command, extra) {
      var asked = { command: command };
      Object.keys(extra || {}).forEach(function (k) { asked[k] = extra[k]; });
      this._commands.push(asked);
      if (this._commands.length > 50) this._commands.shift();
    },

    // What the person asked since Python last looked; taking it empties it.
    takeCommands: function () {
      var taken = this._commands;
      this._commands = [];
      return taken;
    },

    // Where the person is looking: their own last press until Python says it
    // has got there - so each press shows at once in the tree, the bar and the
    // progress, however long the copy for it takes to arrive.
    _cursor: function () {
      var st = this._stepping || {};
      if (this._pending && st.paused && Date.now() - this._pending.at < 15000) {
        return this._pending.cursor;
      }
      return st.cursor || 0;
    },

    // Back (-1) or forward (+1), from a key or a button.
    _move: function (delta) {
      var st = this._stepping || {};
      if (!st.paused) { this._ask(delta < 0 ? "back" : "forward"); return; }
      var last = st.last || 0, here = this._cursor();
      if (delta > 0 && here >= last) { this._ask("step"); return; }
      this._goTo(Math.max(0, Math.min(last, here + delta)));
    },

    // Where to look, as a number: presses that pile up while Python is busy
    // end where the last one did, not one after another.
    _goTo: function (cursor) {
      if (cursor === this._cursor()) return;
      this._pending = { cursor: cursor, at: Date.now() };
      this._ask("cursor", { index: cursor });
      this._renderStepping(this._stepping, this._total);
      this._applyStepping(null, this._activeNode);
      this._paintProgress();
      var st = this._stepping || {};
      if (cursor >= (st.last || 0)) {
        this.showCopy(null, "");                 // the live page is underneath
      } else if (this._copy) {
        this._copy._strip.textContent = this._caption(cursor) + " - on its way";
      }
    },

    _caption: function (cursor) {
      return cursor ? "Copy of the page after step " + cursor : "Copy of the page before step 1";
    },

    _keyText: function (action) {
      var k = (this._stepping.keys || {})[action];
      return k ? " (" + k.text + ")" : "";
    },

    _renderStepping: function (st, total) {
      var bar = this._stepsBar;
      st = st || {};
      this._stepping = st;
      this._total = total || this._total;
      if (!bar) return;
      if (!st.live) { bar.style.display = "none"; return; }
      bar.style.display = "";
      var paused = !!st.paused, cursor = this._cursor(), last = st.last || 0;
      var looking = paused && cursor < last;
      var failed = paused && st.reason === "failure";
      bar.className = "steps" + (failed ? " failure" : paused ? " paused" : "");
      var sb = this._sb;
      sb.play.innerHTML = ICONS[paused ? "play" : "pause"];
      sb.play.classList.toggle("main", paused);
      sb.play.title = (paused ? "Continue" : "Pause after this step") + this._keyText("pause");
      sb.step.title = (paused ? "Run one step, then pause" : "Pause after this step") +
                      this._keyText("step");
      sb.back.title = "Back: the page one step earlier" + this._keyText("back");
      sb.forward.title = (looking ? "Forward: the page one step later" : "Run one step") +
                         this._keyText("forward");
      sb.back.classList.toggle("off", paused && cursor <= 0);
      sb.forward.classList.toggle("off", !paused);
      var of = total ? " of " + total : "";
      var head, sub = "";
      if (!paused) {
        head = "Running";
        sub = "Pause" + this._keyText("pause") + " stops after this step";
      } else if (looking) {
        head = cursor ? "The page after step " + cursor : "The page before step 1";
        sub = "Continue runs step " + (cursor + 1) + " again, on the live page";
      } else if (failed) {
        head = "Step " + last + of + " failed";
        sub = "Go back to run steps again; continue ends the scenario";
      } else {
        head = last ? "Paused after step " + last + of : "Paused before step 1" + of;
        sub = st.reason === "breakpoint" ? "At a breakpoint" : "";
      }
      this._sLabel.firstChild.textContent = head;
      this._sLabel.lastChild.textContent = sub;
      this._sLabel.title = head + (sub ? "\n" + sub : "");
    },

    // Breakpoints and the step looked at, on the active scenario's leaves.
    _applyStepping: function (box, activeNode) {
      var st = this._stepping || {};
      if (box) {
        box.classList.toggle("stepping", !!st.live);
        box.classList.toggle("paused", !!st.paused);
      }
      var points = {};
      (st.breakpoints || []).forEach(function (i) { points[i] = true; });
      var prefix = (activeNode || "") + "/";
      var self = this, cursor = this._cursor();
      this._order.forEach(function (id) {
        var n = self._nodeEls[id];
        if (!n || n.index == null) return;
        var mine = !!activeNode && id.indexOf(prefix) === 0;
        n.bp.classList.toggle("on", mine && !!points[n.index]);
        var taken = mine && st.paused && n.index < (st.last || 0);
        n.row.classList.toggle("taken", taken);
        n.row.classList.toggle("cursor", taken && n.index === cursor - 1 &&
                                         cursor < st.last);
        n.row.classList.toggle("next", mine && st.paused && n.index === (st.last || 0) &&
                                       cursor === st.last);
      });
    },

    _toggleBreakpoint: function (id) {
      var st = this._stepping || {}, n = this._nodeEls[id];
      if (!st.live || !n || id.indexOf((this._activeNode || "") + "/") !== 0) return;
      var points = (st.breakpoints || []).filter(function (i) { return i !== n.index; });
      if (points.length === (st.breakpoints || []).length) points.push(n.index);
      st.breakpoints = points.sort(function (a, b) { return a - b; });
      this._ask("breakpoints", { indices: st.breakpoints });
      this._applyStepping(null, this._activeNode);
    },

    _lookAt: function (id) {
      var st = this._stepping || {}, n = this._nodeEls[id];
      if (!st.live || !st.paused || !n || n.index >= (st.last || 0)) return;
      if (id.indexOf((this._activeNode || "") + "/") !== 0) return;
      this._goTo(n.index + 1);
    },

    _onKey: function (e) {
      var st = this._stepping || {};
      if (!st.live) return;
      var keys = st.keys || {};
      var key = e.key || "";
      // The copy cannot take focus, so its scrolling keys come here.
      var scroll = { PageDown: [0, 0.9], PageUp: [0, -0.9], ArrowDown: [40, 0],
                     ArrowUp: [-40, 0], " ": [0, 0.9] }[key];
      if (this._copy && !e.altKey && !e.ctrlKey && !e.metaKey && (scroll || key === "Home" ||
                                                                  key === "End")) {
        var win = this._copy._frame.contentWindow;
        if (win) {
          e.preventDefault();
          e.stopImmediatePropagation();
          if (key === "Home") win.scrollTo(0, 0);
          else if (key === "End") win.scrollTo(0, win.document.body.scrollHeight);
          else win.scrollBy(0, scroll[0] + scroll[1] * win.innerHeight);
          return;
        }
      }
      for (var action in keys) {
        var k = keys[action];
        if (!k || !k.key) continue;
        var same = k.key.length === 1 ? key.toLowerCase() === k.key.toLowerCase()
                                      : key === k.key;
        if (same && !!e.altKey === !!k.alt && !!e.ctrlKey === !!k.ctrl &&
            !!e.shiftKey === !!k.shift && !!e.metaKey === !!k.meta) {
          // Taken from the page: Alt+Left would otherwise also be the
          // browser's Back, and leave the page the run is on.
          e.preventDefault();
          e.stopImmediatePropagation();
          if (action === "back" || action === "forward") this._move(action === "back" ? -1 : 1);
          else this._ask(action === "pause" ? (st.paused ? "continue" : "pause") : action);
          return;
        }
      }
    },

    // A copy's resources, each sent once and kept as a blob: address for every
    // later copy of the page (engine/history.py names them qavres:<hash>).
    missingResources: function (names) {
      var self = this;
      return (names || []).filter(function (name) { return !self._res[name]; });
    },

    addResources: function (list) {
      var self = this;
      (list || []).forEach(function (one) {
        var name = one[0], kind = one[1], data = one[2], text = one[3];
        if (self._res[name]) return;
        var blob;
        if (text) {
          // What a stylesheet or a frame names was sent before it.
          blob = new Blob([self._resolve(data)], { type: kind });
        } else {
          var bytes = atob(data), out = new Uint8Array(bytes.length);
          for (var i = 0; i < bytes.length; i++) out[i] = bytes.charCodeAt(i);
          blob = new Blob([out], { type: kind });
        }
        self._res[name] = URL.createObjectURL(blob);
      });
      return true;
    },

    _resolve: function (text) {
      var res = this._res;
      return String(text || "").replace(/qavres:([0-9a-f]{40})/g, function (all, name) {
        return res[name] || all;
      });
    },

    // Lay a copy of an earlier page over the live one, or take it away (null).
    // A frame without scripts, filled from a string: the copy cannot run
    // anything, and it asks the server for nothing.
    showCopy: function (html, caption) {
      try {
        this.ensure();
        if (!html) {
          if (this._copy && this._copy.parentNode) this._copy.parentNode.removeChild(this._copy);
          this._copy = null;
          return true;
        }
        var layer = this._copy;
        if (!layer) {
          layer = elem("div", "copy");
          var strip = elem("div", "copy-strip");
          var frame = document.createElement("iframe");
          frame.setAttribute("sandbox", "allow-same-origin");
          frame.setAttribute("tabindex", "-1");
          layer.addEventListener("wheel", function (e) {
            try {
              frame.contentWindow.scrollBy(e.deltaX, e.deltaY);
              e.preventDefault();
            } catch (err) {}
          }, { passive: false });
          layer.appendChild(strip);
          layer.appendChild(frame);
          layer._strip = strip; layer._frame = frame;
          this._shadow.insertBefore(layer, this._wrap);
          this._copy = layer;
        }
        html = this._resolve(html);
        layer._strip.textContent = caption || "";
        var keys = this._stepping.keys || {};
        if (keys.back && keys.forward) {
          var hint = elem("span");
          hint.textContent = keys.back.text + " / " + keys.forward.text + " move between steps";
          layer._strip.appendChild(hint);
        }
        layer._frame.srcdoc = html;
        return true;
      } catch (e) { return false; }
    },

    // --- dragging --------------------------------------------------------
    _wireDrag: function (header) {
      var self = this;
      header.addEventListener("mousedown", function (e) {
        if (e.button !== 0) return;
        var r = self._wrap.getBoundingClientRect();
        self._drag = { dx: e.clientX - r.left, dy: e.clientY - r.top };
        // switch from right-anchored to left/top so we can move freely
        self._wrap.style.right = "auto";
        self._wrap.style.left = r.left + "px";
        self._wrap.style.top = r.top + "px";
        window.addEventListener("mousemove", onMove, true);
        window.addEventListener("mouseup", onUp, true);
        e.preventDefault();
      });
      function onMove(e) {
        if (!self._drag) return;
        var x = e.clientX - self._drag.dx, y = e.clientY - self._drag.dy;
        var maxX = window.innerWidth - 60, maxY = window.innerHeight - 30;
        self._wrap.style.left = Math.max(0, Math.min(x, maxX)) + "px";
        self._wrap.style.top = Math.max(0, Math.min(y, maxY)) + "px";
      }
      function onUp() {
        self._drag = null;
        window.removeEventListener("mousemove", onMove, true);
        window.removeEventListener("mouseup", onUp, true);
      }
    },

    _panel: function (key, builder) {
      if (this._els[key]) return this._els[key];
      var p = builder();
      this._els[key] = p;
      this._body.appendChild(p);
      return p;
    },

    render: function (state) {
      try {
        this.ensure();
        state = state || {};
        var comp = {};
        (state.components || []).forEach(function (c) { comp[c] = true; });

        if (this._wrap) this._wrap.classList.toggle("acting", !!state.acting);
        // Folded by default when asked, unless a person folded or unfolded it.
        var folded = this._userMin === undefined ? !!state.collapsed : this._userMin;
        if (this._card && this._card.classList.contains("min") !== folded) this._setMin(folded);
        var pr = state.progress || {};
        this._progress = pr;
        this._renderBanner(state.banner);
        var stepping = state.stepping || {};
        // Python has caught up with the person's last press: its word again.
        if (this._pending && (!stepping.paused || stepping.cursor === this._pending.cursor)) {
          this._pending = null;
        }
        this._renderStepping(stepping, pr.total);
        this._paintProgress();
        if (comp.status) this._renderStatus(state.status);
        if (comp.progress) this._renderProgress(state.progress);
        if (comp.tree) this._renderTree(state);
        if (comp.logs) this._renderLogs(state.logs);
        // Folded, the header names the step under way rather than the scenario:
        // the one line left should say what is being done. Unfolded, the
        // status panel says it, and the header goes back to the scenario.
        var stt = state.status || {};
        if (stt.flow) this._flowName = stt.flow;
        if (this._titleEl) {
          var doing = stt.action && stt.action !== "-" ? stt.action : "";
          var text = folded && doing ? doing : (this._flowName || this._titleEl.textContent);
          this._titleEl.textContent = text;
          this._titleEl.title = (this._flowName || "") + (doing ? "\n" + doing : "");
        }
      } catch (e) { /* never throw into the page */ }
      // The tree this document holds: Python sends the tree again only when this
      // is not the one it has - a new document starts with none.
      return this._treeVersion;
    },

    // --- banner (flow completion) ---------------------------------------
    _renderBanner: function (banner) {
      var p = this._els.banner;
      if (!banner) { if (p) p.style.display = "none"; return; }
      if (!p) {
        p = elem("div", "panel");
        var b = elem("div", "banner");
        p.appendChild(b);
        p._b = b;
        this._els.banner = p;
        this._body.insertBefore(p, this._body.firstChild);
      }
      p.style.display = "";
      p._b.className = "banner " + banner.kind;
      p._b.innerHTML = "";
      p._b.appendChild(document.createTextNode(banner.text || ""));
      if (banner.sub) {
        var s = elem("span", "sub");
        s.textContent = banner.sub;
        p._b.appendChild(s);
      }
    },

    // --- status ----------------------------------------------------------
    _renderStatus: function (st) {
      st = st || {};
      var p = this._panel("status", function () {
        var panel = elem("div", "panel");
        var grid = elem("div", "grid");
        var rows = {};
        ["Role", "Current", "State", "Elapsed"].forEach(function (k) {
          var b = elem("b"); b.textContent = k + ":";
          // What is being done is the line worth reading whole.
          var v = elem("span", k === "Current" ? "full" : "");
          grid.appendChild(b); grid.appendChild(v);
          rows[k] = v;
        });
        panel.appendChild(grid);
        panel._rows = rows;
        return panel;
      });
      if (st.flow && this._titleEl) this._titleEl.textContent = st.flow;
      p._rows.Role.textContent = st.role || "-";
      p._rows.Current.textContent = st.action || "-";
      p._rows.Current.title = st.action || "";
      p._rows.State.textContent = st.state || "-";
      // Elapsed ticks locally (once/sec) so Python never pushes just for the clock;
      // it freezes once flow_end stamps stoppedAt (the HUD lingers after the run).
      this._startedAt = st.startedAt || this._startedAt;
      this._stoppedAt = st.stoppedAt || null;
      this._elapsedCell = p._rows.Elapsed;
      this._tickElapsed();
      if (this._stoppedAt) {
        if (this._timer) { clearInterval(this._timer); this._timer = null; }
      } else if (!this._timer) {
        var self = this;
        this._timer = setInterval(function () { self._tickElapsed(); }, 1000);
      }
    },

    _tickElapsed: function () {
      var cell = this._elapsedCell;
      if (!this._startedAt || !cell) return;
      var end = this._stoppedAt || Date.now();
      var s = Math.max(0, Math.floor((end - this._startedAt) / 1000));
      var hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
      var pad = function (n) { return (n < 10 ? "0" : "") + n; };
      cell.textContent = pad(hh) + ":" + pad(mm) + ":" + pad(ss);
    },

    // --- progress --------------------------------------------------------
    _renderProgress: function (pr) {
      pr = pr || { done: 0, total: 0 };
      var p = this._panel("progress", function () {
        var panel = elem("div", "panel");
        var bar = elem("div", "bar"); var fill = elem("i"); bar.appendChild(fill);
        var meta = elem("div", "pmeta");
        var pct = elem("span", "pct"); var cnt = elem("span");
        meta.appendChild(pct); meta.appendChild(cnt);
        panel.appendChild(bar); panel.appendChild(meta);
        panel._fill = fill; panel._pct = pct; panel._cnt = cnt;
        return panel;
      });
      this._progress = pr;
      this._paintProgress();
    },

    // The bar and the folded header's fill: how far the run has gone - or,
    // looking back at an earlier step, where that step is.
    _paintProgress: function () {
      var pr = this._progress || {}, st = this._stepping || {};
      var total = pr.total || 0, done = pr.done || 0;
      var cursor = this._cursor(), looking = st.paused && cursor < (st.last || 0);
      var at = looking ? cursor : done;
      var pctv = total ? Math.round((at / total) * 100) : 0;
      var text = looking ? "step " + cursor + " of " + total + " - live after step " + st.last
                         : done + " / " + total + " steps";
      if (this._header) {
        this._header.style.setProperty("--p", pctv + "%");
        this._header.title = total ? text : "";
      }
      var p = this._els.progress;
      if (!p) return;
      p._fill.style.width = pctv + "%";
      p._fill.classList.toggle("back", !!looking);
      p._pct.textContent = pctv + "%";
      p._cnt.textContent = total ? text : "";
    },

    // --- tree ------------------------------------------------------------
    _renderTree: function (state) {
      var p = this._panel("tree", function () {
        var panel = elem("div", "panel");
        var title = elem("div", "title"); title.textContent = "Execution tree";
        var box = elem("div", "tree");
        panel.appendChild(title); panel.appendChild(box);
        panel._box = box;
        return panel;
      });
      // Rebuild the DOM only when the tree identity changes (once per scenario).
      var rebuilt = false;
      if (state.tree && state.treeVersion !== this._treeVersion) {
        this._treeVersion = state.treeVersion;
        this._buildTree(p._box, state.tree);
        rebuilt = true;
      }
      // Only the running scenario is expanded; the others collapse to one line so
      // a long plan stays readable. Re-applied when the active scenario CHANGES
      // (or the tree was rebuilt), never on every push - otherwise a branch the
      // user opened by hand would snap shut again a moment later.
      if (rebuilt || state.activeNode !== this._activeNode) {
        this._activeNode = state.activeNode;
        // null before the first scenario starts: nothing is active, so every
        // planned scenario sits collapsed on one line.
        var active = state.activeNode || null;
        (this._groups || []).forEach(function (g) {
          var sid = g.getAttribute("data-scenario");
          if (sid) g.classList.toggle("collapsed", sid !== active);
        });
      }
      this._applyStates(state.nodeStates || {});
      this._applyStepping(p._box, this._activeNode);
    },

    _buildTree: function (box, root) {
      box.innerHTML = "";
      this._nodeEls = {}; this._order = []; this._parents = {}; this._groups = [];
      var self = this;
      (function walk(node, container, parentId, isRoot) {
        var wrapper = elem("div", "node");
        var isGroup = node.kind !== "step";
        var row = elem("div", "row" + (isGroup ? " group" : " leaf"));
        // Every row has the gutter, so steps and groups stay aligned; only a
        // step's takes a breakpoint.
        var bp = elem("span", "bp");
        var caret = elem("span", "caret");
        caret.textContent = isGroup ? "▾" : "";
        var ico = elem("span", "ico"); ico.textContent = ICON.pending;
        ico.style.color = COLOR.pending;
        var lbl = elem("span", "lbl"); lbl.textContent = node.label || node.id;
        lbl.title = node.label || node.id;
        row.appendChild(bp); row.appendChild(caret); row.appendChild(ico); row.appendChild(lbl);
        wrapper.appendChild(row);
        container.appendChild(wrapper);
        var index = !isGroup && typeof node.step_index === "number" ? node.step_index : null;
        self._nodeEls[node.id] = { row: row, ico: ico, bp: bp, index: index, state: null };
        if (parentId != null) self._parents[node.id] = parentId;
        if (index != null) {
          bp.title = "Pause before this step";
          bp.addEventListener("click", function (e) {
            e.stopPropagation();
            self._toggleBreakpoint(node.id);
          });
          row.addEventListener("click", function () { self._lookAt(node.id); });
        }

        if (isGroup) {
          if (isRoot) wrapper.setAttribute("data-root", "1");
          // A scenario group: a direct child of the session root. These are the
          // ones auto-expanded/collapsed as the run moves from one to the next.
          if (!isRoot && parentId === "session") {
            wrapper.setAttribute("data-scenario", node.id);
          }
          self._groups.push(wrapper);
          row.addEventListener("click", function () {
            wrapper.classList.toggle("collapsed");
          });
          var kids = elem("div", "kids");
          wrapper.appendChild(kids);
          (node.children || []).forEach(function (c) { walk(c, kids, node.id, false); });
        } else {
          self._order.push(node.id);
        }
      })(root, box, null, true);
    },

    _applyStates: function (leafStates) {
      // Leaves come straight from Python; group state is derived from descendants.
      var group = {};
      var self = this;
      var running = null;
      this._order.forEach(function (leafId) {
        var s = leafStates[leafId] || "pending";
        self._paint(leafId, s);
        if (s === "running") running = leafId;
        var pid = self._parents[leafId];
        while (pid != null) {
          var g = group[pid] || (group[pid] = { total: 0, pending: 0, running: 0,
                                                success: 0, failed: 0, skipped: 0 });
          g.total++; g[s] = (g[s] || 0) + 1;
          pid = self._parents[pid];
        }
      });
      Object.keys(group).forEach(function (gid) {
        self._paint(gid, self._groupState(group[gid]));
      });
      // Auto-scroll to the running node, unless the user scrolled recently.
      if (running && this._nodeEls[running] && Date.now() - this._userScrolledAt > 4000) {
        this._autoScrolling = true;
        this._nodeEls[running].row.scrollIntoView({ block: "nearest" });
        var self2 = this;
        setTimeout(function () { self2._autoScrolling = false; }, 80);
      }
    },

    _groupState: function (g) {
      if (g.failed) return "failed";
      if (g.running) return "running";
      if (g.success && (g.pending || g.skipped)) return "running";  // in progress
      if (g.success === g.total) return "success";
      if (g.skipped === g.total) return "skipped";
      return "pending";
    },

    _paint: function (id, state) {
      var n = this._nodeEls[id];
      // Only a node whose state changed touches the DOM: every push repaints
      // the whole tree, and writing all of it each time had the browser
      // restyle 780 rows twice a step.
      if (!n || n.state === state) return;
      // Only the state's own class changes: group, leaf and the stepping marks
      // (breakpoint, the step looked at) belong to the row, not to its state.
      if (n.state) n.row.classList.remove(n.state);
      n.state = state;
      n.row.classList.add(state);
      n.ico.textContent = ICON[state] || ICON.pending;
      n.ico.style.color = COLOR[state] || COLOR.pending;
    },

    // --- logs ------------------------------------------------------------
    _renderLogs: function (logs) {
      logs = logs || [];
      var p = this._panel("logs", function () {
        var panel = elem("div", "panel");
        var title = elem("div", "title"); title.textContent = "Logs";
        var box = elem("div", "logs");
        panel.appendChild(title); panel.appendChild(box);
        panel._box = box;
        return panel;
      });
      var box = p._box;
      box.innerHTML = "";
      logs.forEach(function (entry) {
        var line = elem("div", "log");
        var lv = elem("span", "lv lv-" + (entry.level || "INFO"));
        lv.textContent = "[" + (entry.level || "INFO") + "]";
        line.appendChild(lv);
        line.appendChild(document.createTextNode(entry.msg || ""));
        box.appendChild(line);
      });
      box.scrollTop = box.scrollHeight;   // auto-scroll to newest
    },

    // Flash a box over the element a step just acted on, so you can see WHAT was
    // clicked / typed into / pressed rather than inferring it from the tree.
    //
    // Draws its own absolutely-positioned box instead of styling the target: the
    // page's own CSS is never touched, so nothing reflows and a screenshot taken
    // straight afterwards still shows the app exactly as the app renders it.
    // Called with the element Playwright itself resolved, because these selectors
    // use Playwright syntax (:nth-match, :text-is) that querySelector cannot parse.
    mark: function (el, label) {
      try {
        if (!el || !el.getBoundingClientRect) return false;
        this.ensure();
        var shadow = this._shadow;
        if (!shadow) return false;
        var r = el.getBoundingClientRect();
        if (!r || (!r.width && !r.height)) return false;   // detached / display:none

        var box = elem("div", "mark");
        box.style.left = r.left + "px";
        box.style.top = r.top + "px";
        box.style.width = r.width + "px";
        box.style.height = r.height + "px";
        if (label) {
          var tag = elem("div", "mark-tag");
          tag.textContent = label;
          // Flip the caption inside the box when the element is near the top edge.
          if (r.top < 22) tag.style.top = "0";
          box.appendChild(tag);
        }
        shadow.appendChild(box);
        // Self-cleaning: the box is transient, so a long run never accumulates
        // hundreds of stale nodes in the shadow root.
        setTimeout(function () {
          if (box.parentNode) box.parentNode.removeChild(box);
        }, 1400);
        return true;
      } catch (e) { return false; }
    },

    remove: function () {
      try { if (this._timer) { clearInterval(this._timer); this._timer = null; } } catch (e) {}
      var host = document.getElementById(HOST_ID);
      if (host && host.parentNode) host.parentNode.removeChild(host);
      this._shadow = null;
    }
  };

  window.__ExecHud = Hud;
})();
