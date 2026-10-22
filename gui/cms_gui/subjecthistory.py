"""How a subject reads: its row on the Subjects page, and its story inside it.

Plain functions over what the core answered - ``--cycle-subject-list`` for a
row, ``--cycle-subject-history`` for what is shown when the row is opened - so
every word, every colour and every link can be tested without a window, the
same arrangement ``cyclesubjects.details_html`` has.

**Links are a table, not a URL.** A link in the history opens a file, unfolds
an agent's answer, or asks for what came earlier. Spelling a run directory and
a path into an href would mean escaping both and parsing them back; instead
each builder returns ``(html, actions)``, every href is ``a:<n>``, and the n-th
action says what the click does. The page looks it up; nothing is parsed.
"""

import html
import time

from . import cyclesubjects, theme

#: The short word in front of each entry: who or what it is about.
RUN, YOU, ASKED, AGENT, CHECK, FAILED, STEP = (
    "RUN", "YOU", "ASKED", "AGENT", "CHECK", "FAILED", "STEP")

#: Which tab of an opened row shows what.
HISTORY, FILES, RUNS, COCKPIT_TAB, STAGES = "History", "Files", "Runs", "Cockpit", "Stages"
TABS = (HISTORY, FILES, RUNS, COCKPIT_TAB, STAGES)
#: The tabs that show one run, picked beside them: the live one as it goes,
#: any other as it recorded itself.
RUN_TABS = (COCKPIT_TAB, STAGES)
#: The two views of a history, as the core names them.
CONVERSATION, ALL = "conversation", "all"
SCOPE_LABELS = {CONVERSATION: "Conversation", ALL: "All"}

#: How much of a long answer an entry shows before "answer" unfolds the rest.
SNIPPET = 220
#: The time and tag columns of a history, in pixels: fixed, so every entry's
#: text starts on one line down the page whatever its tag.
TIME_WIDTH = 104
TAG_WIDTH = 62
#: How links read in the history, with the accent of the theme in force.
LINK_CSS = "a { color: %s; text-decoration: none; font-weight: 600; }"

#: What a run stopped for, as the run's own pause reasons name it.
PAUSE_WORDS = {
    "phase_boundary": "Paused after a phase - Continue runs the next one",
    "approval_declined": "Paused - a node approval was declined",
    "approval_required": "Paused - a node waits for approval",
    "user_request": "Paused by you",
    "phase_failed": "Paused - a step of this phase failed",
    "manual_phase": "Paused - the next phase starts when you run it",
}

#: The states the core reports, as the State column says them.
STATE_WORDS = {
    "asking": "Waiting: %s",
    "node_approval": "Waiting: node approval",
    "planned": "Waiting: %s - not answered",
    "parked": "Parked - resume at %s",
    "pausing": "Pausing",
}


def _escape(text):
    return html.escape(str(text if text is not None else ""))


def _first_line(text, limit=SNIPPET):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


# -- the row ------------------------------------------------------------------
def title(subject):
    """What the Subject column says: the key and title, or the run it stands for."""
    if subject.get("key"):
        return ("%s  %s" % (subject["key"], subject["title"])
                if subject.get("title") else subject["key"])
    session = subject.get("session") or {}
    if session:
        return cyclesubjects.subject_title(session)
    return subject.get("id") or ""


def state_text(subject):
    """Where a subject stands, in the words of the State column."""
    state = subject.get("state") or ""
    latest = subject.get("latest") or {}
    session = subject.get("session") or {}
    reached = latest.get("reached_label") or latest.get("reached") or ""
    if not latest and not session:
        return "Notes only" if subject.get("notes") else ""
    if state == "asking":
        return STATE_WORDS[state] % (reached or "a question")
    if state == "planned":
        return STATE_WORDS[state] % (reached or "an approval")
    if state == "parked":
        where = subject.get("resume_at") or ""
        return (STATE_WORDS[state] % where) if where else "Parked - Resume continues it"
    if state == "paused":
        return "Paused - %s" % (latest.get("next_action") or session.get("next_action")
                                or "Continue to go on")
    if state in STATE_WORDS:
        return STATE_WORDS[state]
    if state == "running":
        return "Running: %s" % reached if reached else "Running"
    word = cyclesubjects.status_word(session) if session else state
    return word[:1].upper() + word[1:] if word else ""


