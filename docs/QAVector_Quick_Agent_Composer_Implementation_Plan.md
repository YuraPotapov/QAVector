# QAVector — Quick Agent Composer
## Brief for an AI agent · Implementation plan

**Status:** implemented in 0.16.73 (see `docs/Quick_Agent_Composer_Integration.md`); this is the plan it was built from.  
**Goal:** start a new conversation with an AI agent instantly, from any QAVector screen.  
**Priorities:** UX, reuse of the existing agent infrastructure, compatibility.  
**Context:** QAVector 0.16.72, with **Integrations → Agents → Chat / Connections / Skills / Activity**, the global Search / Command Palette, the shortcut system and agent session management.

> **THE MAIN REQUIREMENT:** press the global shortcut → Quick Agent Composer opens → the cursor is **already** in the message box → type the task → **Enter** starts a new agent conversation. No clicks or steps are required first.

---

## 1. Scope and principles

1. Quick Agent is a **light global front end to QAVector's existing agents**, not another agent engine.
2. It opens in **New Chat** by default, whatever conversation was open last.
3. The default provider comes **from Connections / the existing settings**; no second provider configuration.
4. It offers a quick choice of **provider, model and effort**, the **FOLDER** to choose or check, and a few recent sessions.
5. A session is created **only when the first message is sent**: opening the widget creates no empty chat and starts no agent.
6. Agents → Chat stays the main, full interface. Quick Agent and the full chat share **one source of truth** for sessions, configuration, states and permissions.
7. Change things in increments. **Do not break** cycles, agent plugins, browser sessions, integrations, the global Search or existing shortcuts.
8. Build the UI in **QAVector's existing GUI stack**; no new UI technology without a strong reason.
9. Where UI components or services already exist, **reuse** them rather than copy them.

### Not in the first version

- A new agent engine, or running a CLI straight from the UI.
- A new Connections backend, a new permission system or an own chat storage format.
- Full-text search of every message and AI summaries of sessions.
- Replacing **Agents → Chat**.

---

## 2. The agent's first step: study what exists

**Before writing code:**

- Find the global Search and its overlay: focus, `Esc`, `Enter`, navigation and how shortcuts are registered.
- Find Agents → Chat, Connections, the provider models, supported models, `effort`, `FOLDER`, permissions and settings.
- Study how a session is created and resumed, streaming, cancel/stop, states, the event bus and how history is kept.
- Find how the **default connection/provider** is chosen and where the models' capabilities come from.
- Check how the current project / FOLDER is identified, who owns it and how the GUI reacts when it changes.
- Write the integration points down in a short technical note: "what we reuse / what has to be added".

**Do not** invent class or API names that the repository does not have. Names in the examples below stand for responsibilities only.

---

## 3. UX: layout

A **central overlay** opens over the current QAVector window, in the visual style of the existing Search: dark palette, compact frame, a visible focus, clear keyboard navigation.

Roughly:

```text
┌──────────────────────────────────────────────────────────────────┐
│  ◉ Quick Agent       [New Chat]                  shortcut / Esc  │
├──────────────────────────────────────────────────────────────────┤
│  📁 ~/src/shop                       ⎇ feature/qa-123       ▾   │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  | Write the task for the agent…                                 │
│                                                                  │
│                                                                  │
│  [Claude Code ▾]  [Sonnet ▾]  [Effort: Medium ▾]   [⚙] [Send]  │
├──────────────────────────────────────────────────────────────────┤
│  RECENT CONVERSATIONS                                 View all   │
│  • Project Intelligence implementation · Claude · QAVector     │
│  • Review Cycle architecture · Codex · QAVector                 │
│  • Server startup verification · Claude · Shop                 │
├──────────────────────────────────────────────────────────────────┤
│  Enter — Send   Shift+Enter — New line    ↑↓ — Recent   Esc     │
└──────────────────────────────────────────────────────────────────┘
```

### 3.1 The message box comes first

