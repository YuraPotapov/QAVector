"""Reading somebody's Jira issues, and the comments on them, as a step.

What a cycle wants from Jira is usually one question: *what is on this person's
plate, and what has been said about it*. That is one step - a search plus the
comments on what it found - rather than a Jira client, and this is deliberately
not one. There is no create, no transition, no edit: a cycle step that reads is
a step you can point at a production instance without thinking, and everything
this returns is an input to whatever comes next.

**No dependency.** ``urllib.request`` from the standard library, the way
``engine/assertions.py`` already reaches an HTTP server. Adding a client library
to the core so a step can make two GET requests would be the wrong trade in a
project whose runtime dependency list is one line long.

**The token never belongs in the cycle file.** Cycle files are committed and
ship inside the build. Declare the token as a secret variable and reference it::

    variables:
      jira_token: {secret: true}

    steps:
      - id: mine
        plugin: jira.issues
        with:
          token: "${vars.jira_token}"

See ``cycle/secrets.py`` - that arrangement exists because a live API key ended
up in a cycle file once already.

**Cloud and Server are different products here, and the step says which.**
Atlassian removed the old search endpoint from Cloud and returns comment bodies
as Atlassian Document Format, a tree of nodes; Server keeps the older endpoint
and returns comments as text. Guessing between them from a URL would be a
coin-flip that fails at three in the morning, so ``flavour`` is a field with no
default cleverness: ``cloud`` or ``server``.
"""

import base64
import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

from cycle import jira_report, registry
from cycle.registry import CyclePlugin, PluginMetadata, field, output
from domain.cycle import Artifact

#: Which product this is talking to. They differ in the search endpoint and in
#: how a comment's body comes back, and nothing about a URL says which is which.
CLOUD, SERVER = "cloud", "server"

#: Which side of an issue the person is on. ``any`` is the union, which is the
#: honest default for "what is on their plate" - work is often reported by one
#: person and carried by another.
ROLES = ("assignee", "reporter", "creator", "mentioned", "watcher", "any")

#: The union ``any`` expands to. Not ``watcher`` or ``mentioned``: both are far
#: wider than "their work" and would bury the answer.
ANY_ROLES = ("assignee", "reporter", "creator")

#: How issues come back when the step does not say. Newest first is right for
#: "what is going on" - the question somebody reading a list is usually asking.
#: A cycle that takes one task per run usually wants the opposite and says so.
DEFAULT_ORDER = "updated DESC"

#: The fields worth asking for. Asking for everything makes the response many
#: times larger and slower for facts nothing reads.
FIELDS = ("summary", "description", "status", "assignee", "reporter",
          "created", "updated", "priority", "issuetype", "attachment")

#: What ``attachments`` may be set to. "images" is the default because a
#: screenshot is usually the half of a bug report that the words are about,
#: while a 40 MB log archive is not something an agent is going to read.
NONE, IMAGES, ALL = "none", "images", "all"
ATTACHMENTS = (IMAGES, ALL, NONE)

#: How many bytes of attachments one step will fetch before it stops. A budget
#: rather than a per-file cap: a task with forty screenshots is the case worth
#: guarding against, and a cap per file would let it through.
DEFAULT_ATTACHMENT_BYTES = 25000000

#: What counts as an image, by the media type Jira reports. Checked by prefix
#: so a type nobody listed here - image/avif, say - is still an image.
IMAGE_PREFIX = "image/"

#: The largest picture written into the HTML reading copy. Base64 is a third
#: larger again than the bytes, and a page carrying twenty full screenshots is
#: a page nothing opens quickly. A larger one is still downloaded and still
#: listed; it is only not embedded.
INLINE_BYTES = 4000000

#: One page. Jira caps this itself; asking for more simply gets fewer.
PAGE = 50

#: How long any single request may take. A step's own ``timeout`` governs the
#: whole thing; this is so one hung connection cannot sit there forever.
REQUEST_TIMEOUT = 30


#: What an issue key looks like: a project key, a dash, a number.
ISSUE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-[0-9]+$")


class JiraError(Exception):
    """Jira refused, or could not be reached. Carries a readable reason."""