def state_colour(subject):
    """The ink the State column is written in, read at paint time."""
    if subject.get("needs_you"):
        return theme.WARN
    if subject.get("active"):
        return theme.ACCENT
    session = subject.get("session") or {}
    return cyclesubjects.status_colour(session) if session else theme.NEUTRAL[500]


def cycles_text(subject):
    """The cycles that worked on it, newest first."""
    return ", ".join(one.get("name") or one.get("id") or ""
                     for one in subject.get("cycles") or [])


def note_text(subject):
    """The newest note, on one line."""
    return _first_line((subject.get("note") or {}).get("text"), 120)


def meta_text(subject):
    """What an opened row says about itself: how many runs, since when."""
    runs = subject.get("runs") or 0
    parts = ["%d run%s" % (runs, "" if runs == 1 else "s")]
    if subject.get("started_at"):
        parts.append("since %s" % cyclesubjects.when(subject["started_at"]))
    return " - ".join(parts)


# -- what to do next ----------------------------------------------------------
#: The verbs an opened row can offer, and the page signal each one becomes.
OPEN, COCKPIT, RESUME = "open", "cockpit", "resume"


def next_action(subject, live_run="", busy=False):
    """``(verb, label, enabled, why)``: the one thing to do next, or None.

    ``live_run`` is the run this window is executing, ``busy`` whether it is
    executing anything at all. A question waiting in the live run is answered
    in its Cockpit - the row's own Cockpit tab, the same model as the Cycles
    page's. A run that can be continued is resumed; every other run is opened
    on its canvas.
    """
    latest = subject.get("latest") or {}
    if not latest:
        return None
    if latest.get("running") and latest.get("id") == live_run:
        if subject.get("state") in ("asking", "node_approval"):
            return COCKPIT, "Answer", True, "The run's Cockpit, here: approve, or bring its question forward."
        return OPEN, "Open run", True, ""
    offer = subject.get("resume") or {}
    if offer and not offer.get("problem"):
        if busy:
            return (RESUME, "Resume", False,
                    "Another run is going in this window. Park or stop it first.")
        return RESUME, "Resume", True, "Continue this run where it stopped."
    if busy and not latest.get("running"):
        return OPEN, "Open run", False, "A run is going in this window."
    return OPEN, "Open run", True, offer.get("problem") or ""


def can_park(subject, live_run=""):
    """Whether Park is offered: the run is this window's, and it is going."""
    latest = subject.get("latest") or {}
    return bool(latest.get("running") and latest.get("id") == live_run)


# -- putting a run aside --------------------------------------------------------
#: Steps that ask a person something. Stopped while asking, they ask again on
#: Resume - so a run waiting only on them can be parked at once.
PERSON_PLUGINS = ("approval.gate", "user.choice", "user.input")


def park_point(cycle):
    """Whether stopping the live run now loses nothing.

    ``cycle`` is the window's record of the run (``RunState.cycle``). It has
    paused, or the only steps still going are questions to a person, which
    are asked again on Resume. Anything else going - an agent mid-call - would
    lose the work it is being paid for.
    """
    if (cycle or {}).get("paused"):
        return True
    going = [one for one in ((cycle or {}).get("steps") or {}).values()
             if one.get("status") == "running"]
    return all(one.get("plugin") in PERSON_PLUGINS for one in going)


