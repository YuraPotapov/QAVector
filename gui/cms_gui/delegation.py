"""Handing a Jira issue to an agent: the bridge between two integrations.

Jira knows nothing of agents and the Agents integration nothing of Jira; this is
what uses both, as a cycle step uses both through ``cycle/connections.py``. It
adds itself to Jira's opened issue through the form's generic slot
(``JiraView.add_issue_extension``) and talks to agents through the Agents view
- the chat, the core's ``sessions``, ``session`` and ``changes``.

**One key ties them.** A conversation begun here names the issue in its source:
``ref`` ``jira:<connection>:<KEY>``. The Agents integration matches it exactly and
reads nothing into it; this is the only place that writes it or knows what it
means. Every conversation about the issue is found by it, whoever opened it.

**The conversation is in the chat.** Delegate opens it with an editable draft.
The person sends it when ready; only then does the agent run. The issue shows
the record: Agent work, Summary and Changes, read from the project's
conversations, never from an agent.

**What came before goes along.** A new conversation about an issue starts with
the issue as its task and, when there were conversations about it before, what
they decided - the latest plan, the answers given, what the agent said last -
so a new one does not start from nothing. It is all in the box before it is
sent, to be cut or changed.

Nothing here is a setting of Jira's or of the agents': what it remembers - the
folder an issue's project was worked on in - is its own.
"""

from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QPushButton

from .pages.agentcards import ANSWERS
from .pages.agentwords import when_text
from .pages.delegationtabs import (AgentWorkTab, ChangesTab, DelegatePanel, SummaryTab,
                                   state_text)

#: How much of each earlier conversation goes into a new one's task.
PLAN_CHARS, SAID_CHARS, ANSWER_CHARS = 1500, 800, 400

#: How many earlier conversations about the issue are read for it.
EARLIER = 3

#: Where the bridge keeps what it remembers, in the project's integrations state.
STATE_KEY = "delegation"


def ref_for(connection_id, key):
    """The key a conversation about an issue is found by."""
    return "jira:%s:%s" % (connection_id or "", key or "")


def project_of(issue):
    key = str(issue.get("key") or "")
    return (issue.get("project") or {}).get("key") or (key.split("-")[0] if "-" in key else key)


def plain(html):
    """An issue's description as text: what an agent reads, without markup."""
    if not html:
        return ""
    document = QTextDocument()
    document.setHtml(html)
    return document.toPlainText().strip()


def _cut(text, limit):
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " [...]"


def _indented(text):
    return "\n".join("    " + line for line in str(text).splitlines())


def task_text(issue, previous=""):
    """The task an issue is handed over as: what it is, its description, and
    what earlier conversations about it came to."""
    key = issue.get("key") or ""
    lines = ["%s: %s" % (key, issue.get("summary") or "")]
    facts = [(name, issue.get(field)) for name, field in (
        ("Type", "type"), ("Priority", "priority"), ("Status", "status"))]
    said = " · ".join("%s: %s" % (name, value) for name, value in facts if value)
    if said:
        lines.append(said)
    if issue.get("url"):
        lines.append("Link: %s" % issue["url"])
    description = plain(issue.get("description_html"))
    if description:
        lines += ["", description]
    if previous:
        lines += ["", previous]
    return "\n".join(lines)


def previous_work(key, conversations):
    """What earlier conversations about ``key`` came to: ``[(session, events)]``,
    oldest first. "" when there were none."""
    if not conversations:
        return ""
    lines = ["Previous work on %s, from earlier conversations about it:" % key]
    for session, events in conversations:
        plan = next((one.get("text") for one in reversed(events)
                     if one.get("type") == "plan"), "")
        answers = next((one.get("text") for one in reversed(events)
                        if one.get("type") == "user"
                        and str(one.get("text") or "").startswith(ANSWERS)), "")
        last = next((one.get("text") for one in reversed(events)
                     if one.get("type") == "text"), "")
        lines.append("- %s (%s, %s):" % (session.get("title") or "Untitled",
                                         state_text(session).lower(),
                                         when_text(session.get("updated"))))
        if plan:
            lines += ["  The latest plan:", _indented(_cut(plan, PLAN_CHARS))]
        if answers:
            lines += ["  Answered:", _indented(_cut(answers[len(ANSWERS):], ANSWER_CHARS))]
        if last:
            lines += ["  What the agent said last:", _indented(_cut(last, SAID_CHARS))]
    return "\n".join(lines)


