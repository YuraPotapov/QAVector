# Integrations

Integrations connects a project to the services it works with. Each connection
is defined once in the project. The Integrations page uses it to read and change
things, and anything else in QAVector can use it by its id. Jira is the first
provider.

Integrations knows nothing about what uses it. It offers a connection with its
credentials (`integrations.store.credentials`) and a log of changes
(`integrations.activity`), and it has no setting, column or action for any one
user of those. Cycles use connections as described in
[Cycles](cycles.md#reading-jira).

## Connections

A connection is one account on one site, such as *Product team* on
`https://yourcompany.atlassian.net`. It belongs to the project, like the
project's cycles, scenarios and services. A new project has none.

| Field | Meaning |
|---|---|
| Name | What the page shows. It can be renamed at any time. |
| Id | Made once from the first name and never changed. Whatever uses the connection names it by its id, so renaming breaks nothing. |
| Product | `cloud`, or `server` for Jira Server and Data Center. The two differ in their API and in how text bodies come back, and a URL does not say which one you have. |
| Site | The base URL. |
| Authentication | `oauth` is a sign-in through the browser (Cloud), described below. `basic` is an email plus an API token (Cloud), classic or with scopes. `bearer` is a personal access token (Server or Data Center). |
| Email | The account the API token belongs to. Used only with `basic`. |
| Token | Kept encrypted in the project's secrets store, never shown again. Leaving the box empty when editing keeps the stored token. |
| Verify certificate | Turn it off only for a server with a self-signed certificate on a network you trust. |

**Where it is stored.**

```
<project>/integrations.json     name, site, product, account, settings
<project>/cyclesecrets.json     the token, encrypted, under "@integrations"
```

Both files are readable by their owner only. The token shares the encryption
key and the store that cycle secrets already use. That store protects against
casual exposure, not against someone who already has your account; see
`cycle/secrets.py`. The token never appears on a command line, in an answer, in
a run's files or in the activity log.

**Settings of a connection.** These are set on the Jira page's *Settings* tab.

- **Default project** narrows the issue list when no project is chosen.
- **Refresh every N minutes** is off by default. When it is on, the list
  refreshes only while the page is visible, and it refreshes only the rows on
  screen.

## API tokens with scopes

Jira Cloud refuses an API token that has scopes at the site itself. It answers
such a token only through Atlassian's API gateway
(`api.atlassian.com/ex/jira/<cloud id>`), still with the email and the token.
Nothing about a token says which kind it is, so QAVector finds out on first use.
When the site refuses an email and token with 401, the core asks the site for its
cloud id (`/_edge/tenant_info`, which needs no sign-in) and tries the gateway once.
If the gateway accepts the token, the connection remembers it (`gateway: true`,
`cloud_id`), and every later request goes there, whoever makes it. Typing a
new token, or changing the site or the email, makes it find out again.
Connections shows such an account as *scoped token*.

The same check is part of every Jira Cloud request made with an email and a
token, including requests from a client that carries its own site, email and
token rather than a connection. Cloud can answer a search with a refused token as if
nobody were asking: 200, with no issues. So an empty first page is checked with
one `myself` request before it is shown as empty, and an expired token is
reported as a refused sign-in rather than as an empty list.

## Signing in with Atlassian

On Jira Cloud a connection can be made without typing an email or a token. In
**Add connection**, choose **Sign in with Atlassian**. Your browser opens
Atlassian's consent page, and when you allow access you pick the site. The
connection is created from that site.

Atlassian needs an OAuth app's client secret to finish a sign-in, and has no
official way around that for a desktop program. A secret built into public code
is no secret, so QAVector carries none. The sign-in goes one of two ways:

- **Through a sign-in service.** This is the ordinary way, and the person never
  sees a secret. A team deploys the small Cloudflare Worker in `signin-service/`
  once. It holds the app's secret, and it hands the tokens back sealed to a key
  that QAVector made for that one sign-in. Its address is given when QAVector is
  built (`QAVECTOR_SIGNIN_SERVICE`) and lives only in the package, never in the
  code. See [signin-service/README.md](../signin-service/README.md).
- **With the project's own OAuth app** (developer mode, *Sign-in setup…*). A
  developer registers an app at developer.atlassian.com with the callback
  `http://localhost:47321/callback`. The port is theirs to choose, and the dialog
  shows the URL for it. They enter its Client ID and Secret, which are kept in
  the project's encrypted secrets store. When an own app is set, it comes first.

Without either, the build says so, and an API token still works.

While a sign-in waits, QAVector listens on this machine only (`127.0.0.1`), for
up to five minutes, and Cancel stops it. Requests go through Atlassian's API
gateway (`api.atlassian.com/ex/jira/<cloud id>`), while links still point at the
site. An access token lasts about an hour, so the core refreshes it shortly
before it expires, even for a caller that has been working for longer than
that. A token from the
service is refreshed through the service. Atlassian replaces the refresh token
on every refresh, so the refresh runs under a lock file and the new token is
written back before anything else can use the old one. If Atlassian refuses the
refresh (for example after 90 days without use), the connection reports
**Sign-in refused**, and **Edit → Sign in again** fixes it.

## Requests are lazy

QAVector does not send a request to Jira unless somebody asked for it. Each of
these sends one:

- opening the Issues tab for the first time in a session, which loads one page
  of the list;
- changing a filter, or pressing Enter in the search box (typing alone sends
  nothing);
- **Load more**;
- opening an issue, which reads that issue and its newest comments;
- **Refresh**, which reads the rows on screen and the open issue again, and
  never every issue's details;
- **Test** on a connection;
- any change: a status, a comment, an edit, a file.

List rows carry only a few fields: key, type, priority, summary, status,
assignee and when the issue was updated. Descriptions, comments and attachments
are read only for the opened issue, and a file is downloaded only when you open
it. The project and status lists for the filters are read the first time their
box is opened. Going back to the page shows what was already loaded, with the
time it was loaded, and sends nothing.

## Search, export and import

**Ctrl+K** finds the project's connections, and the issues that are already
loaded on the page, by key, summary, type, assignee or priority. It never asks
Jira. Opening a found issue reads it, as clicking its row does.

**File → Export** can include connections, each with its token, and the
project's own OAuth app with its secret when it has one. The file is readable by
its owner only, like the rest of an export. **File → Import** keeps each
connection's id, so whatever named it in the other project finds it in this
one. For a connection that is already here, *add to the existing ones* only
gives it a token it lacks, and *add copies* adds it beside the existing one as
"... (copy)". An own OAuth app is set only where the project has none.

## Activity

The Activity tab lists what was changed in Jira through QAVector, with the
newest first. That includes changes made on this page and changes made by
anything else through a connection. Each entry says what was done, to which
issue, and whether it worked. It also says where the change came from, in the
words of whoever made it (`source: {label, detail}`). The page shows that text
as given and never interprets it. Reads are not listed, and neither are changes that were
refused before Jira was asked, such as a transition that needs fields or an edit
over somebody else's change. No entry carries field values, comment text or a
token.

The log is `integrations-activity.jsonl`, beside `integrations.json`. It is
readable by its owner only and keeps about the newest 2000 entries.

## Command line

The page reaches integrations only through the core:

```sh
qavector-core --integrations COMMAND [--integrations-file=PATH] [--cycle-secrets-file=PATH]
```

| Command | What it does |
|---|---|
| `list` | Lists every connection, with `token_set` in place of the token. |
| `save --from=F` | Adds or changes a connection. `F` is `{"connection": {...}, "token": "...", "clear_token": false}`. A connection without an `id` is new. |
| `delete ID` | Deletes a connection and its token. |
| `test ID` | Signs in, and records when and as whom on the connection. |
| `call ID OPERATION [--from=F]` | Runs one operation, with `F` holding its request. |
| `activity` | Lists what was changed in Jira through QAVector, newest first. |
| `export [ID...] --to=F` | Writes connections with their tokens, and the own OAuth app, into `F`, readable by its owner only. Prints only the ids. |
| `import --from=F` | Puts exported connections in. `F` is `{"connections", "integration_apps", "mode": "merge"\|"copy"}`. |
| `app` | Shows how signing in would go: `mode` (`own`, `service` or `none`), the service in force, and the project's own app (client ID, port, callback URL, whether a secret is set). |
| `app-save --from=F` | A developer's settings. `F` is `{"client_id", "secret", "port", "service"}`. An empty secret keeps the one that is set. An empty client ID means no own app. An empty service means the build's own service. |
| `sign-in [--from=F]` | Signs in through the browser and answers with `sites` and a `pending` id, which `save` gives to a connection as `oauth_pending`. With `{"connection": ID}` it signs that connection in again. |

Jira operations:

| Operation | Request | Answer |
|---|---|---|
| `test` | none | `account` |
| `projects` | none | `projects: [{key, name}]` |
| `statuses` | none | `statuses: [{name, category}]` |
| `search` | `text`, `project`, `status`, `mine`, `jql`, `order`, `size`, `cursor` | `rows`, `cursor`, `jql` |
| `issue` | `key`, `comments` | `issue`: the fields, `description_html`, `attachments`, `transitions`, the newest `comments` |
| `comments` | `key`, `start`, `size` | earlier comments |

Changes. Each answers with the issue read back afterwards, in the same process,
so the page shows what Jira now holds without asking again:

| Operation | Request | Notes |
|---|---|---|
| `transition` | `key`, `to` (a transition id), `fields` | When the transition's screen requires fields that have no default, Jira is not asked. The answer is `kind: fields` with `needs: [{key, name, allowed}]`, and the request is sent again with `fields`. |
| `comment` | `key`, `body` | Plain text. On Cloud, a blank line starts a new paragraph. |
| `edit_meta` | `key` | What the issue's screen lets this account change, and the priorities on offer. |
| `edit` | `key`, `updated`, `fields` (`summary`, `description`, `labels`, `priority`) | Only the fields given are sent. If the issue's `updated` is no longer the one the editor opened with, nothing is written and the answer is `kind: conflict`. |
| `assignable` | `key`, `query` | The people the issue may be assigned to. |
| `assign` | `key`, `account` | `me`, an id from `assignable`, or empty for nobody. |
| `create_meta` | `project`, `type` | Without `type`, the project's issue types. With it, that type's fields, required ones first. |
| `create` | `project`, `type`, `fields` | |
| `attach` | `key`, `path` | One file from this machine, up to 100 MB. |
| `download` | `key`, `id`, `url`, `to` | Saves one attachment to `to`, only from the connection's own site or its gateway. |

`search` builds its JQL from the filters. Search text that looks like an issue
key becomes `key = "QA-142"`, and other text becomes `text ~ "..."`. With no
filter at all the list is limited to `updated >= -90d`, because Cloud refuses a
search with no condition. A `jql` value is used as written, and an ordering is
added only when it has none.

Every command prints one JSON object. The exit code is 0 when `ok` is true, 1
for a refusal or a failure, and 2 for a wrong command line. Failures carry a
`kind` the page can act on: `signin`, `forbidden`, `not_found`, `invalid`,
`busy`, `unreachable` or `no_token`.

## Not in this version

- OAuth for Jira Server or Data Center. Use a personal access token there.
- Other providers.