def park_lines(cycle):
    """What Park's question says will happen, one line each, in this order."""
    cycle = cycle or {}
    labels = {node.get("id"): node.get("label") or node.get("id")
              for node in (cycle.get("graph") or {}).get("nodes") or []}
    steps = cycle.get("steps") or {}
    finished = sum(1 for one in steps.values() if one.get("status") == "success")
    lines = ["%d finished step%s stay%s finished." % (
        finished, "" if finished == 1 else "s", "s" if finished == 1 else "")]
    for name, one in steps.items():
        if one.get("status") == "running" and one.get("plugin") in PERSON_PLUGINS:
            lines.append('"%s" is waiting for you - it asks again when you resume.'
                         % labels.get(name, name))
    for name, one in steps.items():
        if one.get("status") == "running" and one.get("plugin") not in PERSON_PLUGINS:
            lines.append('"%s" finishes what it is doing first.' % labels.get(name, name))
    lines.append("Nothing more is started, pushed or sent. Resume it from Subjects "
                 "whenever you come back.")
    return lines


def working(cycle):
    """The labels of the steps a park waits for: going, and not asking a person."""
    cycle = cycle or {}
    labels = {node.get("id"): node.get("label") or node.get("id")
              for node in (cycle.get("graph") or {}).get("nodes") or []}
    return [labels.get(name, name) for name, one in (cycle.get("steps") or {}).items()
            if one.get("status") == "running" and one.get("plugin") not in PERSON_PLUGINS]


# -- the history ----------------------------------------------------------------
def _when(timestamp, now=None):
    if not timestamp:
        return ""
    return time.strftime("%d %b %H:%M", time.localtime(timestamp))


def _tag(word):
    colours = {RUN: theme.NEUTRAL[600], YOU: theme.ACCENT, ASKED: theme.WARN,
               AGENT: theme.TAG_INK, CHECK: theme.NEUTRAL[600],
               FAILED: theme.BAD, STEP: theme.NEUTRAL[500]}
    return ("<span style='font-family:%s; font-weight:600; color:%s'>%s</span>"
            % (theme.MONO_CSS, colours.get(word, theme.NEUTRAL[600]), word))


def _muted(text):
    return "<span style='color:%s'>%s</span>" % (theme.NEUTRAL[500], text)


def _block(text):
    """A long text unfolded, kept to its own lines."""
    return ("<div style='margin:4px 0 6px 0; color:%s; white-space:pre-wrap'>%s</div>"
            % (theme.NEUTRAL[800], _escape(text)))


class _Links(object):
    """Collects what each link does while a page is written."""

    def __init__(self):
        self.actions = []

    def add(self, text, action):
        self.actions.append(dict(action))
        return "<a href='a:%d'>%s</a>" % (len(self.actions) - 1, _escape(text))


def run_numbers(answer):
    """run id -> "Run 2": runs numbered as they began, the first one being 1."""
    runs = sorted(answer.get("runs") or [], key=lambda one: one.get("started_at") or 0)
    return {one.get("run"): "Run %d" % (index + 1) for index, one in enumerate(runs)}


def history_html(answer, opened=(), now=None, transcripts=None, told=()):
    """The History tab: ``(html, actions)`` for one subject's story.

    ``opened`` holds the indexes of entries whose long parts are unfolded, and
    ``told`` those of agents whose own account is shown - ``transcripts`` maps
    ``(run, step)`` to what ``--cycle-subject-transcript`` answered, or None
    while it is on its way. The first line offers what came before, when the
    answer was cut short.
    """
    links = _Links()
    entries = answer.get("entries") or []
    names = run_numbers(answer)
    several = len({one.get("cycle") for one in answer.get("runs") or []}) > 1
    rows = []
    if answer.get("earlier"):
        earlier = answer["earlier"]
        rows.append("<tr><td colspan='3'>%s</td></tr>" % links.add(
            "Show %d earlier entr%s" % (earlier, "y" if earlier == 1 else "ies"),
            {"do": "earlier"}))
    if not entries:
        rows.append("<tr><td colspan='3'>%s</td></tr>" % _muted(
            "Nothing has happened here yet."))
    told, transcripts = set(told), transcripts or {}
    for index, entry in enumerate(entries):
        word, text = _entry(index, entry, index in set(opened), links, names, several)
        if entry.get("kind") == "agent" and index in told:
            text += _transcript_html(transcripts.get((entry.get("run"), entry.get("step"))),
                                     entry, links)
        rows.append(
            "<tr><td width='%d' style='white-space:nowrap; color:%s; font-family:%s'>%s</td>"
            "<td width='%d'>%s</td><td>%s</td></tr>" % (
                TIME_WIDTH, theme.NEUTRAL[500], theme.MONO_CSS,
                _escape(_when(entry.get("at"), now)), TAG_WIDTH, _tag(word), text))
    page = ("<table width='100%%' cellspacing='0' cellpadding='2'>%s</table>"
            % "".join(rows))
    return page, links.actions


