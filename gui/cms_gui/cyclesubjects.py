"""The Subjects sidebar: what the cycles have been working on, one row per session.

A development cycle's run is about one task, and the next run may be about the
same task - a resume, the plan sent back, a second attempt a week later - or
about the next one in the queue. From the canvas alone those all look the same:
a graph that went green, or did not. So this lists them the way somebody thinks
about them, like conversations they can go back to::

    + New run
      Step "Take one task" picks the task.
    QA-934  Fix login                                       [bin]
      Development - waiting for approval - 10:42
    QA-703  Export CSV                                      [bin]
      Review and fix - committed - 23 Sep 17:05

It sits to the right of the canvas, where the step Inspector used to be, so
what is being worked on stays in sight while the graph runs. Clicking a row
puts that session's latest run back on the canvas, so Resume, Run step and
Run from here act on it. The pane under the list says what the run is about, how
it began - fresh, resumed, a revision round - what it kept and what it is
doing again and why, and what the cycle remembers about the subject.

Nothing here asks the core anything. The page hands in the sessions
(``--cycle-sessions``) and the live run (``RunState.cycle``), and this turns
them into rows and a page of text; every action goes back out as a signal.
The text is built by plain functions so it can be tested without a window.
"""

import html
import time

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QPushButton,
                               QSplitter, QTextBrowser, QVBoxLayout, QWidget)

from . import icons, theme, widgets

#: The id the "start something new" row carries, which no session can have:
#: every session id has a colon in it.
NEW_RUN = "new"
#: The row id of an entry on the plan: "plan:" and the core's own id.
PLAN_PREFIX = "plan:"
#: The two kinds the core puts on the plan (cycle/planner.py).
APPROVAL = "approval"


def plan_id(item):
    return PLAN_PREFIX + str(item.get("id") or "%s:%s" % (item.get("cycle", ""),
                                                           item.get("key", "")))


def planned_title(item):
    if item.get("kind") == APPROVAL:
        return "%s  waiting for your approval" % (item.get("key") or item.get("cycle", ""))
    return "%s  %s" % (item.get("key", ""), item.get("title", "")) if item.get(
        "title") else item.get("key", "")


def planned_detail(item, cycle_id=""):
    when = time.strftime("%d %b %H:%M", time.localtime(item.get("at") or 0))
    where = "" if item.get("cycle") == cycle_id else "%s - " % item.get("cycle", "")
    if item.get("kind") == APPROVAL:
        return "%snobody answered %s - resume to be asked again" % (where, when)
    return "%splanned %s - waiting for you" % (where, when)


def start_label(item):
    return "Resume" if item.get("kind") == APPROVAL else "Start work"


def planned_html(item):
    """The pane under the list, for an entry on the plan."""
    if item.get("kind") == APPROVAL:
        said = ("A run of %s stopped here because nobody answered it: <i>%s</i>. "
                "Nothing after it was done. Resume continues that run and asks "
                "again; the bin takes it off the plan and leaves the run as it is."
                % (html.escape(item.get("cycle", "")),
                   html.escape(item.get("question", ""))))
    else:
        said = ("A new task the cycle %s found while listening, kept for you to "
                "look at. Start work runs the cycle on it; the bin takes it off "
                "the plan." % html.escape(item.get("cycle", "")))
    rows = ["<p><b>%s</b></p>" % html.escape(planned_title(item)),
            "<p style='color:%s'>%s</p>" % (theme.NEUTRAL[600], said),
            "<p>%s</p>" % html.escape(planned_detail(item))]
    if item.get("url"):
        rows.append("<p><a href='%s'>%s</a></p>" % (html.escape(item["url"], True),
                                                   html.escape(item["url"])))
    return "".join(rows)

#: Everything the sidebar looks like, in one place - the habit
#: ``cyclegraph.py`` and ``stages.py`` keep.
SPEC = {
    "width": 300,
    "list_height": 260,
    "details_height": 240,
    "row": {"pad_x": 8, "pad_y": 5, "gap": 1, "title": 12, "detail": 11},
    "delete_size": 22,
}

#: How a finished run's status reads in a list, where "success" is jargon.
STATUS_WORDS = {
    "success": "passed",
    "failed": "stopped at %s",
    "timeout": "timed out at %s",
    "cancelled": "stopped at %s",
    "interrupted": "interrupted at %s",
    "error": "could not start",
}

