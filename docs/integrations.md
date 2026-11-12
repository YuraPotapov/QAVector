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

## Editing an issue

An opened issue is read-only, so a stray click or keystroke changes nothing.
**Edit** turns its summary, priority, assignee, labels and description into
fields where they stand, and reads which of them this account may change on the
issue's screen. **Save** (Ctrl+S) sends only what changed, together with when
the issue was last changed at the moment Edit was pressed. If somebody changed
it in Jira since, the edit is refused and stays open with what was typed.
**Cancel** drops the changes, and so does Esc after asking if anything was
changed.

On Jira Cloud the description is edited with its formatting. The bar over it
has paragraph styles and headings, bold (Ctrl+B), italic (Ctrl+I), underline
(Ctrl+U), strikethrough, code, bulleted, numbered and check lists (Tab and
Shift+Tab nest them), links, code blocks, quotes and clearing the formatting.
What is pasted keeps only what Jira can hold. A description with something the
editor cannot keep, such as a table or a picture, stays as it is unless you
choose **Edit without them**. The words in a table are kept as lines. On Jira
Server the description is text, as Jira Server keeps it.

The people an issue can be assigned to are read when the assignee list is
first opened, or when you type a name and press Enter, and never before.

## Comments and mentions

On Jira Cloud a comment has the same formatting bar as the description. Typing
**@** offers the people who can see the issue. They are read once per issue,
the first time @ is typed in its comment or its description, and the list then
narrows as you type without asking Jira again. A name that is not among them
is looked up only when you choose **Search Jira for "…"**. The read runs off
the window's thread, so typing goes on while it is out. A mention is one piece
of the text: Backspace removes it whole. Jira notifies the people mentioned.
On Jira Server a comment is text, and a mention is written into it as
`[~username]`.

While an issue is being edited, nothing else is written to it from the page.
Refresh leaves what was typed alone. Opening another issue, picking another
connection or switching projects asks first when there are changes.

## Search, export and import

**Ctrl+K** finds the project's connections, and the issues that are already
loaded on the page, by key, summary, type, assignee or priority, without asking
Jira. Below them it offers a row of its own, `Search Jira for "QA-123"`. Typing
never searches Jira. Choosing that row (Enter or a click) does, with one request
per connection that has a token, in every project, not only the default one.
What Jira finds shows under **Jira** in the same Search. Typing something else
drops it, and the row has to be chosen again. Opening a found issue shows it on
the Issues tab, at the top of the list if the list did not have it, and reads
it there. With no connection that has a token, Jira is not offered at all.

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
- Other providers. Codex comes to Agents in a later version.

## Agents chat

**Integrations → Agents → Chat** keeps conversations in the current project.
Connections and the skills library are shared across projects. **Settings**
folds the less frequently used controls: agent, model and skills on one row,
working folder and restrictions on another. The chosen agent and folder stay
visible in the summary. Access mode remains beside the message, with Attach,
Stop and Send (Queue while the agent is answering).

Use **Attach files…**, drop local files into the chat, or paste copied files
into the message box. Selected files can be removed before sending. Each
message accepts up to 20 files with a combined size of 20 MB; text is optional.
Files are copied when their turn starts into the conversation's private
`attachments/` directory. Keep originals available until a queued turn starts.
Later turns can read these copies even if the originals have moved or been
deleted. Restricted execution exposes this directory read-only. File names
are retained in the message history; the agent reads supported formats using
its available tools. Attaching a file does not install document converters.

Enter sends; Shift+Enter starts a new line. Stop also prevents the queued
messages from starting. After a failure or Stop, queued text and files return
to the composer for review. Message drafts and selected files stay with their
conversation while switching chats; they are not persisted across app restarts.

### Access

A conversation is held in one folder, chosen before its first message; the
agent's program keeps the conversation under that folder, so it does not move
afterwards. What the agent may do there is chosen per message:

| Access | The agent may |
|---|---|
| **Read** (the default) | read and search the folder, and change nothing |
| **Edit** | also create and change files; each change is applied as it is made |
| **Full** | also run commands - everything this account can do |