def _files(entry, index, links):
    return " ".join(links.add(one.get("name") or one.get("path") or "file",
                              {"do": "file", "run_dir": entry.get("run_dir", ""),
                               "path": one.get("path", "")})
                    for one in entry.get("files") or [])


def _run_name(entry, names, several):
    name = names.get(entry.get("run"), "Run")
    if several and entry.get("cycle_name"):
        name += " (%s)" % entry["cycle_name"]
    return name


def _entry(index, entry, opened, links, names, several):
    """``(tag, html)`` for one entry."""
    kind = entry.get("kind")
    toggle = lambda text: links.add(text, {"do": "toggle", "index": index})  # noqa: E731
    label = _escape(entry.get("label") or entry.get("step") or "")
    if kind == "run.start":
        return RUN, _escape(_start_words(entry, _run_name(entry, names, several)))
    if kind == "run.end":
        return RUN, _escape(_end_words(entry, _run_name(entry, names, several)))
    if kind == "run.gone":
        return RUN, _escape("%s - its folder is no longer there"
                            % _run_name(entry, names, several))
    if kind == "control":
        return RUN, _escape(_control_words(entry))
    if kind == "revision":
        text = "Sent \"%s\" back (round %s): \"%s\"" % (
            entry.get("target_label") or entry.get("target") or "the plan",
            entry.get("number") or 1, entry.get("feedback") or "")
        return YOU, _escape(text)
    if kind == "node_approval":
        if entry.get("status") == "pending":
            return ASKED, "Node \"%s\" waits for your approval" % label
        return YOU, "%s node \"%s\"" % (_escape((entry.get("status") or "").capitalize()),
                                          label)
    if kind == "node_approvals":
        count = entry.get("count") or 0
        text = _escape("%d node%s approved" % (count, "" if count == 1 else "s"))
        text += " " + toggle("show" if not opened else "hide")
        if opened:
            text += _block("\n".join(entry.get("labels") or []))
        return STEP, text
    if kind == "message":
        target = ("the run" if entry.get("scope") == "run"
                  else "\"%s\"" % (entry.get("label") or entry.get("step") or "a step"))
        text = "To %s: \"%s\"" % (target, entry.get("text") or "")
        return YOU, _escape(text) + " " + _muted(_escape(_delivery(entry)))
    if kind == "note":
        return YOU, _escape("Note: ") + _escape(entry.get("text") or "").replace("\n", "<br>")
    if kind == "ask":
        return ASKED, _ask(entry, opened, toggle, label)
    if kind == "agent":
        return AGENT, _agent(entry, index, opened, toggle, label, links)
    if kind == "check":
        text = "%s - %s" % (label, _escape(entry.get("message") or entry.get("status") or ""))
        checks = entry.get("checks") or []
        if checks:
            text += " " + toggle("hide" if opened else "checks")
            if opened:
                text += _block("\n".join(_check_line(one) for one in checks))
        return CHECK, text
    if kind == "failed":
        text = "%s - %s" % (label, _escape(_first_line(entry.get("message") or "", MESSAGE)
                                           or entry.get("status")))
        files = _files(entry, index, links)
        return FAILED, text + ((" " + files) if files else "")
    if kind == "steps":
        count = entry.get("count") or 0
        text = _escape("%d step%s passed" % (count, "" if count == 1 else "s"))
        text += " " + toggle("hide" if opened else "show")
        if opened:
            lines = []
            for one in entry.get("steps") or []:
                files = _files(dict(entry, files=one.get("files")), index, links)
                lines.append("%s%s" % (_escape(one.get("label") or one.get("step")),
                                       (" - " + files) if files else ""))
            text += "<div style='margin:4px 0 6px 0'>%s</div>" % "<br>".join(lines)
        return STEP, text
    return STEP, "%s - %s" % (label, _escape(entry.get("status") or ""))