#: Why each kind of run began, as a reader would say it.
MODE_WORDS = {
    "fresh": "A new run.",
    "resume": "Resumed - attempt %d of this run.",
    "partial": "Part of the cycle, the rest taken from run %s.",
}

#: The memory fields worth a line of their own in a status, and what they say.
REMEMBERED_STATES = {"committed": "committed"}

ROLE_SESSION = Qt.UserRole


# -- words --------------------------------------------------------------------
def when(timestamp, now=None):
    """A time as a list says it: the hour today, the date before that."""
    if not timestamp:
        return ""
    now = time.time() if now is None else now
    then = time.localtime(timestamp)
    if time.strftime("%Y%m%d", then) == time.strftime("%Y%m%d", time.localtime(now)):
        return time.strftime("%H:%M", then)
    return time.strftime("%d %b %H:%M", then)


def status_word(session):
    """Where one session is, in a few words: "waiting for approval"."""
    state = session.get("status") or ""
    reached = session.get("reached_label") or session.get("reached") or ""
    if session.get("running"):
        if session.get("reached_plugin") == "approval.gate":
            return "waiting for approval"
        return "running - %s" % reached if reached else "running"
    remembered = ((session.get("memory") or {}).get("fields") or {}).get("state")
    if remembered in REMEMBERED_STATES:
        return REMEMBERED_STATES[remembered]
    word = STATUS_WORDS.get(state, state or "unknown")
    if "%s" in word:
        return word % reached if reached else word.replace(" at %s", "")
    return word


def status_colour(session):
    """The ink a status is written in, read at paint time."""
    if session.get("running"):
        return theme.ACCENT
    state = session.get("status")
    remembered = ((session.get("memory") or {}).get("fields") or {}).get("state")
    if state == "success" or remembered in REMEMBERED_STATES:
        return theme.OK
    if state in ("failed", "timeout", "error"):
        return theme.BAD
    return theme.WARN


def subject_title(session):
    """What a row is called: its key and title, or the run it stands for."""
    subject = session.get("subject") or {}
    if subject.get("key"):
        return ("%s  %s" % (subject["key"], subject["title"])
                if subject.get("title") else subject["key"])
    return "Run %s" % when(session.get("started_at"))


def row_detail(session, open_cycle=""):
    """The second line of a row: where it is, and anything the title lacks.

    The cycle is named only when it is not the one open - every row of the
    open cycle saying so pushed the status off the end of a narrow column -
    and the time only when the title does not already carry it.
    """
    parts = []
    if session.get("cycle") != open_cycle:
        parts.append(session.get("name") or session.get("cycle") or "")
    parts.append(status_word(session))
    if (session.get("subject") or {}).get("key"):
        parts.append(when(session.get("updated_at")))
    return parts


def picks(graph_subject, labels):
    """Who decides a cycle's subject, as a sentence. "" when it declares none."""
    if not graph_subject:
        return ""
    sources = [labels.get(one) or one for one in graph_subject.get("sources") or []]
    kind = graph_subject.get("kind") or "subject"
    if not sources:
        return "The run decides the %s." % kind
    return "Step %s picks the %s." % (" and ".join('"%s"' % one for one in sources),
                                     kind)


def new_run_text(graph_subject, labels):
    """What the "+ New run" row says will happen."""
    if not graph_subject:
        return "Each run of this cycle stands on its own."
    return picks(graph_subject, labels)


def _escape(text):
    return html.escape(str(text if text is not None else ""))