- Once the overlay shows, **give the message editor real keyboard focus**, not only a drawn caret.
- Typing works **at once, with no Tab and no mouse click**.
- The editor is multi-line and grows within set bounds; long text scrolls.
- `Enter` sends a non-empty message; `Shift+Enter` adds a line; `Esc` closes the overlay (and does not cancel a running agent).
- During IME composition (choosing a character from suggestions), `Enter` **must not** send by accident.
- After the overlay closes, focus goes back to the GUI element it came from, where possible.
- After a provider, model or effort is chosen, focus returns to the editor and the draft is kept.

### 3.2 Project context (FOLDER)

- Show the working folder QAVector hands the agent, as **Agents → Chat → FOLDER / Browse...** does.
- If no folder is set, say so; never make one up.
- Allow another folder through the existing folder picker.
- With Git, an **optional** badge for the branch or detached HEAD; the Composer does not depend on Git.
- When a recent session from another project is chosen, show its own context clearly; **never replace it with the current page's**.
- If the context is only a suggestion from the active part of the GUI, show where it came from and let it be changed before sending.

### 3.3 Provider, model, effort

- **Provider list:** only installed and available Connections, with a proper state or message for inactive ones.
- The first choice is QAVector's **default provider/connection**.
- **Model list:** only the models the chosen connection/provider supports, as the existing integration reports them.
- **Effort list:** only supported levels, behaving correctly for providers without effort.
- After a provider change, apply that provider's consistent default model and effort; **never carry incompatible values over**.
- If only one option exists or a feature is unsupported, show no fake settings.
- Remember only the last choices the current settings allow; never overwrite the global default provider by itself.
- `Manage Connections...` leads to the existing settings page.
- Check the parameters before starting, and show a specific error in the overlay.

### 3.4 Recent conversations

- Show the **3–5 most recent sessions**, of any provider (the number is a constant or a UI setting; no new settings system).
- Each entry: title, provider, project/FOLDER, a short state, relative time.
- Choosing one **opens the existing session**; it neither creates a new one nor restarts the agent.
- An active session shows its current events and state when opened.
- With no sessions the list is empty, and the Composer still starts a new chat fully.
- Preferably a `View all` that goes to the existing **Agents → Chat**.

### 3.5 Shortcuts

- Add the action to the **existing registry of configurable shortcuts**, shown in QAVector's general table of actions.
- Propose `Ctrl+Shift+A` **only if it is free**; check conflicts with global and system shortcuts already taken.
- The UI shows the shortcut actually set, never a hard-coded one.
- `↑/↓` through recent sessions only if it **does not get in the way of moving the cursor in the editor** — for example a separate `Ctrl+↑/↓`, or `Tab` to reach the list.
- Do not take keys while dialogs or shortcuts are active where the global action makes no sense.

---

## 4. User scenarios

### Scenario A — A new chat (the main one)

1. The person is on any QAVector page (Run, Cycles, Artifacts and so on).
2. They open Quick Agent with the shortcut.
3. The overlay opens in **New Chat**, the cursor already in the message box.
4. Provider, model and effort come from valid Connections settings.
5. They type and press `Enter`.
6. QAVector creates one new session through the existing agent/session API, sends the message and opens a compact view of the work (Quick Chat or another fitting existing view).
7. The conversation goes on in the full **Agents → Chat** with no message or state lost.

### Scenario B — Changing the provider

1. Open the Composer and start typing.
2. Choose Codex instead of Claude Code.
3. Model and effort switch to what Codex offers.
4. Focus is back in the message box, the draft kept.
5. `Enter` creates a new session with the chosen provider, model and effort.

### Scenario C — Going on with a conversation

1. Open the Composer (**New Chat** by default).
2. Choose a recent session.
3. Show that session's own agent, model, effort, FOLDER, history and state.
4. The next message goes **into that same** session.
5. Closed and opened again, the Composer starts with **New Chat** again.

### Scenario D — No provider connected

1. The overlay opens with focus in the message box.
2. A clear message, "No Connections set up", and the way to set them up.
3. No sending with an invalid provider.
4. Text already typed is kept while going to Connections and back.

### Scenario E — A session runs while the overlay closes

- Closing Quick Agent / Quick Chat **does not stop the agent**.
- State and messages keep updating through the common event system.
- The person can come back through a recent session or **Agents → Chat**.

---