def _start_words(entry, name):
    mode = entry.get("mode") or ""
    if mode == "resume":
        how = "resumed (time %d)" % (entry.get("resume_count") or 1)
    elif mode == "partial":
        how = "part of the cycle, the rest from %s" % (entry.get("source_run") or "an earlier run")
    elif mode == "action":
        how = "a bounded action"
    else:
        how = "fresh"
    words = "%s started - %s" % (name, how) if mode != "resume" else "%s %s" % (name, how)
    if entry.get("execution_mode"):
        words += " - %s mode" % entry["execution_mode"].capitalize()
    return words


#: How much of a run's or a step's own message a line carries. Some are a whole
#: JSON reply; the first stretch says what happened, the rest is in its files.
MESSAGE = 160


def _end_words(entry, name):
    status = entry.get("status") or ""
    message = _first_line(entry.get("message") or "", MESSAGE)
    if message == "parked":
        return "%s parked - Resume continues it" % name
    if status == "success":
        return "%s finished" % name
    if status == "cancelled":
        return "%s stopped" % name + (": %s" % message if message and message != "interrupted"
                                      else "")
    if status == "timeout":
        return "%s timed out" % name + (": %s" % message if message else "")
    return "%s %s" % (name, status or "ended") + (": %s" % message if message else "")


def _control_words(entry):
    action = entry.get("action")
    if action == "pause":
        return PAUSE_WORDS.get(entry.get("reason") or "", "Paused")
    if action == "continue":
        return "Continued"
    if action == "run_phase":
        return "Ran the next phase"
    if action == "retry_phase":
        return "Retried the phase%s" % (" %s" % entry["phase"] if entry.get("phase") else "")
    return action or ""


def _delivery(entry):
    state = entry.get("state") or ""
    if state == "delivered" or entry.get("delivered"):
        return "- delivered"
    if state in ("rejected", "not_delivered", "uncertain"):
        reason = entry.get("reason") or ""
        return "- %s%s" % (state.replace("_", " "), (": " + reason) if reason else "")
    return "- waiting to be delivered" if state else ""


def _ask(entry, opened, toggle, label):
    question = entry.get("question") or ""
    text = label + (" - " + _escape(question) if question and question != label else "")
    options = entry.get("options") or []
    if options:
        text += "<br>" + _muted(_escape(" / ".join(str(one) for one in options)))
    status = entry.get("status") or ""
    who = entry.get("who") or ""
    if entry.get("revision"):
        answer = "Sent back%s: \"%s\"" % ((" by %s" % who) if who else "",
                                         entry.get("feedback") or "")
    elif entry.get("text"):
        answer = "You wrote: \"%s\"" % _first_line(entry["text"])
    elif entry.get("answer") and entry.get("automatic"):
        answer = "Answered by itself: \"%s\"" % entry["answer"]
    elif entry.get("answer"):
        answer = "Answered \"%s\"%s" % (entry["answer"], (" by %s" % who) if who else "")
    elif entry.get("value") not in (None, "", []):
        value = entry["value"]
        shown = ", ".join(str(one) for one in value) if isinstance(value, list) else str(value)
        answer = "Chose %s" % shown
    elif status == "running":
        answer = "waiting for you"
    elif status in ("failed", "timeout", "cancelled") or entry.get("kept"):
        why = entry.get("message") or status
        answer = "not answered" + (" - %s" % why if why else "")
    else:
        answer = status
    text += "<br>" + _escape(answer)
    detail = entry.get("detail") or ""
    if detail:
        text += " " + toggle("hide" if opened else "what was asked")
        if opened:
            text += _block(detail)
    elif not entry.get("kept"):
        text += " " + _muted("(this run did not record the question itself)")
    return text