class JiraIssues(CyclePlugin):
    metadata = PluginMetadata(
        id="jira.issues", name="Jira Issues", category=registry.ACTION,
        summary="Read a person's issues and the comments on them. Reads only.",
        permissions=("network",),
        inputs=(
            field("site", "Site", required=True,
                  hint="https://yourcompany.atlassian.net, or your Jira "
                       "Server's base URL."),
            field("flavour", "Product", "choice", default=CLOUD,
                  options=(CLOUD, SERVER),
                  hint="Cloud and Server differ in their search endpoint and "
                       "in how comments come back. Nothing about the URL says "
                       "which you have."),
            field("auth", "Authentication", "choice", default="basic",
                  options=("basic", "bearer"),
                  hint="basic is Cloud: your email plus an API token. bearer "
                       "is a Server or Data Center personal access token."),
            field("email", "Account email",
                  hint="Only for basic. The account the API token belongs to."),
            field("token", "API token", required=True,
                  hint="Reference a secret variable - ${vars.jira_token} - so "
                       "the value stays out of this file. Cycle files are "
                       "committed and shipped."),
            field("user", "Whose issues", required=True,
                  hint="An account id on Cloud, a username on Server, or "
                       "currentUser() for whoever the token belongs to."),
            field("project", "Projects",
                  hint="One key, or several separated by commas - QA, or "
                       "QA, WEB. Blank reads every project the account can "
                       "see, which with a small limit can mean one noisy "
                       "project crowds the others out."),
            field("issue", "Only this issue",
                  hint="An issue key - QA-934. When set, the step reads "
                       "exactly that issue, whoever it belongs to and whatever "
                       "its status, and the queue below is not asked. What a "
                       "cycle's subject pins to run again on one task: "
                       "${vars.task_key}, empty for the next one in the queue."),
            field("role", "Their part in it", "choice", default="any",
                  options=ROLES,
                  hint="any is assignee, reporter or creator - work is often "
                       "reported by one person and carried by another."),
            field("jql", "Extra JQL",
                  hint="ANDed with the user clause, e.g. "
                       "statusCategory != Done AND updated >= -14d. Write the "
                       "ordering in Sort by rather than here - this clause is "
                       "bracketed, and an ORDER BY inside brackets is not JQL."),
            field("order", "Sort by", default=DEFAULT_ORDER,
                  hint="The ORDER BY, without those words - 'updated DESC', "
                       "or 'status ASC, created ASC'. It decides which issue "
                       "a step with `limit: 1` gets, so a cycle that takes one "
                       "task at a time is choosing its queue here."),
            field("limit", "How many issues", "number", default=50,
                  hint="The first ones in the order above."),
            field("description_limit", "Longest description", "number",
                  default=20000,
                  hint="Characters. A description is the task itself, so this "
                       "is generous - it is here because a step reading fifty "
                       "issues at once would otherwise hand an agent a "
                       "prompt of megabytes. 0 means keep all of it."),
            field("comments", "Include comments", "check", default=True,
                  hint="One more request per issue, so a large limit with this "
                       "on is a lot of requests."),
            field("comment_limit", "Comments per issue", "number", default=20,
                  hint="The most recent ones."),
            field("attachments", "Files on the issue", "choice", default=IMAGES,
                  options=ATTACHMENTS,
                  hint="images downloads the pictures, which are usually what "
                       "a bug report is actually about. all takes every file. "
                       "none lists them without fetching anything."),
            field("attachment_bytes", "Attachment budget", "number",
                  default=DEFAULT_ATTACHMENT_BYTES,
                  hint="Bytes. Downloading stops once this much has arrived, "
                       "and the rest are still listed - so one issue with a "
                       "video on it cannot turn a read into a long wait."),
            field("verify_tls", "Verify the certificate", "check", default=True,
                  hint="Off only for a Server with a self-signed certificate "
                       "on a network you trust."),
        ),
        outputs=(
            output("issues", "list",
                   "Each issue as {key, summary, description, status, "
                   "assignee, reporter, created, updated, url, comments, "
                   "attachments}."),
            output("keys", "list", "Just the issue keys, for a later step."),
            output("key", "text",
                   "The first issue's key, or empty. Here because a later "
                   "step usually wants one issue and `${...}` cannot index a "
                   "list - so with `limit: 1` this is the one it found."),
            output("title", "text",
                   "The first issue's summary, or empty - the other half of "
                   "key, for the same reason."),
            output("count", "number", "How many issues came back."),
            output("comment_count", "number", "How many comments in total."),
            output("report_path", "text", "The whole answer, as JSON on disk."),
            output("html_path", "text", "A formatted reading copy beside jira.json."),
            output("attachments", "list",
                   "Every file on every issue, as {key, filename, mime, size, "
                   "url, path}. `path` is where it was downloaded, relative to "
                   "the run, and empty for one that was only listed."),
            output("images", "list",
                   "Just the downloaded pictures' paths. What to hand an agent "
                   "that can look at them - a screenshot is usually the half "
                   "of a bug report the words are about."),
            output("attachment_count", "number",
                   "How many files are on the issues, downloaded or not."),
        ),
    )

    def problems(self, settings):
        settings = settings or {}
        literal = dict(settings)
        for key in ("limit", "comment_limit", "comments", "verify_tls"):
            if isinstance(literal.get(key), str) and "${" in literal[key]:
                literal.pop(key)
        found = super().problems(literal)
        wanted = settings.get("attachments", IMAGES)
        if (isinstance(wanted, str) and "${" not in wanted
                and wanted not in ATTACHMENTS):
            found.append("attachments must be one of %s."
                         % ", ".join(ATTACHMENTS))
        budget = settings.get("attachment_bytes", DEFAULT_ATTACHMENT_BYTES)
        if not (isinstance(budget, str) and "${" in budget):
            try:
                if isinstance(budget, bool) or int(budget) < 0:
                    raise ValueError()
            except (TypeError, ValueError, OverflowError):
                found.append("attachment_bytes must be a whole number of bytes.")
        issue = settings.get("issue")
        if (isinstance(issue, str) and issue.strip() and "${" not in issue
                and not ISSUE_KEY.match(issue.strip())):
            found.append("issue must be an issue key such as QA-12, not %r."
                         % issue)
        for key in ("site", "email", "token", "user", "jql", "project", "issue"):
            if key in settings and not isinstance(settings[key], str):
                found.append("%s must be text." % key)

        site = str(settings.get("site") or "")
        if site and "${" not in site and not site.startswith(("http://", "https://")):
            found.append("Site must start with http:// or https://.")
        if (settings.get("auth", "basic") == "basic"
                and not str(settings.get("email") or "").strip()):
            # Basic auth against Jira is the email, not a username, and an
            # empty one fails with a 401 that says nothing about which half is
            # missing.
            found.append("Basic authentication needs the account email.")
        for key, ceiling in (("limit", 1000), ("comment_limit", 200)):
            count = settings.get(key, 50)
            if isinstance(count, str) and "${" in count:
                continue
            try:
                if (isinstance(count, bool) or not 1 <= int(count) <= ceiling
                        or float(count) != int(count)):
                    raise ValueError()
            except (TypeError, ValueError, OverflowError):
                found.append("%s must be an integer from 1 to %d."
                             % (key, ceiling))
        return found

    def execute(self, context, step):
        problems = self.problems(step.settings)
        if problems:
            return registry.failed("; ".join(problems))
        context.cancel.raise_if_set()

        get = Session(
            site=str(self.setting(step.settings, "site")).rstrip("/"),
            auth=self.setting(step.settings, "auth", "basic"),
            email=str(self.setting(step.settings, "email", "") or ""),
            token=str(self.setting(step.settings, "token")),
            verify=bool(self.setting(step.settings, "verify_tls", True)))
        flavour = self.setting(step.settings, "flavour", CLOUD)
        limit = int(self.setting(step.settings, "limit", 50))

        issue = str(self.setting(step.settings, "issue", "") or "").strip()
        if issue:
            query = 'key = "%s"' % _escape(issue)
        else:
            query = jql_for(str(self.setting(step.settings, "user")),
                            self.setting(step.settings, "role", "any"),
                            str(self.setting(step.settings, "jql", "") or ""),
                            str(self.setting(step.settings, "project", "") or ""),
                            str(self.setting(step.settings, "order", "") or ""))
        description_limit = int(self.setting(step.settings, "description_limit", 0) or 0)
        rich_comments = {}
        try:
            raw = _search(get, flavour, query, limit, context.cancel)
            issues = [_issue(one, get.site, flavour, description_limit)
                      for one in raw]
            if self.setting(step.settings, "comments", True):
                rich_comments = _add_comments(get, flavour, issues,
                                              int(self.setting(step.settings,
                                                               "comment_limit", 20)),
                                              context.cancel)
        except JiraError as exc:
            return registry.failed(str(exc))

        # After the issues and their comments, and never fatal: a picture that
        # could not be fetched is a picture missing from the reading, not a
        # reason to lose the issue it was attached to. Each entry says whether
        # it arrived, so nothing downstream has to guess.
        downloaded = _fetch_attachments(
            get, issues, context, step.id,
            self.setting(step.settings, "attachments", IMAGES),
            int(self.setting(step.settings, "attachment_bytes",
                             DEFAULT_ATTACHMENT_BYTES) or 0),
            context.cancel)

        answer = {"jql": query, "issues": issues}
        path = os.path.join(context.step_dir(step.id), "jira.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(answer, handle, indent=2, ensure_ascii=False)
            handle.write("\n")

        # Keep the original document bodies only for the reading copy. Neither
        # the JSON artifact nor the agent's inputs gain presentation fields.
        descriptions = {}
        for one in raw:
            body = (one.get("fields") or {}).get("description")
            if body is not None and (not description_limit
                                     or len(_body(body, flavour)) <= description_limit):
                descriptions[one.get("key", "")] = body
        html_path = os.path.join(context.step_dir(step.id), "jira.html")
        with open(html_path, "w", encoding="utf-8") as handle:
            handle.write(jira_report.render(answer, descriptions, rich_comments,
                                            _pictures(issues, context)))

        comments = sum(len(one.get("comments") or []) for one in issues)
        files = [dict(one, key=issue["key"])
                 for issue in issues for one in issue.get("attachments") or []]
        images = [one["path"] for one in files
                  if one.get("path") and str(one.get("mime", "")).startswith(
                      IMAGE_PREFIX)]
        return registry.PluginResult(
            "success",
            outputs={"issues": issues,
                     "keys": [one["key"] for one in issues],
                     "key": issues[0]["key"] if issues else "",
                     "title": issues[0].get("summary", "") if issues else "",
                     "count": len(issues), "comment_count": comments,
                     "report_path": context.relative(path),
                     "html_path": context.relative(html_path),
                     "attachments": files, "images": images,
                     "attachment_count": len(files)},
            artifacts=[Artifact("json", context.relative(path), step.id,
                                name="jira", bytes=os.path.getsize(path)),
                       Artifact("html", context.relative(html_path), step.id,
                                name="jira.html", bytes=os.path.getsize(html_path))]
            + downloaded,
            message="jira: %d issue%s, %d comment%s%s" % (
                len(issues), "" if len(issues) == 1 else "s",
                comments, "" if comments == 1 else "s",
                ", %d file%s" % (len(downloaded),
                                 "" if len(downloaded) == 1 else "s")
                if downloaded else ""))


# -- the query ----------------------------------------------------------------
def jql_for(user, role, extra="", project="", order=""):
    """The JQL for "this person's issues", in the order the step asked for.

    Quoted, with the quotes in the names escaped. A user string and a project
    key both reach this from a cycle file or a variable, and JQL has the same
    injection problem every query language has: an unescaped quote ends the
    literal and whatever follows is read as query.

    ``order`` is the ORDER BY without those words. It is a field of its own
    rather than something written into ``extra`` because ``extra`` is bracketed
    - and an ORDER BY inside brackets is a syntax error, not an ordering. It
    matters more than it looks: a cycle that takes one task per run is choosing
    which task here.
    """
    who = user.strip()
    literal = who if who.endswith("()") else '"%s"' % _escape(who)
    if role == "any":
        clause = "(%s)" % " OR ".join("%s = %s" % (one, literal)
                                      for one in ANY_ROLES)
    elif role == "mentioned":
        clause = "text ~ %s" % literal      # the only way Jira expresses it
    else:
        clause = "%s = %s" % (role, literal)

    where = projects_clause(project)
    if where:
        clause = "%s AND %s" % (clause, where)
    extra = (extra or "").strip()
    if extra:
        # Bracketed, because an OR inside it would otherwise swallow
        # everything to its left.
        clause = "%s AND (%s)" % (clause, extra)
    return "%s ORDER BY %s" % (clause, _ordering(order))


def _ordering(order):
    """The ORDER BY tail, with those words stripped if somebody wrote them.

    Written both ways by people who have typed JQL before, and both readings
    are obvious - so accepting one and producing a syntax error for the other
    would be a gratuitous way to fail.
    """
    order = (order or "").strip()
    if not order:
        return DEFAULT_ORDER
    without = order.lstrip()
    if without[:8].upper() == "ORDER BY":
        without = without[8:].strip()
    return without or DEFAULT_ORDER


def projects_clause(project):
    """``project = "QA"`` or ``project in ("QA", "WEB")``, or nothing at all.

    Blank means every project the account can see, which is the right default
    for "what is on this person's plate" - work crosses projects. It is not
    the right answer for every account, though: with a small ``limit`` and
    ``ORDER BY updated DESC``, one busy project can take the whole page and a
    quiet one never appears, which is a wrong answer nobody notices.
    """
    keys = [one.strip() for one in str(project or "").split(",")]
    keys = ['"%s"' % _escape(one) for one in keys if one]
    if not keys:
        return ""
    if len(keys) == 1:
        return "project = %s" % keys[0]
    return "project in (%s)" % ", ".join(keys)


def _escape(text):
    return text.replace("\\", "\\\\").replace('"', '\\"')


# -- talking to it ------------------------------------------------------------
class Session(object):
    """The site, the credentials, and one way of asking it something.

    Shared with ``jira_transition.py`` rather than written twice: the
    authentication, the two flavours' base paths and the business of turning
    an HTTP failure into something worth reading are the same question whether
    a step is reading or writing, and two copies would drift on the half that
    says why a 401 happened.
    """

    def __init__(self, site, auth, email, token, verify=True):
        self.site = site
        self._verify = verify
        if auth == "bearer":
            self._header = "Bearer " + token
        else:
            pair = ("%s:%s" % (email, token)).encode("utf-8")
            self._header = "Basic " + base64.b64encode(pair).decode("ascii")

    def __call__(self, path, params=None, body=None):
        """GET, or POST when ``body`` is given. Returns the parsed JSON."""
        url = self.site + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        request = urllib.request.Request(url)
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request = urllib.request.Request(url, data=data, method="POST")
            request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", self._header)
        request.add_header("Accept", "application/json")
        context = None if self._verify else ssl._create_unverified_context()
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT,
                                        context=context) as answer:
                raw = answer.read().decode("utf-8", "replace").strip()
            # A write answers 204 with nothing in it, which is a success and
            # not a malformed reply. Reading a body that is not there was fine
            # while only the reading plugin used this.
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raise JiraError(_why(exc, url))
        except (urllib.error.URLError, OSError) as exc:
            raise JiraError("Cannot reach %s: %s" % (self.site, exc))
        except ValueError as exc:
            raise JiraError("%s did not answer with JSON: %s" % (url, exc))

    def fetch(self, url):
        """One attachment's bytes. Separate from ``__call__``, deliberately.

        That one parses JSON and asks for it in the Accept header; this wants
        whatever the file is. They also differ in what a URL means: an
        attachment's ``content`` is absolute and may point at a media host that
        is not the site itself, so it is used as given rather than appended -
        after checking it is the site's, because following an absolute URL out
        of a payload would send this account's credentials wherever the payload
        said to.
        """
        target = url if "://" in url else self.site + url
        if not _same_site(target, self.site):
            raise JiraError("%s is not on %s; not sending credentials there."
                            % (target, self.site))
        request = urllib.request.Request(target)
        request.add_header("Authorization", self._header)
        context = None if self._verify else ssl._create_unverified_context()
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT,
                                        context=context) as answer:
                return answer.read()
        except urllib.error.HTTPError as exc:
            raise JiraError(_why(exc, target))
        except (urllib.error.URLError, OSError) as exc:
            raise JiraError("Cannot reach %s: %s" % (target, exc))