## 5. Architecture and integration

### 5.1 Layers

```text
Global Hotkey / QAVector Navigation
             │
             ▼
     Quick Agent Composer  ────────→ Existing Connections Settings
             │                           │
             ▼                           ▼
     Shared Session API  ←──── Provider / Model / Effort capabilities
             │
             ├─────────────→ Existing Session Storage / History
             │
             ├─────────────→ Existing Agent Runtime / Plugins
             │
             └─────────────→ Existing Event Stream
                                   │
                        ┌──────────┴──────────┐
                        ▼                     ▼
                    Quick Chat          Agents → Chat
```

- The Composer owns only **the draft and the chosen configuration until a session is created**.
- Session lifecycle, sending, streaming, permissions, actions and persistence stay in the existing service.
- No second CLI client, provider API calls or agent process manager.
- Answers and events carry stable `session_id` and request ids, so a second press of Send creates no duplicate.

### 5.2 Domain contracts, as an example

These are **example contracts**, not names the code must have. Fit them to the real architecture first.

```python
@dataclass
class QuickAgentDraft:
    message: str
    connection_id: str | None
    model_id: str | None
    effort: str | None
    project_folder: str | None

@dataclass
class AgentSessionSummary:
    session_id: str
    title: str
    connection_id: str
    project_folder: str | None
    state: str
    updated_at: datetime
```

Main actions: `resolve_default_connection`, `list_available_models`, `list_effort_options`, `list_recent_sessions`, `create_session_and_send`, `open_existing_session`, `subscribe_to_session_events`, `open_full_chat`.

### 5.3 Guarantees and race conditions

- A second Enter or a double click on Send creates no second session and sends no message twice.
- A failed start keeps the text; a controlled retry is offered.
- Switching providers quickly never applies **stale async answers** with model lists.
- Changes to global Connections settings reach the Composer on its next opening or through events.
- If the current FOLDER is gone or unreachable, show an error; never put another folder in its place silently.
- A recent session's fixed configuration is never mixed with a new draft's settings.

---

## 6. Quick Chat (after the first message)

The MVP may open the existing full chat after Send, **but the target UX** is a compact Quick Chat panel over the current screen.

It has:

- the session's title, provider, model and state;
- the message stream and visible tool-call states;
- a box for the next message, with clear focus;
- the actions the existing runtime allows: Stop / Cancel, and Pause / Resume **only if really supported**;
- `Open Full Chat`, to the same session;
- closing the panel does not stop the work;
- where possible, the panel stays pinned while moving between screens without hiding the main controls.

**Important:** Quick Chat never bypasses action confirmations, permissions or Restrictions. An agent waiting for approval is clearly shown, and the approval mechanism is the one the full Chat uses.

---

## 7. Permissions, security and isolation

- Choosing a FOLDER sets the **working context**, not OS-level isolation by itself. Use the existing Restrictions / agent isolation.
- Before starting, show the effective access mode (Read / Edit / Full, where the current Chat has it).
- Never raise permissions through the quick widget or a provider change.
- The existing session policies and approvals apply without exceptions.
- Drafts may hold confidential data: keep them out of ordinary debug logs; if they are written to disk, use QAVector's usual protection for local data.
- Send no text to a cloud service before the person's explicit `Send`; any exception (an external model check, say) is documented.
- Never attach the current file, a selected artifact or the UI's context by itself: only what was chosen explicitly or is clearly shown.

---

## 8. Stages for the agent

### Stage 0 — Audit (required)

**Result:** a map of the existing GUI, sessions, Connections and shortcuts, and a short note on where the work plugs in.  
**Done when:** it is settled which classes are reused and which are new.

### Stage 1 — Composer MVP

- The global overlay, styled in the current design system.
- The action wired to the existing configurable shortcuts.
- Focus in the message box on opening.
- `Enter` sends, `Shift+Enter` adds a line, `Esc` closes.
- New Chat by default; no session before Send.

**Done when:** a person opens the widget from Run and creates one agent session without the mouse.

### Stage 2 — Connections / model / effort / FOLDER