def details_html(session, live=None, graph_subject=None, labels=None):
    """The pane under the list, for one session (or None for "+ New run").

    ``live`` is ``RunState.cycle`` when the run on screen belongs to this
    session - it knows how the run began and what it is doing again, which the
    run index does not. ``graph_subject`` is the open cycle's declaration.
    """
    labels = labels or {}
    named = lambda step: _escape(labels.get(step) or step)  # noqa: E731
    parts = []
    if session is None:
        parts.append("<p><b>Start a new run.</b></p>")
        parts.append("<p>%s</p>" % _escape(new_run_text(graph_subject, labels)))
        return "".join(parts)

    subject = dict(session.get("subject") or {})
    live_subject = (live or {}).get("subject") or {}
    for key, value in live_subject.items():
        if value:
            subject[key] = value
    if subject.get("key"):
        parts.append("<p>Working on %s <b>%s</b>%s</p>" % (
            _escape(subject.get("kind") or ""), _escape(subject["key"]),
            " - %s" % _escape(subject["title"]) if subject.get("title") else ""))
        if subject.get("step"):
            parts.append("<p style='color:%s'>Decided by %s.</p>"
                         % (theme.NEUTRAL[500], named(subject["step"])))
    elif graph_subject:
        parts.append("<p>Not decided yet. %s</p>"
                     % _escape(picks(graph_subject, labels)))
    else:
        parts.append("<p>This run stands on its own - the cycle does not say "
                     "what it works on.</p>")

    parts.append("<p><span style='color:%s'><b>%s</b></span></p>" % (
        status_colour(session), _escape(status_word(session))))
    if graph_subject is None:
        # Another cycle's session: say whose, since the canvas shows a
        # different one until the row is opened.
        parts.append("<p style='color:%s'>%s</p>" % (
            theme.NEUTRAL[500],
            _escape(session.get("name") or session.get("cycle") or "")))
    if session.get("message") and session.get("status") not in ("success",):
        parts.append("<p style='color:%s'>%s</p>"
                     % (theme.NEUTRAL[600], _escape(session["message"])))

    mode = (live or {}).get("mode") or {}
    if mode.get("mode"):
        words = MODE_WORDS.get(mode["mode"], mode["mode"])
        if mode["mode"] == "resume":
            words = words % ((mode.get("resume_count") or 0) + 1)
        elif mode["mode"] == "partial":
            words = words % (mode.get("source_run") or "an earlier run")
        parts.append("<h4>This run</h4><p>%s</p>" % _escape(words))
        if mode.get("kept"):
            parts.append("<p>Kept from before: %s.</p>"
                         % ", ".join(named(one) for one in mode["kept"]))
        if mode.get("rerun"):
            parts.append("<p>Doing again:</p><ul>%s</ul>" % "".join(
                "<li>%s - %s</li>" % (named(one.get("step")),
                                      _escape(one.get("reason") or ""))
                for one in mode["rerun"]))
    for round_ in (live or {}).get("revisions") or []:
        parts.append("<p>Plan sent back (round %s): <i>%s</i></p>" % (
            _escape(round_.get("number")), _escape(round_.get("feedback"))))

    remembered = session.get("memory") or {}
    if remembered.get("key"):
        parts.append("<h4>What the cycle remembers</h4>")
        rows = "".join("<tr><td>%s</td><td>&nbsp;&nbsp;%s</td></tr>"
                       % (_escape(name), _escape(value))
                       for name, value in sorted((remembered.get("fields") or {}).items()))
        parts.append("<p style='color:%s'>%s%s</p>" % (
            theme.NEUTRAL[500], _escape(remembered["key"]),
            ", updated %s" % _escape(when(remembered.get("updated_at")))
            if remembered.get("updated_at") else ""))
        if rows:
            parts.append("<table>%s</table>" % rows)
        claim = remembered.get("claim") or {}
        if claim.get("owner") and (claim.get("until") or 0) > time.time():
            parts.append("<p>Held by run %s until %s.</p>" % (
                _escape(claim["owner"]), _escape(when(claim.get("until")))))

    runs = session.get("runs") or []
    if runs:
        parts.append("<h4>Runs</h4><ul>%s</ul>" % "".join(
            "<li><a href='run:%s'>%s</a> - %s%s</li>" % (
                _escape(one.get("id")), _escape(when(one.get("started_at"))),
                _escape(status_word(dict(one, status=one.get("state"),
                                         memory={}))),
                " (resumed %d)" % one["resume_count"]
                if one.get("resume_count") else "")
            for one in runs))
    return "".join(parts)


# -- the sidebar --------------------------------------------------------------
class _Row(QWidget):
    """One session: what it is about on top, where it is underneath, and a bin."""

    def __init__(self, title, detail, colour, deletable=None, parent=None):
        super().__init__(parent)
        look = SPEC["row"]
        layout = QHBoxLayout(self)
        layout.setContentsMargins(look["pad_x"], look["pad_y"], 4, look["pad_y"])
        layout.setSpacing(6)
        text = QVBoxLayout()
        text.setSpacing(look["gap"])
        self.title = QLabel(title)
        self.title.setStyleSheet("font-size: %dpx; font-weight: 600;"
                                 % look["title"])
        self.title.setToolTip(title)
        self.detail = QLabel(detail)
        self.detail.setStyleSheet("font-size: %dpx; color: %s;"
                                  % (look["detail"], colour))
        self.detail.setToolTip(detail)
        text.addWidget(self.title)
        text.addWidget(self.detail)
        layout.addLayout(text, 1)
        self.bin = None
        if deletable is not None:
            # A ghost push button, like the canvas's zoom buttons: a bare
            # tool button drew its frame and not the icon inside it.
            self.bin = icons.button(QPushButton(), "delete")
            self.bin.setProperty("variant", "ghost")
            self.bin.setFixedSize(SPEC["delete_size"], SPEC["delete_size"])
            self.bin.setToolTip("Delete this session: its runs, their reports, "
                                "and what the cycle remembers about it.")
            self.bin.setEnabled(bool(deletable))
            layout.addWidget(self.bin, 0, Qt.AlignVCenter)


