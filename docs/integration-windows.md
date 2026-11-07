# Integration window architecture

Every integration **must support both an embedded view and a separate window**.
Both hosts use the same GUI widget and the same running application. A separate
integration window must never outlive the main application or keep it running
after the main window has accepted a close.

## Ownership and navigation

- Register every provider view through `IntegrationHost` in `IntegrationsPage`.
  The host supplies separate-window support for all registered providers.
- Keep one view and at most one separate window per provider. Detach by
  reparenting the existing view; do not construct a second view, application,
  GUI process, cache, or independent connection context.
- Opening an already detached provider raises its existing window. Search
  results also route to that window.
- Closing the separate window, or choosing **Return to main window**, returns
  the existing view to the Integrations page and brings that page forward.
- Moving the view preserves loaded data, selection, filters, drafts and
  pending requests. It must not trigger another initial network fetch or
  repopulate an already displayed list merely because its parent changed.
- Window placement is remembered per provider. Detachment is session-only:
  the next application launch starts with embedded views.

## Active project

All integration windows follow the main application's **current project**.
There are no independently pinned project contexts or concurrent windows of
one provider for different projects.

An accepted project switch immediately updates every view's core and every
window's project title, before waiting for the main inventory refresh. Clear
the previous project's issues, connections, activity and drafts; restore the
new project's saved filters and load its connections as required by visibility.
Project renaming updates window titles too.

Before switching, ask about unsaved drafts and settings. While a write is in
flight, wait for its outcome before allowing the switch or application exit.
An issue being edited with changes counts as a draft. Finish or cancel an
open sign-in dialog before changing projects.

## Provider lifecycle contract

A provider view implements:

- `window_actions`: a layout in its existing header for the host's icon-only
  detach/return button. Keep this control in the header; do not add an empty
  toolbar row above the integration. The tooltip and accessible name describe
  its current action.

- `set_core(core)`: cancel old reads, invalidate callbacks, reset project data,
  and bind the new context. Each request captures its core when submitted;
  the worker must not look up a mutable `self.core` when it eventually starts.
- `has_unsaved()`: report drafts and settings that leaving the project or
  exiting would discard.
- `busy_reason(include_dialogs=True)`: explain why a project cannot be left
  yet, including pending writes and open dialogs. With `False`, report writes
  that must finish before application shutdown.
- `shutdown()`: stop timers, cancel reads and OAuth waits, reject owned
  dialogs, and prevent late callbacks or show events from starting new work.
  It must be safe to call more than once.

Search (Ctrl+K) asks every provider, and a provider answers for itself:

- `search_rows()`: `search.Hit`s for what the view has already loaded. It never
  asks the service.
- `search_source()`: `(title, detail)` for the row that offers to search the
  service, or `None` when nothing is set up to search, such as no connection
  with a token. Then no row is shown. Offering the row asks the service nothing.
- `search_remote(query, done)`: search the service because that row was chosen,
  off the GUI thread. Call `done(hits, error)` once, with `search.Hit`s only.
- `show_found(target)`: put a hit's target on screen (an issue on its tab,
  read as a click on it would) and return whether it could.

`IntegrationsPage` files every target as `(provider, target)`, drops anything
that is not a `search.Hit`, and opens the provider that showed the hit.

`IntegrationsPage` aggregates lifecycle calls for every registered provider.
`MainWindow` performs the leave/close checks and calls shutdown only after exit
has been accepted. The page also handles `QApplication.aboutToQuit` and its own
close. Detached windows use `WA_QuitOnClose=False`; explicit shutdown and QObject
ownership cleanup close them with the page. Pending core subprocesses are
cancelled and reaped; GUI threads remain managed by the shared background runner.

Cancelling a read is different from undoing a write. Do not cancel a submitted
write and imply that it did not happen, or automatically retry it on reopen.

## Acceptance checks for providers

Verify detach/return without lost drafts or duplicate fetches; repeated open;
navigation and search while detached; active-project switch during a pending
read; rejection of late replies; refusal to switch during a write; unsaved-change
confirmation; cancellation of OAuth on shutdown; closing the main application
with separate windows open; and restoring geometry within the current screen.

The Jira implementation and GUI tests are the first implementation of this
contract. Adding another provider must preserve the same host and lifecycle
behavior.
