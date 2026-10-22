"""The run's operational view, with native approval controls."""

import html
import math
import time
from collections import Counter
from urllib.parse import quote, unquote

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QFrame, QProgressBar, QPushButton, QScrollArea,
                               QSizePolicy, QTextBrowser, QVBoxLayout, QWidget)

from . import cycleactivity as activity, cycleapproval, cycleresult, theme, widgets


def _text(value):
    return html.escape(str(value or ""))


def _link(cycle, name, plugins):
    node = next((one for one in (cycle.get("graph") or {}).get("nodes", []) if one.get("id") == name), {})
    return '<a href="node:%s">%s%s</a>' % (quote(name, safe=""), "! " if node.get("important") else "", _text(activity.label(cycle, name, plugins)))


def _visible_approvals(cycle, status="pending"):
    if activity.stopping(cycle) or cycle.get("status") == "cancelled":
        return []
    steps = cycle.get("steps") or {}
    return [one for one in (cycle.get("node_approvals") or {}).values()
            if one.get("status") == status and (steps.get(one.get("step")) or {}).get("status") not in ("skipped", "cancelled")]


def _current_node(cycle, live, request_id=None):
    if cycle.get("status") == "cancelled":
        return "", None
    if activity.stopping(cycle):
        return activity.primary(cycle) if live else "", None
    if cycle.get("paused"):
        declined = _visible_approvals(cycle, "declined")
        if cycle.get("pause_reason") == "approval_declined" and declined:
            return declined[-1].get("step", ""), declined[-1]
        return "", None
    pending = _visible_approvals(cycle)
    if pending:
        receipt = next((one for one in pending if one.get("id") == request_id), pending[0])
        return receipt.get("step", ""), receipt
    return activity.primary(cycle) if live else "", None


def _reading_position(browser):
    """Keep visible text at its screen offset when earlier cards disappear."""
    top = browser.verticalScrollBar().value()
    bottom = top + browser.viewport().height()
    document = browser.document()
    layout = document.documentLayout()
    blocks, counts = [], Counter()
    block = document.begin()
    while block.isValid():
        text = block.text()
        if text.strip():
            rect = layout.blockBoundingRect(block)
            offset = rect.top() - top
            visible = rect.bottom() > top and rect.top() < bottom
            blocks.append((not visible, offset if visible else abs(offset), text, offset))
            counts[text] += 1
        block = block.next()
    anchors = [(text, offset) for _, _, text, offset in sorted(blocks) if counts[text] == 1]
    return top, browser.horizontalScrollBar().value(), anchors


def _restore_reading_position(browser, position):
    top, left, anchors = position or (0, 0, [])
    document = browser.document()
    layout = document.documentLayout()
    positions, counts = {}, Counter()
    block = document.begin()
    while block.isValid():
        text = block.text()
        positions[text] = layout.blockBoundingRect(block).top()
        counts[text] += 1
        block = block.next()
    for text, offset in anchors:
        if counts[text] == 1:
            top = round(positions[text] - offset)
            break
    browser.verticalScrollBar().setValue(top)
    browser.horizontalScrollBar().setValue(left)