class SubjectsPanel(QWidget):
    """The list of sessions, and the page about the one picked."""

    #: A session was picked: show its latest run. Carries the session.
    activated = Signal(dict)
    #: One run of a session was picked from the pane: (cycle id, run id).
    run_picked = Signal(str, str)
    new_requested = Signal()
    delete_requested = Signal(dict)
    #: A planned task: start work on it, or take it off the plan.
    planned_start = Signal(dict)
    planned_remove = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sessions = []
        self._planned = []
        self._cycle = ""
        self._graph_subject = None
        self._labels = {}
        self._live = None
        self._picked = ""                # a session id, or NEW_RUN, or ""
        self._busy = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(widgets.kicker("Subjects"))

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setProperty("role", "panel")
        self.list.itemClicked.connect(self._clicked)

        self.details = QTextBrowser()
        self.details.setOpenLinks(False)
        self.details.setOpenExternalLinks(False)
        self.details.document().setDocumentMargin(10)
        self.details.setStyleSheet("font-size: 12px;")
        self.details.anchorClicked.connect(self._link)
        self.new_button = icons.button(QPushButton(), "run", "Start")
        self.new_button.setProperty("variant", "primary")
        self.new_button.clicked.connect(self._start)
        below = QWidget()
        below_layout = QVBoxLayout(below)
        below_layout.setContentsMargins(0, 0, 0, 0)
        below_layout.setSpacing(6)
        below_layout.addWidget(self.details, 1)
        below_layout.addWidget(widgets.row(None, self.new_button))

        split = QSplitter(Qt.Vertical)
        split.addWidget(self.list)
        split.addWidget(below)
        split.setStretchFactor(0, 1)
        split.setSizes([SPEC["list_height"], SPEC["details_height"]])
        layout.addWidget(split, 1)
        self._rebuild()

    # -- what the page hands in -----------------------------------------------
    def set_cycle(self, cycle_id, graph):
        """The open cycle: its rows come first, and it is what "+ New run" runs."""
        graph = graph or {}
        self._cycle = cycle_id or ""
        self._graph_subject = graph.get("subject")
        self._labels = {node.get("id"): node.get("label") or node.get("id")
                        for node in graph.get("nodes") or [] if node.get("id")}
        self._rebuild()

    def set_sessions(self, sessions):
        self._sessions = [one for one in sessions or [] if isinstance(one, dict)]
        self.set_live(self._live)

    def set_planned(self, planned):
        """Tasks kept for later, across cycles. The open cycle's come first."""
        self._planned = [one for one in planned or [] if isinstance(one, dict)]
        if self._picked.startswith(PLAN_PREFIX) and self.planned_item() is None:
            self._picked = ""
        self._rebuild()

    def planned_item(self):
        """The planned task picked, or None."""
        for one in self._planned:
            if plan_id(one) == self._picked:
                return one
        return None

    def set_live(self, live):
        """The run on screen. Its session becomes the picked row."""
        self._live = live or None
        found = self.session_of_run((live or {}).get("run_id", ""))
        if found is not None:
            self._picked = found["id"]
        elif live and (live.get("subject") or {}).get("key"):
            self._picked = "%s:%s" % (live.get("cycle", ""), live["subject"]["key"])
        self._rebuild()

    def set_busy(self, busy):
        """A cycle is running: nothing that starts or deletes one is offered."""
        if bool(busy) != self._busy:
            self._busy = bool(busy)
            self._rebuild()

    # -- asking ---------------------------------------------------------------
    def sessions(self):
        return list(self._sessions)

    def session(self, session_id):
        for one in self._sessions:
            if one.get("id") == session_id:
                return one
        return None

    def session_of_run(self, run_id):
        if not run_id:
            return None
        for one in self._sessions:
            if any(run.get("id") == run_id for run in one.get("runs") or []):
                return one
        return None

    def picked(self):
        """The session picked, or None - for "+ New run" or nothing at all."""
        return self.session(self._picked)

    def ordered(self):
        """The open cycle's sessions first, then the rest; newest first in each."""
        mine = [one for one in self._sessions if one.get("cycle") == self._cycle]
        rest = [one for one in self._sessions if one.get("cycle") != self._cycle]
        return mine + rest

    def row_ids(self):
        """What each row stands for, top to bottom. For tests and nothing else."""
        return [self.list.item(index).data(ROLE_SESSION)
                for index in range(self.list.count())]

    # -- drawing --------------------------------------------------------------
    def _add(self, key, row):
        item = QListWidgetItem()
        item.setData(ROLE_SESSION, key)
        item.setSizeHint(QSize(0, row.sizeHint().height()))
        self.list.addItem(item)
        self.list.setItemWidget(item, row)
        if key == self._picked:
            item.setSelected(True)
        return item

    def _rebuild(self):
        self.list.blockSignals(True)
        self.list.clear()
        if self._cycle:
            self._add(NEW_RUN, _Row("+ New run", new_run_text(
                self._graph_subject, self._labels), theme.NEUTRAL[500]))
        ordered = ([one for one in self._planned if one.get("cycle") == self._cycle]
                   + [one for one in self._planned if one.get("cycle") != self._cycle])
        for item in ordered:
            row = _Row(planned_title(item), planned_detail(item, self._cycle),
                       theme.ACCENT, deletable=not self._busy)
            row.bin.setToolTip("Take it off the plan." if item.get("kind") == APPROVAL
                               else "Take it off the plan. It is not offered again.")
            row.bin.clicked.connect(lambda _checked=False, one=item:
                                    self.planned_remove.emit(one))
            self._add(plan_id(item), row)
        for session in self.ordered():
            detail = " - ".join(part for part in row_detail(session, self._cycle)
                                if part)
            row = _Row(subject_title(session), detail, status_colour(session),
                       deletable=not session.get("running") and not self._busy)
            row.bin.clicked.connect(lambda _checked=False, one=session:
                                    self.delete_requested.emit(one))
            self._add(session.get("id"), row)
        self.list.blockSignals(False)
        self._paint_details()

    def _paint_details(self):
        picked = self.picked()
        live = self._live
        if picked is not None and live and not any(
                run.get("id") == live.get("run_id") for run in picked.get("runs") or []):
            # The run on screen belongs to another session - unless it is this
            # subject's new run, which the list has not caught up with yet.
            if (live.get("subject") or {}).get("key") != (
                    picked.get("subject") or {}).get("key"):
                live = None
        mine = picked is None or picked.get("cycle") == self._cycle
        planned = self.planned_item()
        if planned is not None:
            self.details.setHtml(planned_html(planned))
            self.new_button.setText(start_label(planned))
            self.new_button.setVisible(True)
            self.new_button.setEnabled(not self._busy)
            return
        self.new_button.setText("Start")
        if picked is None and self._picked != NEW_RUN:
            self.details.setHtml(
                "<p style='color:%s'>%s</p>" % (
                    theme.NEUTRAL[500],
                    "Pick a row to see what it worked on."
                    if self._sessions else "Nothing has run yet."))
        else:
            self.details.setHtml(details_html(
                picked, live, self._graph_subject if mine else None,
                self._labels if mine else {}))
        self.new_button.setVisible(self._picked == NEW_RUN)
        self.new_button.setEnabled(not self._busy and bool(self._cycle))

    # -- what a person does ---------------------------------------------------
    def _start(self):
        planned = self.planned_item()
        if planned is not None:
            self.planned_start.emit(planned)
        else:
            self.new_requested.emit()

    def _clicked(self, item):
        picked = item.data(ROLE_SESSION) or ""
        self._picked = picked
        self._paint_details()
        session = self.session(picked)
        if session is not None:
            self.activated.emit(session)

    def _link(self, url):
        target = url.toString()
        picked = self.picked()
        if target.startswith("run:") and picked is not None:
            self.run_picked.emit(picked.get("cycle", ""), target[len("run:"):])
        elif target.startswith(("http://", "https://")):
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(url)