- Use the current default connection.
- Show providers, models and effort from the capabilities, as they are.
- Change the FOLDER through the existing mechanism.
- Keep the draft and return focus after a list is used.
- Handle empty or unavailable connections.

**Done when:** a provider change loses no text, and a new session gets exactly the chosen parameters.

### Stage 3 — Recent sessions

- Show 3–5 recent sessions from the existing storage.
- Open them **without creating new ones**.
- Show their states, providers and project contexts.
- `View all` / `Open Full Chat`.

**Done when:** a person gets back to an existing conversation from any QAVector screen.

### Stage 4 — Compact Quick Chat

- Wire the shared stream, states and events.
- History, further messages, Stop / approvals (as the runtime allows).
- No second agent process or session state.
- Closing Quick Chat does not stop the work.

**Done when:** one session shows the same in Quick Chat and in the full Agents → Chat.

### Stage 5 — Polish

- Accessibility: focus management, keyboard navigation, screen readers where the GUI stack supports them.
- Resizing and other displays; long model names and paths.
- Offline, CLI unavailable, errors and retry.
- Optional pinned conversations and drafts — only after a stable MVP.
- Update the user documentation and the list of shortcuts.

---

## 9. Testing

### Unit / component tests

- The default connection, and the fallback without configuration.
- Mapping of models and effort, with no incompatible values after a provider change.
- New Chat logic, draft persistence, the order of recent sessions.
- No duplicate session or send.
- Protection against stale async capability answers.

### GUI / interaction tests

- Open from Run, Artifacts, Cycles and Agents; the cursor is in the message box.
- Type a message **with the keyboard only** and press Enter; one session is created.
- `Shift+Enter` adds a line and does not send.
- During composition `Enter` does not send.
- Change the provider and effort; focus is back and the text is still there.
- Choose a recent session with another FOLDER: its own context opens.
- Close the overlay while an answer is generated; the agent goes on.
- The shortcut does not conflict with Search or other global actions.

### Integration / regression

- Claude Code, Codex and the other integrations **actually installed**; not every one has effort.
- Existing access rights, approvals, Restrictions, Skills and session lifecycles.
- History kept, and the same session opened correctly in the full Chat.
- No regressions in Search, Run, Cycles, browser sessions, settings and shortcuts.
- Checked on the Ubuntu desktop QAVector targets.

---

## 10. Acceptance criteria / definition of done

- [ ] The overlay opens from every main QAVector screen.
- [ ] Once open, **focus is in the message input** and typing works.
- [ ] **New Chat** by default; no session before the first Send.
- [ ] The **default connection** is chosen from the existing Connections.
- [ ] Provider, model and effort change quickly; the options follow the capabilities.
- [ ] FOLDER shows the real working context and can be changed through the existing picker.
- [ ] A few recent sessions are shown; opening one creates no duplicate.
- [ ] `Enter` sends; `Shift+Enter` adds a line; `Esc` closes the overlay.
- [ ] Using a list does not erase the draft; focus returns to the editor.
- [ ] The widget and the full Chat work with **one** session / stream / state / permission set.
- [ ] Closing the overlay **does not stop** a running agent.
- [ ] No regressions in existing features and no shortcut conflicts.
- [ ] There are tests, short documentation and a description of the changed modules.

---

## 11. Instructions for the implementing agent

1. **Do not start by rewriting the existing Chat.** Study the mechanisms there and build a thin new UI over them.
2. Work in stages, in small logical changes; check the main flows after each stage.
3. Hard-code no providers, models, effort, class names, folders, shortcuts or CLI behaviour.
4. Do not change the general default provider when the person only picked another one in the Composer for the moment.
5. Reuse the existing data structures, persistence, permissions and events.
6. Write code comments in **English**.
7. On an architectural conflict, describe it and propose the smallest safe fix rather than a parallel subsystem.
8. When done, hand over: the changed files, how it integrates, test results, known limits, and how to check it by hand.

### The expected result

QAVector has a global Quick Agent Composer that **cuts the interaction down to "shortcut → type → Enter"**, uses the default provider from Connections, lets the model and effort be changed quickly, reopens recent conversations, and continues the work in the same agent runtime without losing context or any of Agents → Chat's features.