def render(cycle, live=False, plugins=None, threshold=60, expanded_details=(), include_current=True,
           questions=(), now=None):
    """The Cockpit's page. ``questions`` are those standing in windows of their
    own (``AskBridge.waiting``) - the run's, since a window runs one at a time."""
    if not cycle:
        return "<h2>Run Cockpit</h2><p>Start a cycle or select a saved run in Subjects.</p>"
    plugins = plugins or {}
    steps = cycle.get("steps") or {}
    subject = cycle.get("subject") or {}
    title = " · ".join(str(value) for value in (subject.get("key") or cycle.get("name") or cycle.get("cycle"),
                                                cycle.get("current_phase")) if value)
    coverage = cycle.get("coverage") or {}
    parts = ['<h2>%s</h2>' % _text(title), '<p>%s · %s/%s selected steps completed · %s total</p>' % (
        _text(cycle.get("scope") or "legacy scope"), coverage.get("completed", 0),
        coverage.get("selected", "?"), coverage.get("total", len(steps)))]
    if cycle.get("control_error"):
        parts.append('<p><b>%s</b></p>' % _text(cycle["control_error"]))
    mode = cycle.get("execution_mode") or ((cycle.get("graph") or {}).get("execution") or {}).get("default_mode") or "auto"
    parts.append('<p><b>Mode: %s</b></p>' % _text(mode.capitalize()))
    if mode == "auto":
        parts.append('<p>Important steps also run without implicit approval. Explicit decision gates still ask for a decision.</p>')
    approvals = _visible_approvals(cycle)
    if approvals:
        parts.append('<h3>WAITING FOR NODE APPROVAL</h3>')
        for receipt in approvals:
            parts.append('<p><b>%s</b><br>%s</p>%s%s' % (
                _text(receipt.get("label") or receipt.get("step")), _text(receipt.get("importance_reason") or "Approval before this step starts"),
                cycleapproval.preview(receipt, cycleapproval.binding(receipt) in expanded_details),
                "" if live else "<p>Resume to review this request</p>"))
    asked = list(questions or ()) if live else []
    if asked:
        # Answered in its own window, not here: what the Cockpit adds is that
        # one is standing, for how long, and the way back to it.
        parts.append('<h3>WAITING FOR YOUR ANSWER</h3>')
        now = time.time() if now is None else now
        for one in asked:
            parts.append('<p><b>%s</b><br>%s<br>Waiting %s · answered in its own window</p>' % (
                _text(activity.label(cycle, one.get("step"), plugins) if one.get("step") else "Question"),
                _text(one.get("question")),
                _text(activity.duration(max(0, now - (one.get("since") or now)) * 1000))))
    phase_html = ""
    if cycle.get("phase_definition"):
        rows = []
        for phase in cycle["phase_definition"]:
            receipt = (cycle.get("phase_receipts") or {}).get(phase["id"], {})
            state = "Needs rerun" if receipt.get("invalidated") else receipt.get("status", "pending").capitalize()
            policy = {"automatic": "Automatic", "stop_after": "Pause after completion", "approval_required": "Approval required",
                      "manual": "Run on request"}.get(phase.get("policy", "automatic"), phase.get("policy"))
            rows.append('<p><b>%s</b> · %s · %s<br>%s%s</p>' % tuple(_text(value) for value in (
                phase.get("title") or phase["id"], state, policy, phase.get("purpose", ""),
                " · " + receipt["outcome"].replace("_", " ").capitalize() if receipt.get("outcome") and not receipt.get("invalidated") else "")))
        phase_html = '<h3>PHASES</h3>' + "".join(rows)
    if not live:
        parts.append("<p><b>%s</b></p>" % _text(
            cycleresult.completion(cycle)[0] if cycle.get("status") == "success" else activity.header(cycle)))
    names = activity.active(cycle) if live else []
    primary, current_approval = _current_node(cycle, live)

    def card(name):
        step = steps[name]
        progress = step.get("progress") or {}
        generation = step.get("execution_generation", 1)
        previous = sum(segment.get("elapsed_ms", 0) for segment in step.get("execution_segments") or []
                       if segment.get("generation", 1) != generation)
        count = ("%s / %s %s · " % (progress["current"], progress["total"], progress.get("unit", ""))
                 if progress.get("current") is not None and progress.get("total") is not None else "")
        return '<p><b>%s</b><br>%s · %s<br>%s%s<br>%s%s</p>' % (
            _link(cycle, name, plugins), _text(activity.duration(activity.elapsed(step, live))),
            _text(activity.status(step, cycle.get("_stopping", False))), _text(count),
            _text(progress.get("title") or step.get("message")),
            _text(activity.activity_text(step, live, threshold)),
            ("<br>Execution %s · previous executions %s" % (generation, activity.duration(previous))) if generation > 1 else "")

    operational_start = len(parts)
    if include_current:
        parts.append('<h3>NOW</h3>' + (card(primary) if primary and current_approval is None else
                     '<p>%s</p>' % _text(activity.header(cycle, live))))
    parts.append('<h3>ALSO RUNNING</h3>' + ("".join(card(name) for name in names if name != primary)
                                          or "<p>No parallel steps.</p>"))
    completed = [name for name, step in steps.items() if step.get("status") == "success"]
    completed_start = len(parts)
    parts.append('<h3>COMPLETED · %s</h3>' % len(completed))
    for name in completed:
        step = steps[name]
        evidence = step.get("message") or "%d artifact(s)" % len(step.get("artifacts") or [])
        parts.append('<p>%s · %s<br>%s%s</p>' % (
            _link(cycle, name, plugins), _text(activity.duration(activity.elapsed(step))), _text(evidence[:700]),
            " · reused from " + _text(step["source_run"]) if step.get("source_run") else ""))
    if not completed:
        parts.append("<p>No completed steps yet.</p>")
    next_start = len(parts)
    parts.append('<h3>NEXT</h3>')
    next_nodes = activity.next_nodes(cycle)
    for node in next_nodes:
        waiting = [name for name in node.get("needs") or [] if (steps.get(name) or {}).get("status") != "success"]
        reason = "Waiting for " + ", ".join(activity.label(cycle, name, plugins) for name in waiting) if waiting else "Dependencies settled"
        if any(receipt.get("step") == node["id"] for receipt in approvals):
            reason = "Waiting for node approval"
        if node.get("condition"):
            reason += " · condition will be checked before starting"
        if node.get("plugin") in ("approval.gate", "user.input", "user.choice"):
            reason += " · requires your decision"
        if not live:
            reason += " · Resume to continue" if cycle.get("paused") else " · run is stopped"
        elif cycle.get("paused"):
            reason += " · " + (cycle.get("next_action") or "Continue run")
        parts.append('<p>%s<br>%s</p>' % (_link(cycle, node["id"], plugins), _text(reason)))
    if not next_nodes:
        parts.append("<p>No further steps will start.</p>" if activity.stopping(cycle) or cycle.get("status") == "cancelled"
                     else "<p>No pending steps.</p>")
    parts.append('<h3>EXTERNAL CHANGES</h3>')
    brief = cycle.get("task_brief") or {}
    parts.append('<p>%s</p>' % ("None declared by Prepare task. Shell effects have not been independently verified."
                               if cycle.get("outcome") == "task_prepared" and brief.get("effects_verification") == "declared" else
                               "Not recorded. This run does not yet have an external change ledger."))
    attention_start = len(parts)
    problems = [name for name, step in steps.items() if step.get("status") != "skipped" and (
        step.get("status") in activity.FAILED or step.get("reason_code") == "blocked" or step.get("blocked_by"))]
    if problems:
        parts.append('<h3>NEEDS ATTENTION</h3>')
        roots = {activity.causes(cycle, name)[-1][0] for name in problems if activity.causes(cycle, name)}
        blocked = sum(bool(step.get("blocked_by")) for step in steps.values())
        parts.append('<p>%d root failure(s) · %d downstream step(s) blocked</p>' % (len(roots), blocked))
        for name in problems:
            chain = [(one, message) for one, message in activity.causes(cycle, name)
                     if steps[one].get("status") != "skipped"]
            parts.append('<p>' + '<br>'.join('%s · %s' % (_link(cycle, one, plugins), _text(message[:1000]))
                                             for one, message in chain) + '</p>')
    content = "".join(parts[:operational_start])
    content += '<table width="100%%" cellspacing="8"><tr><td width="58%%" valign="top">%s</td><td valign="top">%s</td></tr></table>' % (
        "".join(parts[operational_start:completed_start]), "".join(parts[next_start:attention_start]))
    content += "".join(parts[attention_start:])
    content += phase_html
    if cycle.get("policy_changes"):
        content += '<h3>MODE CHANGES</h3>'
        for change in cycle["policy_changes"]:
            content += '<p>%s to %s · %s</p>' % tuple(_text(value) for value in (
                change.get("from", change.get("previous_mode", "")), change.get("to", change.get("mode", "")),
                {"paused_change": "Changed while paused", "resume_change": "Changed when resuming"}.get(change.get("reason"),
                    change.get("reason") or change.get("action") or "Policy changed")))
    decisions = [one for one in (cycle.get("node_approvals") or {}).values()
                 if one.get("status") != "pending" and (steps.get(one.get("step")) or {}).get("status") != "skipped"]
    if decisions:
        content += '<h3>NODE DECISIONS</h3>'
        for receipt in decisions:
            important = receipt.get("important") or any(node.get("id") == receipt.get("step") and node.get("important")
                                                        for node in (cycle.get("graph") or {}).get("nodes", []))
            content += '<p>%s · %s · %s mode · execution %s</p>' % tuple(_text(value) for value in (
                ("! " if important else "") + (receipt.get("label") or receipt.get("step")), str(receipt.get("status") or "").capitalize(),
                str(receipt.get("mode") or "").capitalize(), receipt.get("execution_generation", 1)))
    if brief:
        git = brief.get("git") or {}
        workspace = brief.get("workspace") or {}
        content += '<h3>TASK BRIEF</h3><p>%s<br>%s · %s<br>HEAD %s<br>%s %s</p>' % tuple(_text(value) for value in (
            workspace.get("path"), workspace.get("operation"), git.get("branch"), git.get("head"), git.get("base_ref"), git.get("base_commit")))
    content += "".join(parts[completed_start:next_start])
    css = "body {color:%s; font-size:13px;} h2 {margin:0;} h3 {color:%s; margin:6px 0;} a {color:%s;}" % (theme.TEXT, theme.NEUTRAL[700], theme.ACCENT)
    return '<html><head><style>%s</style></head><body>%s</body></html>' % (css, content)