def _agent(entry, index, opened, toggle, label, links):
    parts = []
    if entry.get("effort"):
        parts.append(entry["effort"] + " effort")
    if isinstance(entry.get("cost_usd"), (int, float)):
        parts.append("$%.2f" % entry["cost_usd"])
    head = label + (" - " + _escape(", ".join(parts)) if parts else "")
    status = entry.get("status") or ""
    if status not in ("success", ""):
        head += " - " + _escape("%s: %s" % (status, _first_line(entry.get("message") or "",
                                                                 MESSAGE)))
    elif entry.get("verified") is True:
        attempts = entry.get("attempts") or 1
        head += " - " + _escape("verified in %s attempt%s" % (attempts, "" if attempts == 1 else "s"))
    elif entry.get("verified") is False:
        head += " - not verified"
    summary = entry.get("summary") or ""
    line = []
    if entry.get("risk"):
        line.append("risk %s" % entry["risk"])
    issues = entry.get("issues") or []
    if issues:
        line.append("%d issue%s" % (len(issues), "" if len(issues) == 1 else "s"))
    text = head
    if line or summary:
        text += "<br>" + _escape(("%s. " % ", ".join(line)) if line else "") + \
            _escape(_first_line(summary))
    more = summary or issues or entry.get("recommendations") or entry.get("out_of_scope")
    links_line = []
    if more:
        links_line.append(toggle("hide" if opened else "answer"))
    for number, path in enumerate(entry.get("prompts") or []):
        links_line.append(links.add("prompt" if number == 0 else "prompt %d" % (number + 1),
                                    {"do": "file", "run_dir": entry.get("run_dir", ""),
                                     "path": path}))
    links_line.append(links.add("transcript", {"do": "transcript", "index": index,
                                               "run": entry.get("run", ""),
                                               "step": entry.get("step", "")}))
    if links_line:
        text += " " + " ".join(links_line)
    if opened and more:
        text += _block(_answer_text(entry))
    return text


#: How each kind of stage in an agent's account reads.
STAGE_WORDS = {"start": "Started", "thinking": "Thinking", "text": "Said",
               "review": "Answered", "tool": "Used", "result": "Finished"}


def _transcript_html(told, entry, links):
    """An agent's own account, call by call, under its entry."""
    if told is None:
        return "<div style='margin:4px 0 6px 0'>%s</div>" % _muted("Reading its account...")
    calls = told.get("calls") or []
    if not calls:
        return "<div style='margin:4px 0 6px 0'>%s</div>" % _muted(
            "This step kept no account of its calls.")
    parts = []
    if told.get("cut"):
        parts.append(_muted(_escape("%d earlier stages left out." % told["cut"])))
    for number, call in enumerate(calls):
        head = "<b>Call %d</b>" % (number + 1)
        if call.get("prompt"):
            head += " " + links.add("prompt", {"do": "file", "run_dir": entry.get("run_dir", ""),
                                               "path": call["prompt"]})
        lines = [head]
        for stage in call.get("stages") or []:
            kind = stage.get("kind") or ""
            word = STAGE_WORDS.get(kind, kind)
            title = stage.get("title") or ""
            detail = stage.get("detail") or ""
            if kind in ("thinking", "text"):
                line = "%s: %s" % (_escape(word), _escape(detail or title))
                if kind == "thinking":
                    line = _muted(line)
            elif kind == "tool":
                line = _muted(_escape("%s %s" % (word, title)))
            else:
                line = _escape(" - ".join(part for part in (title, detail) if part))
            lines.append(line)
        parts.append("<br>".join(lines))
    return ("<div style='margin:4px 0 6px 0; padding-left:8px; border-left:2px solid %s'>%s</div>"
            % (theme.DIVIDER, "<br><br>".join(parts)))


def _answer_text(entry):
    parts = [entry.get("summary") or ""]
    issues = entry.get("issues") or []
    if issues:
        parts.append("Issues:\n" + "\n".join("- %s" % _issue_line(one) for one in issues))
    if entry.get("recommendations"):
        parts.append("Recommendations:\n" + "\n".join(
            "- %s" % one for one in entry["recommendations"]))
    if entry.get("out_of_scope"):
        parts.append("Left out of scope:\n" + "\n".join(
            "- %s" % one for one in entry["out_of_scope"]))
    return "\n\n".join(part for part in parts if part)


