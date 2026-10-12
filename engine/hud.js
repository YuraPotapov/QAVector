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
    ".banner .sub{display:block;color:#c8ccd2;font-weight:400;margin-top:3px}";

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
      var body = elem("div", "body");
      card.appendChild(header);
      card.appendChild(body);
      wrap.appendChild(card);
      shadow.appendChild(wrap);

      this._shadow = shadow;
      this._wrap = wrap; this._card = card; this._body = body; this._titleEl = title;
      this._header = header; this._minBtn = header.lastChild;
      this._els = {}; this._treeVersion = null; this._nodeEls = {};
      this._wireDrag(header);
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
        if (this._header) {
          this._header.style.setProperty("--p", (pr.total ? Math.round(100 * (pr.done || 0) / pr.total) : 0) + "%");
          this._header.title = pr.total ? (pr.done || 0) + " / " + pr.total + " steps" : "";
        }
        this._renderBanner(state.banner);
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
      var total = pr.total || 0, done = pr.done || 0;
      var pctv = total ? Math.round((done / total) * 100) : 0;
      p._fill.style.width = pctv + "%";
      p._pct.textContent = pctv + "%";
      p._cnt.textContent = done + " / " + total + " steps";
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
    },

    _buildTree: function (box, root) {
      box.innerHTML = "";
      this._nodeEls = {}; this._order = []; this._parents = {}; this._groups = [];
      var self = this;
      (function walk(node, container, parentId, isRoot) {
        var wrapper = elem("div", "node");
        var isGroup = node.kind !== "step";
        var row = elem("div", "row" + (isGroup ? " group" : ""));
        var caret = elem("span", "caret");
        caret.textContent = isGroup ? "▾" : "";
        var ico = elem("span", "ico"); ico.textContent = ICON.pending;
        ico.style.color = COLOR.pending;
        var lbl = elem("span", "lbl"); lbl.textContent = node.label || node.id;
        lbl.title = node.label || node.id;
        row.appendChild(caret); row.appendChild(ico); row.appendChild(lbl);
        wrapper.appendChild(row);
        container.appendChild(wrapper);
        self._nodeEls[node.id] = { row: row, ico: ico };
        if (parentId != null) self._parents[node.id] = parentId;

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
      if (!n) return;
      n.ico.textContent = ICON[state] || ICON.pending;
      n.ico.style.color = COLOR[state] || COLOR.pending;
      // keep the "group" class so the caret/hover styling survives a repaint
      n.row.className = "row " + state + (n.row.classList.contains("group") ? " group" : "");
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