def _same_site(url, site):
    """Whether ``url`` is on the same host and scheme as the configured site."""
    one, other = urllib.parse.urlsplit(url), urllib.parse.urlsplit(site)
    return (one.scheme, one.netloc) == (other.scheme, other.netloc)


def _why(exc, url):
    """An HTTP failure as something worth reading.

    Jira puts the real reason in the body - "the JQL names a field that does
    not exist", "this account cannot browse that project" - and the status line
    alone sends somebody looking in the wrong place.
    """
    detail = ""
    try:
        payload = json.loads(exc.read().decode("utf-8", "replace"))
        if isinstance(payload, dict):
            messages = payload.get("errorMessages") or []
            errors = payload.get("errors") or {}
            detail = "; ".join([str(one) for one in messages]
                               + ["%s: %s" % pair for pair in errors.items()])
    except Exception:                       # noqa: BLE001 - best effort only
        detail = ""
    if exc.code == 401:
        detail = detail or ("the credentials were refused. On Cloud this is "
                            "your email plus an API token, not your password.")
    elif exc.code == 403:
        detail = detail or "the account may not do that."
    elif exc.code in (404, 410):
        detail = detail or ("no such endpoint. Check Product: Cloud and Server "
                            "do not share one.")
    return "Jira answered %s for %s%s" % (exc.code, url,
                                          " - " + detail if detail else "")


