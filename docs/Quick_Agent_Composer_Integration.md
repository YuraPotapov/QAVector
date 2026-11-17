# Quick Agent Composer: integration audit and MVP

## Existing integration points

- GUI: PySide6. `widgets.FloatingPanel` supplies the Search tool window without
  an X11 keyboard grab; Search's `clarifypopup`/`clarifyhead` theme roles are reusable.
- `MainWindow.action_registry` (`hotkeys.ActionRegistry`) owns configurable actions,
  conflicts, persisted overrides and displayed shortcuts. `Ctrl+Shift+A` has no
  existing repository binding; desktop-level interception still needs a desktop check.
- `AgentsView` reads Connections and provider kinds through `Core.agents` and
  serializes async responses with `_call`. `ModelPicker` reads the CLI catalogue
  on demand and discards stale responses when its context changes.
- `agentchat.Composer` already supplies folder picking, access modes, restrictions,
  model/effort controls, attachments and an expanding message editor.
- `ChatTab._defaults` reads the existing `integrations_state.agents` last connection,
  access mode and recent folder. There is no separate default-connection flag.
  The active Run page does not own the agent's folder; no directory is inferred.
- `ChatTab.delegate` is the existing entry point for starting a conversation from
  another surface. `session-new`, `_start`, `AgentTurn`, `_heard`, `_answered` and
  the core's session/event storage own lifecycle, streaming, retry, approvals and
  persistence. The lifecycle currently lives in ChatTab, not a separate GUI service.
- `AgentsView.open_session` restores a session's own configuration and events.
  Session lists are project-scoped, across all connections, via `agents sessions`.

## First increment

Add `quickagent.QuickAgentPopup`, reusing Composer and FloatingPanel. Opening only
reads Connections, kinds and recent sessions; it neither creates a conversation
nor starts a turn. The in-memory draft is separate from full Chat's drafts. Use
the existing delegation entry point with explicit model, effort, restrictions,
skills, attachments and other-agent policy, without changing the remembered
default connection. The full Chat remains the sole owner of all active turns.

Send and recent-session selection open the existing full Chat (the MVP route
allowed by section 6 of the implementation plan). A separate compact streaming
Quick Chat is deferred; introducing a second ChatTab would duplicate lifecycle
state. Project changes clear the popup draft through the existing unsaved-change
boundary. Closing the popup never stops a turn.

## Verification

Use headless Qt interaction tests with the existing fake core and fake turns:
keyboard focus and submission, IME, newline/Escape, deduplication during delayed
creation, failed creation/retry, connection defaults and switching, stale model
responses, recent sessions, draft preservation and project reset. Real CLI/auth,
desktop shortcut conflicts and Ubuntu window-manager focus need manual checks.

## Manual smoke check

1. Configure a working connection and folder in Agents → Chat. From Run,
   Artifacts and Cycles press the configured Quick Agent key, type and press
   Enter without clicking. Check that exactly one conversation starts in Chat.
2. Before Send, use Shift+Enter and an IME composition, change connection,
   model and effort, and check that the draft remains and access is appropriate.
3. Expand Settings, close with Esc and reopen: Settings must start collapsed
   while the draft remains. Visit Integrations → Agents → Connections and
   return. Rebind Quick Agent in Hotkeys and verify the displayed shortcut.
4. Select a recent conversation from another folder. Check its history, folder
   and running state; close/reopen Quick Agent while it runs and confirm it
   continues. Use Stop and approvals in the full Chat as usual.
5. Try no Connections, an unavailable CLI and a deleted folder. Check the
   error and retained draft. A failed first turn is retried in full Chat.

The first increment implements the audit and Composer/Connections/Recent
Sessions stages. Compact Quick Chat, a Git branch badge and native Ubuntu/real
provider acceptance remain follow-up work; no separate session service or
second streaming view has been introduced.

## Changed modules

- `gui/cms_gui/quickagent.py`: global draft panel, capability/connection reads,
  recent-session list, validation and handoff to the shared Chat lifecycle.
- `gui/cms_gui/theme.py`: dedicated header and recent-conversation row styles
  for both themes, with clear title/context hierarchy and selection feedback.
- `gui/cms_gui/main_window.py`: configurable action and navigation to Chat or
  Connections, with modal/popup guards.
- `gui/cms_gui/pages/agentchat.py`: IME protection, provider-switch resets,
  explicit delegation options, preservation of the full Chat draft and a
  navigation generation guard for initial session-list restoration.
- `gui/cms_gui/pages/agents.py`: connection-read notifications and participation
  of Quick Agent drafts in the existing project-change/close boundary.
- `gui/tests/test_quickagent.py`, `gui/tests/test_hotkey_integration.py`: new
  component, async lifecycle and actual keyboard-delivery coverage.
- `gui/README.md`, `CHANGELOG.md`, this document: usage and integration notes.

## Automated results (2026-11-14)

268 targeted tests passed across separate runs: Quick Agent (16), model picker,
hotkey registry and Agents page (99), actual hotkey integration (15), Chat,
delegation and other-agent approvals (109), and Search (29). The existing
remembered-session restoration test was also rerun after the navigation guard.
`git diff --check` passed. An offscreen render was inspected for layout.

Commands use `gui/.venv/bin/python -m pytest` with `QT_QPA_PLATFORM=offscreen`.
The Search HTML regression initially aborted in the restricted sandbox because
Qt WebEngine's sandbox-host syscall was denied. The complete Search group then
passed outside that restriction with `QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu`.
No real agent prompt was sent, and no production Connections/settings were
changed by these tests.

## UI refinement

Settings now start collapsed on every opening; background Connections refreshes
preserve the current expansion state. The panel no longer includes a Manage
Connections button. Setup errors name the existing Connections page. The header
uses one background surface, and recent conversations have separate title,
context, time and status fields with padding and separators. Their widgets stay
in place during status updates, preserving keyboard selection and hover.

The popup now has a fixed 560 px height, capped to the available screen before
showing. No content-driven window resizing remains. The composer is bounded by
a scroll area, and history occupies the remaining region without sizing itself
to the number of sessions. The existing background host reads history, with
`widgets.Skeleton` covering its region during slow reads; success, empty results,
errors and clock ticks do not alter window geometry.