class CurrentNode(QFrame):
    """The current step stays in view while the run's history scrolls below it."""

    opened = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("currentNode")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFocusPolicy(Qt.TabFocus)
        self.step_id = ""
        self.kicker = widgets.kicker("Current step")
        self.state = widgets.Tag()
        self.elapsed = widgets.heading("", "mono")
        self.title = widgets.elided_line("", "h2")
        self.title.setStyleSheet("font-size: 19px; font-weight: 600;")
        self.detail = widgets.elided_line("", "body")
        self.activity = widgets.elided_line("", "muted")
        for label in (self.kicker, self.state, self.elapsed, self.title, self.detail, self.activity):
            label.setTextFormat(Qt.PlainText)
            label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        policy = self.progress.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.progress.setSizePolicy(policy)
        self.progress.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(3)
        heading = widgets.row(self.kicker, self.state, None, self.elapsed)
        heading.setObjectName("currentNodeHeader")
        layout.addWidget(heading)
        layout.addWidget(self.title)
        layout.addWidget(self.detail)
        layout.addWidget(self.activity)
        layout.addWidget(self.progress)
        self.hide()

    def refresh(self, cycle, live=False, plugins=None, threshold=60, request_id=None):
        cycle = cycle or {}
        self.setVisible(bool(cycle))
        name, receipt = _current_node(cycle, live, request_id)
        self.step_id = name
        self.setCursor(Qt.PointingHandCursor if name else Qt.ArrowCursor)
        self.setToolTip("Open step details (Space)" if name else "")
        step = (cycle.get("steps") or {}).get(name) or {}
        progress = step.get("progress") or {}
        determinate = False
        parallel = len([one for one in activity.active(cycle) if one != name]) if live else 0
        extra = " · +%s running" % parallel if parallel else ""
        self.kicker.setText("CURRENT STEP" if name else "RUN STATE")
        if receipt:
            title = receipt.get("label") or activity.label(cycle, name, plugins)
            paused = cycle.get("paused", False)
            state = "Paused" if paused else "Waiting for approval" if live else "Resume to review"
            detail = "Approval declined · Continue to review again" if paused else receipt.get("importance_reason") or "Review the request below"
            pending = len(_visible_approvals(cycle))
            self.activity.setText(("Run paused" if paused else "Waiting to start") +
                                  (" · +%s awaiting approval" % (pending - 1) if pending > 1 else "") + extra)
            self.elapsed.setText("")
            variant = "warn"
        elif name:
            title = activity.label(cycle, name, plugins)
            state = "Stopping" if activity.stopping(cycle) else "Pausing" if cycle.get("pause_requested") else activity.status(step)
            self.elapsed.setText(activity.duration(activity.elapsed(step, live)))
            current, total = progress.get("current"), progress.get("total")
            count = "%s / %s %s · " % (current, total, progress.get("unit", "")) if current is not None and total is not None else ""
            fallback = "Waiting for your decision" if "Waiting" in state else "Step in progress"
            detail = count + str(progress.get("title") or step.get("message") or fallback)
            self.activity.setText(activity.activity_text(step, live, threshold) + extra)
            if (isinstance(current, (int, float)) and isinstance(total, (int, float))
                    and math.isfinite(current) and math.isfinite(total) and current >= 0 and total > 0):
                self.progress.setValue(round(min(1, current / total) * 1000))
                determinate = True
            variant = "warn" if "Waiting" in state or "Pausing" in state or "Stopping" in state else "outline"
        else:
            title = activity.header(cycle, live, plugins)
            raw = cycle.get("status")
            state = "Stopping" if activity.stopping(cycle) else "Stopped" if raw == "cancelled" else "Paused" if cycle.get("paused") else {
                "success": "Completed", "cancelled": "Stopped", "failed": "Failed", "timeout": "Timed out",
                "interrupted": "Interrupted", "error": "Error"}.get(raw, "Starting" if live else "Saved run")
            detail = "No further steps will start" if activity.stopping(cycle) else cycle.get("next_action") or ""
            self.activity.setText("")
            self.elapsed.setText("")
            variant = "warn" if activity.stopping(cycle) or cycle.get("paused") or raw == "interrupted" else "bad" if raw in ("failed", "timeout", "error") else "neutral"
        self.title.setText(title)
        self.detail.setText(detail)
        self.state.set(state, variant)
        self.progress.setVisible(determinate)
        color = theme.WARN if variant == "warn" else theme.BAD if variant == "bad" else theme.ACCENT if name else theme.DIVIDER
        css = ("QFrame#currentNode {background:%s; border:1px solid %s; border-left:3px solid %s;}"
               "QWidget#currentNodeHeader {background:transparent;}") % (theme.SURFACE, theme.DIVIDER, color)
        if self.styleSheet() != css:
            self.setStyleSheet(css)
        self.setAccessibleName("%s: %s. %s. %s" % (self.kicker.text(), title, state, detail))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.step_id and self.rect().contains(event.position().toPoint()):
            self.opened.emit(self.step_id)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Space and self.step_id:
            self.opened.emit(self.step_id)
            event.accept()
        else:
            super().keyPressEvent(event)