def _search(get, flavour, query, limit, cancel):
    """Every issue the query finds, up to ``limit``, newest first.

    Paged, because Jira caps a page well below what a step may ask for, and
    silently returning the first fifty of two hundred would be the kind of
    wrong answer nobody notices.
    """
    found, token, start = [], None, 0
    while len(found) < limit:
        cancel.raise_if_set()
        want = min(PAGE, limit - len(found))
        if flavour == CLOUD:
            # The older /search endpoints are gone from Cloud; this one pages
            # by an opaque token rather than by offset.
            body = {"jql": query, "maxResults": want, "fields": list(FIELDS)}
            if token:
                body["nextPageToken"] = token
            page = get("/rest/api/3/search/jql", body=body)
            token = page.get("nextPageToken")
        else:
            page = get("/rest/api/2/search",
                       {"jql": query, "maxResults": want, "startAt": start,
                        "fields": ",".join(FIELDS)})
            start += want
        issues = page.get("issues") or []
        found.extend(issues)
        if not issues or (flavour == CLOUD and (page.get("isLast") or not token)):
            break
        if flavour == SERVER and start >= int(page.get("total") or 0):
            break
    return found[:limit]


def _add_comments(get, flavour, issues, limit, cancel):
    """The most recent comments on each issue, in place.

    One request per issue: Jira has no endpoint for "the comments on these
    twenty issues", and asking for comments inside the search response returns
    all of them with no way to cap it.
    """
    version = "3" if flavour == CLOUD else "2"
    bodies = {}
    for issue in issues:
        cancel.raise_if_set()
        page = get("/rest/api/%s/issue/%s/comment" % (version, issue["key"]),
                   {"maxResults": limit, "orderBy": "-created"})
        raw = (page.get("comments") or [])[:limit]
        issue["comments"] = [_comment(one, flavour) for one in raw]
        bodies[issue["key"]] = [one.get("body") for one in raw]
    return bodies