class Delegation:
    """The Delegate button, and Agent work, Summary and Changes, on an opened issue.

    ``open_agents(session_id)`` brings the agents' chat forward - on a
    conversation, or on whatever it shows for None.
    """

    def __init__(self, jira, agents, settings, open_agents):
        self.jira, self.agents, self.settings = jira, agents, settings
        self.open_agents = open_agents
        self.detail = self.panel = None
        self.issue, self.connection_id, self.ref = None, "", ""
        self.sessions = []
        self.changes = {}               # conversation id -> what changed, as the core said
        agents.chat.conversation_changed.connect(self._changed)

    # -- what Jira's form asks of an extension ---------------------------------------
    def attach(self, detail):
        self.detail = detail
        self.button = QPushButton("Delegate…")
        self.button.setToolTip("Hand this issue to an agent - or carry on a conversation "
                               "about it")
        self.button.clicked.connect(self.open_panel)
        self.button.setEnabled(False)
        detail.add_action(self.button)
        self.work = detail.add_tab("Agent work", AgentWorkTab())
        self.summary = detail.add_tab("Summary", SummaryTab())
        self.changes_tab = detail.add_tab("Changes", ChangesTab())
        self.work.open_requested.connect(self.open_agents)
        self.work.feed_wanted.connect(self._read_feed)
        self.summary.wanted.connect(self._read_all_changes)
        self.changes_tab.wanted.connect(self._read_all_changes)
        self.changes_tab.changes_wanted.connect(self._read_changes)
        self._show()

    def show_issue(self, connection_id, issue, partial):
        self.issue, self.connection_id = dict(issue), connection_id
        self.button.setEnabled(True)
        ref = ref_for(connection_id, issue.get("key"))
        if ref != self.ref:
            self.ref, self.sessions, self.changes = ref, [], {}
            self._show()
            self.load()

    def clear(self):
        self.issue, self.ref, self.sessions, self.changes = None, "", [], {}
        self.button.setEnabled(False)
        if self.panel is not None:
            self.panel.hide()
        self._show()

    # -- reading ------------------------------------------------------------------
    def key(self):
        return (self.issue or {}).get("key") or ""

    def load(self):
        """The conversations about the issue: the project's record, by its ref."""
        ref = self.ref
        if not ref:
            return

        def done(payload):
            if ref != self.ref or not payload.get("ok"):
                return
            self.sessions = list(payload.get("sessions") or [])
            self._show()
            if self.summary.isVisible() or self.changes_tab.isVisible():
                self._read_all_changes()

        self.agents.ask("delegation-sessions", lambda core, cancel: core.agents(
            "sessions", document={"ref": ref}, cancel=cancel), done)

    def _show(self):
        if self.detail is None:
            return
        key = self.key()
        self.work.set_sessions(self.sessions, key)
        self.summary.set_sessions(self.sessions, self.changes, key)
        self.changes_tab.set_sessions(self.sessions, self.changes, key)
        count = len(self.sessions)
        self.detail.set_tab_title(self.work, "Agent work %d" % count if count else "Agent work")

    def _read_feed(self, session_id):
        def done(payload):
            detail = self.work.detail(session_id)
            if detail is not None and payload.get("ok"):
                detail.show_events(payload.get("events") or [])

        self.agents.ask("delegation-session:%s" % session_id, lambda core, cancel: core.agents(
            "session", session_id, document={"limit": 400}, cancel=cancel), done)

    def _read_all_changes(self):
        for one in self.sessions:
            self._read_changes(one.get("id"))

    def _read_changes(self, session_id):
        if not session_id or session_id in self.changes:
            return
        ref = self.ref

        def done(payload):
            if ref != self.ref:
                return
            self.changes[session_id] = (dict(payload) if payload.get("ok") else
                                        {"files": [], "error": payload.get("error") or
                                         "What changed could not be read."})
            self._show()

        self.agents.ask("delegation-changes:%s" % session_id, lambda core, cancel: core.agents(
            "changes", session_id, cancel=cancel), done)

    def _changed(self, session_id):
        """A conversation was begun or went on: read the issue's again."""
        if self.issue is not None:
            self.changes.pop(session_id, None)
            self.load()

    # -- handing it over ----------------------------------------------------------
    def _memory(self):
        return dict(self.settings.integrations_state().get(STATE_KEY) or {})

    def _remember(self, **changes):
        state = dict(self.settings.integrations_state())
        state[STATE_KEY] = dict(state.get(STATE_KEY) or {}, **changes)
        self.settings.save_integrations_state(state)

    def defaults(self):
        """What a new conversation about the issue starts from: the folder its
        project was worked on in, else the last one agents worked in here."""
        agents = dict(self.settings.integrations_state().get("agents") or {})
        mine = self._memory()
        folders = [one for one in agents.get("folders") or [] if isinstance(one, str)]
        folder = (mine.get("folders") or {}).get(project_of(self.issue or {})) or (
            folders[0] if folders else "")
        return {"connection": agents.get("connection") or "", "folder": folder,
                "mode": mine.get("mode") or "edit", "plan": mine.get("plan", True)}

    def open_panel(self):
        if self.issue is None:
            return
        if self.panel is None:
            self.panel = DelegatePanel(self.detail)
            self.panel.continue_requested.connect(self._carry_on)
            self.panel.delegate_requested.connect(self.delegate)
        earlier = [one for one in self.sessions if not one.get("record")]
        if not self.agents.connections:
            self.agents.list_connections(then=lambda *_: self._offer(earlier))
        else:
            self._offer(earlier)

    def _offer(self, earlier):
        """Read what the latest earlier conversations came to, then offer."""
        if self.issue is None:
            return
        issue, wanted = dict(self.issue), list(reversed(earlier[:EARLIER]))
        read = {}

        def show():
            previous = previous_work(issue.get("key"), [(one, read.get(one["id"], []))
                                                        for one in wanted])
            self.panel.set_offer(issue.get("key"), earlier, self.agents.connections,
                                 self.defaults(), task_text(issue, previous))
            self.panel.open_below(self.button)

        if not wanted:
            show()
            return
        left = [len(wanted)]
        for one in wanted:
            def done(payload, sid=one["id"]):
                read[sid] = list(payload.get("events") or []) if payload.get("ok") else []
                left[0] -= 1
                if not left[0] and self.issue is not None and \
                        self.issue.get("key") == issue.get("key"):
                    show()

            self.agents.ask("delegation-earlier:%s" % one["id"],
                            lambda core, cancel, sid=one["id"]: core.agents(
                                "session", sid, document={"limit": 400}, cancel=cancel), done)

    def _carry_on(self, session_id):
        self.open_agents(session_id)

    def delegate(self, chosen, message):
        """Prepare a chat draft with the issue's ref; the person sends it."""
        issue = self.issue or {}
        key = issue.get("key") or ""
        folders = dict(self._memory().get("folders") or {})
        folders[project_of(issue)] = chosen.get("folder")
        self._remember(folders=folders, plan=bool(chosen.get("plan")), mode=chosen.get("mode"))
        title = "%s · %s" % (key, issue.get("summary") or "")
        source = {"label": "Jira %s" % key, "detail": issue.get("summary") or "",
                  "ref": self.ref}
        chosen = dict(chosen, title=title if len(title) <= 60 else title[:57].rstrip() + "...")

        def done(payload):
            if not payload.get("ok"):
                self.agents.chat.composer.say(payload.get("error") or "The conversation "
                                              "could not be begun.", bad=True)

        self.panel.hide()
        self.open_agents(None)
        self.agents.delegate(chosen, source, message, done)