Whatever the access, the folder's **restrictions** hold: the same network,
files and limits the lock beside every Project folder sets. A folder with the
network off cannot reach the model, so a message there is refused at once
rather than left to retry.

A write the access does not allow is refused by the program and is not
recorded as a file written. A conversation started by a cycle is shown
read-only: it is the record of what that step did, not something to carry on.

## Agent connections

**Integrations → Agents → Connections** lists the agent programs QAVector can
talk to - Claude Code for now - once for every project. A connection says:

| Field | Meaning |
|---|---|
| Name | What the page shows. Renaming keeps its id, which is what cycles name. |
| Agent | Which program it is. Fixed once made: a conversation held with one program cannot be carried on by another. |
| Program | The program's path. Blank finds it on PATH each time it is used; the dialog says where it is now. |
| Model, Effort | What it is asked for unless a message or a step says otherwise. Blank is the program's own choice. |

The program signs in by itself and keeps its own credentials: nothing typed
here is a secret, and `agents.json` - beside the user's data, owner-readable -
holds none. **Test** asks the program who it is signed in as and remembers the
answer; **Sign in…** starts the program's own browser sign-in. If it wants a
code typed into a terminal instead, run `claude auth login` there and press
Test. Neither asks a model anything.

## Skills

**Integrations → Agents → Skills** is a library of skills - folders with a
`SKILL.md` saying what the skill is for and how to do it, and whatever files
it uses - that an agent can be handed for a conversation. It is shared by every
project, beside the user's data in `agent-skills/`.

**Nothing is added unseen.** A skill's instructions are followed by an agent
and its scripts may be run by one, so adding is two steps:

1. **+ Skill**, then a link or a folder, then **Look**. A link is a repository
   or a folder in one, as its page's address says it -
   `https://git.example.invalid/owner/repo/tree/main/skills/name` - or any address git
   can fetch (`ssh://`, `git@host:path`). Look fetches it with git and your own
   credentials and shows every skill found: its files, whether any could be
   run, where it came from and at which commit. Nothing is added yet.
2. **Add** takes exactly the copy that was looked at - there is no second fetch
   for it to differ in. A skill whose name the library has already is replaced
   only when it is ticked on purpose.

**Update…** fetches a skill's source again and says which files changed before
any of it is taken. Nothing updates by itself. A skill that reaches outside
its own folder through a link, or is too big to be one, is refused.

In a conversation, **Skills** in the composer's settings ticks the library's
skills for it. They are copied into the conversation for each turn and handed
to the program for that turn only: neither the program's own configuration
nor the working folder changes. A skill with scripts can run them only with
Full access. The skills a program has by itself are listed for information.

## Agents activity

**Activity** lists every turn taken with an agent in the project, the newest
first: from the chat, and from every agent step of a cycle - with a
connection or without one (see *Every call on the record* in
[cycle-agents.md](cycle-agents.md)). Each row says when, which connection, who
asked, where, with what access, model and skills, how long it took, what it
cost and how it ended. Double-click opens the conversation.

The record is the conversations themselves, in `<project>/agent-sessions/`,
owner-readable only; nothing is kept beside it to disagree with it.

## Command line: agents

```sh
qavector-core --integrations agents COMMAND [--agents-file=PATH]
              [--agent-sessions-dir=PATH] [--agent-skills-dir=PATH]
```

| Command | What it does |
|---|---|
| `kinds` | The agent programs this build knows, and where each is on PATH. Starts nothing. |
| `list`, `save --from=F`, `delete ID` | The connections. |
| `test ID` or `test --from=F` | Asks the program who is signed in; a saved one remembers the answer. |
| `sign-in ID` | The command that signs the program in, for the caller to run. |
| `sessions`, `session ID` | The conversations, and one with what was said in it. |
| `session-new --from=F`, `session-set ID --from=F`, `session-delete ID` | Start, change, remove one. |
| `send ID --from=F` | Take one turn. Prints a line per event as it happens and the answer last; SIGTERM stops the agent. |
| `activity` | Every turn of every conversation, newest first. |
| `skills`, `skill-look --from=F`, `skill-add --from=F`, `skill-update NAME`, `skill-remove NAME` | The skills library. Look and Update are the only commands that reach the network. |

`send` is the only command that asks a model anything.