# -- what comes back ----------------------------------------------------------
def _issue(raw, site, flavour=CLOUD, description_limit=0):
    fields = raw.get("fields") or {}
    return {
        "key": raw.get("key", ""),
        "summary": fields.get("summary") or "",
        # The summary is a title; this is the task. A plan made from the title
        # and the comments alone is a plan made from the margins of the page.
        "description": _cut(_body(fields.get("description"), flavour),
                            description_limit),
        "status": _name(fields.get("status")),
        "type": _name(fields.get("issuetype")),
        "priority": _name(fields.get("priority")),
        "assignee": _person(fields.get("assignee")),
        "reporter": _person(fields.get("reporter")),
        "created": fields.get("created") or "",
        "updated": fields.get("updated") or "",
        "url": "%s/browse/%s" % (site, raw.get("key", "")),
        "comments": [],
        # Listed whether or not anything is downloaded. A task whose
        # specification is a screenshot used to arrive as a description that
        # simply did not mention it - the picture was dropped from the body
        # and the file was never asked for - so whatever read this planned the
        # work from half the page without knowing there was another half.
        "attachments": _attachments(fields.get("attachment")),
    }


def _attachments(raw):
    """The files on an issue, as plain data. ``path`` is filled in if fetched.

    ``content`` is the URL the bytes are at, and it is absolute and
    authenticated - the same credentials the rest of this uses. It is kept
    under ``url`` rather than dropped so a person reading the JSON can open
    the file even when the step was told not to download anything.
    """
    found = []
    for one in raw or []:
        if not isinstance(one, dict):
            continue
        found.append({"filename": str(one.get("filename") or ""),
                      "mime": str(one.get("mimeType") or ""),
                      "size": int(one.get("size") or 0),
                      "created": one.get("created") or "",
                      "author": _person(one.get("author")),
                      "url": str(one.get("content") or ""),
                      # Where it landed on this machine. Empty until something
                      # fetches it, and still empty if the fetch failed - so a
                      # reader can tell "not asked for" from "here it is".
                      "path": ""})
    return found