class ApprovalActions(QWidget):
    answered = Signal(str, str, bool)

    def __init__(self, receipt, parent=None):
        super().__init__(parent)
        self.request_id, self.fingerprint = receipt["id"], receipt["fingerprint"]
        self.label = widgets.elided_line(receipt.get("label") or receipt.get("step"), "body")
        self.label.setTextFormat(Qt.PlainText)
        self.approve_button = QPushButton("Approve")
        self.approve_button.setProperty("variant", "primary")
        self.approve_button.setToolTip("Approve this step with the inputs shown above (Enter).")
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setToolTip("Decline this approval and pause the run (Esc). This step will stay pending.")
        for button, decision in ((self.approve_button, True), (self.cancel_button, False)):
            button.setAutoDefault(False)
            button.setDefault(False)
            button.clicked.connect(lambda _checked=False, approved=decision: self._answer(approved))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(widgets.row(self.label, None, self.approve_button, self.cancel_button))

    def _answer(self, approved):
        self.setEnabled(False)
        self.answered.emit(self.request_id, self.fingerprint, approved)


class QuestionActions(QWidget):
    """A question a step asks in a window of its own, and the way to that window."""

    shown = Signal(str)

    def __init__(self, question, label, parent=None):
        super().__init__(parent)
        self.request_id = str(question.get("id"))
        self.label = widgets.elided_line(label, "body")
        self.label.setTextFormat(Qt.PlainText)
        self.show_button = QPushButton("Show the question")
        self.show_button.setProperty("variant", "primary")
        self.show_button.setToolTip("Bring the question's window to the front - it is answered there.")
        self.show_button.setAutoDefault(False)
        self.show_button.clicked.connect(lambda: self.shown.emit(self.request_id))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(widgets.row(self.label, None, self.show_button))


