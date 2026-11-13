# Integrations / Jira — design proposal

Status: interactive design mockup; production implementation has not started.

## Confirmed placement

Integrations is a top-level sidebar entry in the **Application** group,
immediately after **Plugins**, at the same navigation level. It is available
in regular mode and follows existing sidebar visibility/collapse preferences.
Jira is the first provider inside Integrations.

## Proposed navigation

- Integrations: provider catalog with Jira, connection status, and entry points
  to issues and connection management.
- Jira: Issues, Connections, Activity, and Settings tabs.
- Issues: connection selector, text/key search, project/status filters, JQL,
  selectable issue list, and a detail pane. At narrow widths details stack.
- Issue details: status transition, edit, comments, attachments, relationships,
  and an entry point to run a Cycle. Extended operations live under More.
- Connections: existing connections and an add-connection form, with product
  type and authentication choices appropriate to Cloud / Data Center.
- Activity: operation-level results and a path to investigate failures.
- Settings: default project, refresh cadence, and automation permissions.

## Visual direction

Use the existing Industry design language: square corners, hairline dividers,
slate-blue accent, slate title bar, compact controls, and monospace issue keys.
Match the existing light/dark palettes in `gui/cms_gui/theme.py`. Keep English
labels consistent with the current product. Mockup records are synthetic.

## Implementation sequence

1. Review the navigation and interactive mockup.
2. Extract a shared integration service from the existing Jira Cycle plugins;
   keep existing input/output contracts and secrets working.
3. Add connection storage and the core request protocol; choose the Cloud OAuth
   broker design before implementing authorization.
4. Implement Integrations navigation, catalog, and read-only Jira views in Qt.
5. Implement create/edit, workflow transitions, comments, attachments, and links.
6. Add worklogs, watchers, planning, bulk operations, move/clone/archive where
   supported, and expose the same operations to Cycles and agents.

The capability matrix must distinguish Cloud/Data Center, API availability,
authentication scopes, user permissions, and license restrictions. GUI and
automation use the same core operation service and policy checks.

## Mockup boundaries

The mockup explores navigation, selection, filters, connection setup, issue
creation, transitions, comments, and Cycle selection using local sample data.
It does not connect to Jira, authorize an account, or execute a Cycle.
Production behavior must handle dynamic field metadata, ADF preservation,
conflicts, ambiguous write outcomes, pagination, rate limits, and partial bulk
success. Secrets must never be included in project files or activity logs.