def _wanted(attachment, how):
    """Whether this file is one the step asked to have downloaded."""
    if how == ALL:
        return True
    if how == IMAGES:
        return str(attachment.get("mime", "")).startswith(IMAGE_PREFIX)
    return False


def _fetch_attachments(get, issues, context, step_id, how, budget, cancel=None):
    """Download what the step asked for. Returns the artifacts it wrote.

    Never raises. Every other failure in this plugin loses the whole answer,
    and that is right for a search that did not happen - but a file that could
    not be fetched costs one picture, and losing the issue over it would be a
    worse answer than the issue without the picture. The entry simply keeps its
    empty ``path``.

    ``budget`` is bytes for the step, not per file: an issue with forty
    screenshots on it is the case worth guarding against, and a cap per file
    would wave that through.
    """
    how = str(how or IMAGES)
    if how == NONE:
        return []
    spent, written = 0, []
    for issue in issues:
        for attachment in issue.get("attachments") or []:
            if cancel is not None and cancel.is_set():
                return written
            if not _wanted(attachment, how) or not attachment.get("url"):
                continue
            size = int(attachment.get("size") or 0)
            if budget and spent + size > budget:
                # Skipped rather than truncated, and the rest are still tried:
                # a small picture after a large one is worth having, and half
                # a PNG is not a picture.
                continue
            where = os.path.join(context.step_dir(step_id), "attachments",
                                 _safe(issue.get("key") or "issue"))
            try:
                os.makedirs(where, exist_ok=True)
                path = _unique(os.path.join(
                    where, _safe(attachment.get("filename") or "file")))
                data = get.fetch(attachment["url"])
                with open(path, "wb") as handle:
                    handle.write(data)
            except (JiraError, OSError):
                continue
            spent += len(data)
            attachment["path"] = context.relative(path)
            written.append(Artifact(
                "image" if str(attachment.get("mime", "")).startswith(IMAGE_PREFIX)
                else "file", attachment["path"], step_id,
                name=attachment.get("filename") or os.path.basename(path),
                bytes=len(data)))
    return written