class Cockpit(QWidget):
    node_selected = Signal(str)
    approval_answered = Signal(str, str, bool)
    #: request id: bring that question's window to the front.
    question_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        self.current = CurrentNode()
        self.current.opened.connect(self.node_selected.emit)
        layout.addWidget(self.current)
        self.browser = QTextBrowser()
        self.browser.setOpenLinks(False)
        self.browser.anchorClicked.connect(self._clicked)
        self.setFocusProxy(self.browser)
        layout.addWidget(self.browser)
        self.approval_actions = {}
        self.actions = QScrollArea()
        self.actions.setWidgetResizable(True)
        self.actions.setFrameShape(QFrame.NoFrame)
        self.actions.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.actions_body = QWidget()
        self.actions_layout = QVBoxLayout(self.actions_body)
        self.actions_layout.setContentsMargins(0, 0, 0, 0)
        self.actions_layout.setSpacing(6)
        # The questions' rows first, the approvals' after them: approvals are
        # taken out and put back on every change, and stay below this box.
        self.questions_box = QWidget()
        self.questions_layout = QVBoxLayout(self.questions_box)
        self.questions_layout.setContentsMargins(0, 0, 0, 0)
        self.questions_layout.setSpacing(6)
        self.actions_layout.addWidget(self.questions_box)
        self.question_actions = {}
        self._questions = []
        self._questions_context = None
        self.actions.setWidget(self.actions_body)
        self.actions.hide()
        layout.addWidget(self.actions)
        self._approval_context = None
        self._html = None
        self._render_context = None
        self._details_run = None
        self._expanded_details = set()
        self._current_request = None
        QApplication.instance().focusChanged.connect(self._focus_changed)
        self._approval_shortcuts = []
        for key, approved in ((Qt.Key_Return, True), (Qt.Key_Enter, True), (Qt.Key_Escape, False)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.setAutoRepeat(False)
            shortcut.setEnabled(False)
            shortcut.activated.connect(lambda decision=approved: self._answer_shortcut(decision))
            self._approval_shortcuts.append(shortcut)

    def _focus_changed(self, _previous, focus):
        if focus is None or not self._render_context:
            return
        actions = next((one for one in self.approval_actions.values() if one.isAncestorOf(focus)), None)
        if actions is not None:
            self._current_request = actions.request_id
            self.current.refresh(*self._render_context, request_id=self._current_request)

    def _answer_shortcut(self, approved):
        focus = QApplication.focusWidget()
        actions = next((one for one in self.approval_actions.values()
                        if focus is one or (focus is not None and one.isAncestorOf(focus))), None)
        if actions is None and len(self.approval_actions) == 1:
            actions = next(iter(self.approval_actions.values()))
        if actions is not None:
            (actions.approve_button if approved else actions.cancel_button).click()

    def _clicked(self, url):
        value = url.toString()
        if value.startswith("node:"):
            self.node_selected.emit(unquote(value[5:]))
        elif value.startswith("details:") and self._render_context:
            cycle = self._render_context[0] or {}
            receipt = (cycle.get("node_approvals") or {}).get(unquote(value[8:]))
            if receipt and receipt.get("status") == "pending":
                key = cycleapproval.binding(receipt)
                self._expanded_details.symmetric_difference_update({key})
                self.refresh(*self._render_context)

    def set_questions(self, questions):
        """The questions standing in windows of their own (``AskBridge.waiting``)."""
        self._questions = [dict(one) for one in questions or ()]
        if self._render_context:
            self.refresh(*self._render_context)

    def refresh(self, cycle, live=False, plugins=None, threshold=60):
        self._render_context = (cycle, live, plugins, threshold)
        run_id = (cycle or {}).get("run_id")
        position = _reading_position(self.browser) if run_id == self._details_run else None
        if run_id != self._details_run:
            self._expanded_details.clear()
            self._current_request = None
            self._details_run = run_id
        pending = {cycleapproval.binding(one) for one in _visible_approvals(cycle or {})}
        self._expanded_details.intersection_update(pending)
        value = render(cycle, live, plugins, threshold, self._expanded_details, include_current=False,
                       questions=self._questions)
        updates = self.browser.updatesEnabled()
        self.browser.setUpdatesEnabled(False)
        try:
            if value != self._html:
                self.browser.setHtml(value)
                self._html = value
            self.current.refresh(cycle, live, plugins, threshold, self._current_request)
            self._refresh_approvals(cycle or {}, live)
            self._refresh_questions(cycle or {}, live, plugins)
            # Removing the buttons changes the reader's viewport and scroll range.
            self.layout().activate()
            _restore_reading_position(self.browser, position)
        finally:
            self.browser.setUpdatesEnabled(updates)

    def _refresh_approvals(self, cycle, live):
        receipts = _visible_approvals(cycle) if live else []
        context = (cycle.get("run_id"), tuple((one["id"], one.get("fingerprint")) for one in receipts))
        if context != self._approval_context:
            previous = self.approval_actions
            same_run = self._approval_context is not None and context[0] == self._approval_context[0]
            current = {}
            for receipt in receipts:
                actions = previous.get(receipt["id"]) if same_run else None
                if actions is None or actions.fingerprint != receipt["fingerprint"]:
                    actions = ApprovalActions(receipt)
                    actions.answered.connect(self.approval_answered.emit)
                current[receipt["id"]] = actions
            for actions in previous.values():
                self.actions_layout.removeWidget(actions)
                if actions not in current.values():
                    actions.setParent(None)
                    actions.deleteLater()
            for actions in current.values():
                self.actions_layout.addWidget(actions)
                # Shown now, not when the event loop gets to it: a row not shown
                # yet counts for nothing in the height measured just below.
                actions.show()
            self.approval_actions = current
            self._approval_context = context
            self._fit_actions()
        if cycle.get("control_error"):
            for actions in self.approval_actions.values():
                actions.setEnabled(True)
        self.actions.setVisible(bool(receipts) or bool(self.question_actions))
        for shortcut in self._approval_shortcuts:
            shortcut.setEnabled(bool(receipts))

    def _refresh_questions(self, cycle, live, plugins=None):
        """A row with Show the question for each one standing, while the run is live."""
        asked = self._questions if live and cycle else []
        context = tuple((one.get("id"), one.get("step")) for one in asked)
        if context != self._questions_context:
            for actions in self.question_actions.values():
                self.questions_layout.removeWidget(actions)
                actions.setParent(None)
                actions.deleteLater()
            self.question_actions = {}
            for one in asked:
                label = activity.label(cycle, one.get("step"), plugins) if one.get("step") else "Question"
                actions = QuestionActions(one, label)
                actions.shown.connect(self.question_requested.emit)
                self.questions_layout.addWidget(actions)
                actions.show()
                self.question_actions[actions.request_id] = actions
            self._questions_context = context
            self.questions_box.setVisible(bool(asked))
            self._fit_actions()
        self.actions.setVisible(bool(asked) or bool(self.approval_actions))

    def _fit_actions(self):
        """The buttons' area as tall as its rows, up to a limit past which it scrolls.

        Measured polished: a Cockpit filled while not on screen - a tab not
        chosen yet - measured its buttons without the stylesheet's padding and
        kept a box too short for them once they had it.
        """
        self.actions_body.ensurePolished()
        self.actions_body.layout().activate()
        self.actions.setFixedHeight(min(160, self.actions_body.sizeHint().height() + 2))

    def showEvent(self, event):                # noqa: N802 - Qt's own spelling
        super().showEvent(event)
        self._fit_actions()


class ActiveStrip(QPushButton):
    opened = Signal()

    def __init__(self, state, settings, parent=None, navigation_needed=None):
        super().__init__(parent)
        self.state, self.settings = state, settings
        self.navigation_needed = navigation_needed or (lambda: True)
        self.setProperty("variant", "ghost")
        self.setCursor(Qt.PointingHandCursor)
        self.clicked.connect(self.opened.emit)
        self.summary = widgets.elided_line("", "body")
        self.summary.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 5, 12, 5)
        layout.addWidget(self.summary)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        state.changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        cycle = self.state.cycle or {}
        live = self.state.cycle_running
        visible = live and self.navigation_needed()
        self.setVisible(visible)
        if not visible:
            return
        subject = cycle.get("subject") or {}
        step = (cycle.get("steps") or {}).get(activity.primary(cycle), {})
        title = " · ".join(str(value) for value in (subject.get("key") or cycle.get("name"),
            "Mode: " + (cycle.get("execution_mode") or "auto").capitalize(), cycle.get("current_phase"), activity.header(cycle, True)) if value)
        detail = "" if cycle.get("paused") else activity.activity_text(step, True, self.settings.cycle_inactivity_seconds)
        self.summary.setText(title + (" · " + detail if detail else ""))
        self.setAccessibleName(title)
        self.setToolTip(title + ("\n" + detail if detail else "") + "\nOpen the current cycle run")
        # Sub-day parallel timers still require the faster cadence.
        values = [activity.elapsed(cycle["steps"][name], True) or 0 for name in activity.active(cycle)]
        self.timer.setInterval(60000 if values and min(values) >= 86400000 else 1000)