def _issue_line(issue):
    if not isinstance(issue, dict):
        return str(issue)
    where = issue.get("file") or ""
    if where and issue.get("line"):
        where = "%s:%s" % (where, issue["line"])
    severity = issue.get("severity") or ""
    return "%s%s%s" % ("[%s] " % severity if severity else "",
                       issue.get("description") or issue.get("title") or "",
                       " (%s)" % where if where else "")


def _check_line(check):
    if not isinstance(check, dict):
        return str(check)
    held = check.get("held")
    mark = "held" if held is True else "did not hold" if held is False else ""
    return "%s%s%s" % (check.get("name") or "", (": " + mark) if mark else "",
                       (" (%s)" % check["detail"]) if check.get("detail") else "")


# -- the files and the runs -----------------------------------------------------
def run_choices(answer, live_run=""):
    """``[(run id, label)]`` for the Cockpit and Stages tabs' picker, newest first."""
    names = run_numbers(answer)
    found = []
    for run in answer.get("runs") or []:
        parts = [names.get(run.get("run"), "Run")]
        if run.get("run") == live_run:
            parts[0] += " (live)"
        when = _when(run.get("started_at"))
        if when:
            parts.append(when)
        if run.get("cycle_name"):
            parts.append(run["cycle_name"])
        found.append((run.get("run", ""), " - ".join(parts)))
    return found


def files_html(answer, now=None):
    """The Files tab: every step's files, newest run first. ``(html, actions)``."""
    links = _Links()
    names = run_numbers(answer)
    groups = answer.get("files") or []
    if not groups:
        return _muted("No step of these runs kept a file."), []
    parts = []
    for group in groups:
        parts.append("<p style='margin:8px 0 2px 0'><b>%s</b> %s</p>" % (
            _escape(names.get(group.get("run"), "Run")),
            _muted(_escape(" - ".join(part for part in (
                _when(group.get("started_at"), now), group.get("cycle_name") or "") if part)))))
        rows = []
        for step in group.get("steps") or []:
            files = " ".join(links.add(one.get("name") or one.get("path") or "file",
                                       {"do": "file", "run_dir": group.get("run_dir", ""),
                                        "path": one.get("path", "")})
                             for one in step.get("files") or [])
            rows.append("<tr><td style='padding-right:12px'>%s</td><td>%s</td></tr>"
                        % (_escape(step.get("label") or step.get("step")), files))
        parts.append("<table cellspacing='0' cellpadding='2'>%s</table>" % "".join(rows))
    return "".join(parts), links.actions


def runs_html(answer, now=None):
    """The Runs tab: every run, newest first, each openable. ``(html, actions)``."""
    links = _Links()
    names = run_numbers(answer)
    runs = answer.get("runs") or []
    if not runs:
        return _muted("No run is left of this subject."), []
    rows = []
    for run in runs:
        status = run.get("status") or ""
        word = _end_words({"status": status, "message": run.get("message") or ""}, "")
        rows.append(
            "<tr><td style='padding-right:12px'>%s</td><td style='padding-right:12px; "
            "color:%s; font-family:%s'>%s</td><td style='padding-right:12px'>%s</td>"
            "<td>%s</td><td>&nbsp;%s</td></tr>" % (
                links.add(names.get(run.get("run"), "Run"),
                          {"do": "run", "cycle": run.get("cycle", ""),
                           "run_dir": run.get("run_dir", "")}),
                theme.NEUTRAL[500], theme.MONO_CSS, _escape(_when(run.get("started_at"), now)),
                _escape(run.get("cycle_name") or run.get("cycle") or ""),
                _escape(word.strip()),
                links.add("files", {"do": "files", "run_dir": run.get("run_dir", "")})))
    return ("<table cellspacing='0' cellpadding='2'>%s</table>" % "".join(rows)), links.actions