def _pictures(issues, context):
    """The downloaded images as ``data:`` URIs, by issue key, for the reading copy.

    Embedded rather than linked, for two reasons that point the same way. The
    reading copy is a single file somebody can send to a colleague, and a
    relative link would leave them with a page of missing pictures; and its
    content security policy allows no remote or local file loads at all, which
    is what makes it safe to open a document Jira supplied.

    Bounded per file, because base64 is a third larger again than the bytes and
    a page carrying twenty screenshots is a page nothing opens quickly. One too
    large keeps its place in the document as the note it always had.
    """
    found = {}
    for issue in issues:
        for one in issue.get("attachments") or []:
            path, mime = one.get("path"), str(one.get("mime") or "")
            if not path or not mime.startswith(IMAGE_PREFIX):
                continue
            if int(one.get("size") or 0) > INLINE_BYTES:
                continue
            try:
                with open(os.path.join(context.workspace, path), "rb") as handle:
                    raw = handle.read()
            except OSError:
                continue
            found.setdefault(issue["key"], []).append({
                "filename": one.get("filename") or "",
                "mime": mime,
                "src": "data:%s;base64,%s" % (
                    mime, base64.b64encode(raw).decode("ascii"))})
    return found


def _safe(name):
    """A filename Jira supplied, reduced to one this machine will accept.

    Jira allows what an operating system does not - separators included - and
    an attachment called ``../../.ssh/authorized_keys`` is not a hypothetical
    thing for a downloader to be careless about.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "")).strip("-.")
    return (cleaned or "file")[:120]


def _unique(path):
    """``path``, or the first ``name-2.png`` that nothing has taken.

    Two attachments on one issue may share a name - Jira allows it, and a
    second screenshot pasted into a comment usually is called the same thing -
    and the second one silently replacing the first is the sort of loss nobody
    notices until they are looking at the wrong picture.
    """
    if not os.path.exists(path):
        return path
    stem, extension = os.path.splitext(path)
    suffix = 2
    while os.path.exists("%s-%d%s" % (stem, suffix, extension)):
        suffix += 1
    return "%s-%d%s" % (stem, suffix, extension)


def _comment(raw, flavour):
    return {"author": _person(raw.get("author")),
            "created": raw.get("created") or "",
            "updated": raw.get("updated") or "",
            "body": _body(raw.get("body"), flavour)}


def _name(value):
    return (value or {}).get("name", "") if isinstance(value, dict) else ""


def _person(value):
    """Whoever this is, as a name. Never the account id or the email.

    A cycle's outputs end up in reports and in an agent's prompt, and an email
    address is personal data that nothing downstream here needs.
    """
    if not isinstance(value, dict):
        return ""
    return value.get("displayName") or value.get("name") or ""


def _body(value, flavour):
    """A comment's text.

    Server sends a string. Cloud sends Atlassian Document Format - a tree of
    nodes where the words are in the leaves - so the leaves are gathered and
    the structure is dropped. That loses formatting, which is the right thing
    to lose: what reads this is a report or an agent, and neither wants a
    document tree.
    """
    if isinstance(value, str):
        return value.strip()
    if flavour == CLOUD and isinstance(value, dict):
        return _flatten(value).strip()
    return "" if value is None else str(value)


def _flatten(node):
    """The words out of an ADF tree, with enough of its shape to still read.

    Not a renderer: what reads this is an agent or a report, and neither wants
    a document tree. But some structure *is* the content, and dropping it
    changes what the text says rather than only how it looks:

    * a **table** whose cells run together is a wall of words - the rows and
      the columns are what made it a table;
    * a **list** is a set of separate things, and a paragraph of them joined
      end to end reads as one thing;
    * a **link's** address is often the whole point of the sentence, and ADF
      keeps it in a mark rather than in the words.

    So those three keep a plain-text equivalent, and everything else is still
    reduced to its words.
    """
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")

    if kind == "text":
        return _linked(node)
    if kind == "hardBreak":
        return "\n"
    if kind == "mention":
        return str((node.get("attrs") or {}).get("text") or "")
    if kind == "emoji":
        return str((node.get("attrs") or {}).get("text") or "")
    if kind == "inlineCard":
        return str((node.get("attrs") or {}).get("url") or "")
    if kind == "rule":
        return "---\n"
    if kind in ("media", "mediaInline"):
        # A media node has no words in it, so the generic branch below reduced
        # it to nothing at all: a bug report whose whole specification was a
        # screenshot arrived as a description that did not mention one. It says
        # so now, and the file itself is in the step's `attachments`.
        return _media(node)

    children = node.get("content") or []
    if kind in ("bulletList", "orderedList"):
        ordered = kind == "orderedList"
        rows = []
        for index, one in enumerate(children, 1):
            text = _flatten(one).strip()
            if text:
                mark = "%d. " % index if ordered else "- "
                rows.append(mark + text.replace("\n", "\n  "))
        return "\n".join(rows) + "\n" if rows else ""
    if kind in ("tableCell", "tableHeader"):
        # One cell, on one line: a cell that kept its paragraph breaks would
        # put the rest of its row on the next line and the table would stop
        # lining up at the first cell that had two sentences in it.
        return " ".join(
            " ".join(_flatten(one).split()) for one in children).strip()
    if kind == "tableRow":
        return "| " + " | ".join(_flatten(one) for one in children) + " |\n"

    inner = "".join(_flatten(one) for one in children)
    if kind in ("paragraph", "heading", "listItem", "blockquote", "codeBlock",
                "table", "mediaSingle", "mediaGroup"):
        return inner + "\n"
    return inner


def _media(node):
    """A picture or a file in the body, as the line of text it stands for.

    What can be said about it is thin - ADF keeps the file under a media id
    that is not the attachment id the REST API uses, so the two cannot be
    matched up reliably - but *that there is one* is the part whose absence
    changed the meaning of the description. The alt text is used when the
    node carries one, because a person who wrote alt text wrote the caption.
    """
    attrs = node.get("attrs") or {}
    kind = "image" if str(attrs.get("type") or "") != "link" else "link"
    name = str(attrs.get("alt") or "").strip() or str(attrs.get("url") or "").strip()
    return "[%s: %s]" % (kind, name) if name else "[%s]" % kind


def _cut(text, limit):
    """``text``, shortened to ``limit`` characters and saying that it was.

    Saying so matters more here than usual: what reads this is an agent about
    to plan work from it, and silently handing it three quarters of a
    specification is how it confidently plans three quarters of the job.
    """
    text = text or ""
    if not limit or len(text) <= limit:
        return text
    return text[:limit] + "\n\n[... cut at %d characters; the rest is in Jira]" % limit


def _linked(node):
    """One text leaf, with its link's address kept beside the words.

    ADF puts the address in a mark rather than in the text, so flattening the
    leaves alone silently drops every URL in the document - and in a Jira
    description the address is often the whole point of the sentence.
    """
    text = str(node.get("text") or "")
    for mark in node.get("marks") or []:
        if isinstance(mark, dict) and mark.get("type") == "link":
            href = str((mark.get("attrs") or {}).get("href") or "")
            if href and href != text:
                return "%s (%s)" % (text, href)
    return text
