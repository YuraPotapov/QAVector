"""A standalone reading copy of Jira results; the technical JSON stays unchanged.

New runs render the ADF bodies already returned by Jira. Plain-text bodies
are formatted locally too, without fetching the issue again. Only known
document nodes become HTML; task text is always escaped.
"""

import collections
import html
import re
from datetime import datetime
from urllib.parse import urlsplit


STYLE = """
:root { color-scheme: light; --ink:#213248; --muted:#637388;
  --line:#dde4ed; --paper:#fff; --ground:#f1f4f8; --accent:#2463a5; }
* { box-sizing:border-box; }
body { margin:0; background:var(--ground); color:var(--ink);
  font:16px/1.65 system-ui,-apple-system,"Segoe UI",sans-serif; }
a { color:var(--accent); text-underline-offset:3px; overflow-wrap:anywhere; }
.top { background:#203448; color:white; padding:22px max(24px,calc((100% - 1060px)/2)); }
.top a { color:#d1e6ff; }
.top-row { display:flex; align-items:center; justify-content:space-between; gap:24px; }
.brand { font-size:12px; font-weight:700; letter-spacing:.15em; }
.top h1 { font-size:26px; line-height:1.3; margin:8px 0 0; }
main { max-width:1120px; margin:28px auto; padding:0 24px 40px; }
.contents { display:flex; gap:12px; flex-wrap:wrap; margin-bottom:22px; }
.contents a { background:var(--paper); padding:7px 14px; border:1px solid var(--line);
  border-radius:6px; text-decoration:none; }
.issue { background:var(--paper); border:1px solid var(--line); border-radius:10px;
  padding:32px 40px; margin-bottom:28px; box-shadow:0 3px 12px #20344808; }
.issue-header { border-bottom:1px solid var(--line); padding-bottom:24px; margin-bottom:26px; }
.issue-key { font-weight:700; text-decoration:none; }
h2 { font-size:28px; line-height:1.35; margin:10px 0 20px; overflow-wrap:anywhere; }
h3 { font-size:21px; margin:1.6em 0 .6em; line-height:1.4; }
h4,h5,h6 { font-size:17px; margin:1.4em 0 .5em; }
.metadata { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
  gap:16px 24px; margin:0; font-size:14px; }
dt { color:var(--muted); font-size:12px; margin-bottom:3px; }
dd { margin:0; overflow-wrap:anywhere; }
.badge,.mention { background:#eaf1fa; border-radius:4px; padding:2px 7px; }
.badge { display:inline-block; font-size:13px; font-weight:600; }
.section-label { color:var(--muted); font-size:12px; letter-spacing:.1em;
  text-transform:uppercase; margin:28px 0 16px; font-weight:700; }
.document { overflow-wrap:anywhere; }
.document > :first-child { margin-top:0; }
p { margin:.65em 0; }
li { margin:.35em 0; }
li p { margin:.25em 0; }
ul,ol { padding-left:1.6em; }
hr { border:0; border-top:1px solid var(--line); margin:28px 0; }
blockquote,.panel { margin:18px 0; padding:12px 18px; border-left:3px solid #87acd5;
  background:var(--ground); }
pre { white-space:pre-wrap; overflow-wrap:anywhere; padding:16px;
  border:1px solid var(--line); border-radius:6px; background:var(--ground); }
code { font: .9em/1.6 ui-monospace,"DejaVu Sans Mono",monospace;
  background:var(--ground); padding:2px 4px; border-radius:3px; }
pre code { padding:0; }
.table-wrap { overflow-x:auto; margin:18px 0; }
table { border-collapse:collapse; width:100%; font-size:14px; }
th,td { border:1px solid var(--line); padding:10px 12px; text-align:left; vertical-align:top; }
th { background:var(--ground); font-weight:600; }
.comment { border-top:1px solid var(--line); padding:20px 0; }
.comment-header { display:flex; justify-content:space-between; gap:16px;
  flex-wrap:wrap; font-size:14px; margin-bottom:12px; }
time,.muted { color:var(--muted); font-size:14px; }
/* An embedded picture is read where the words put it, so it is bounded rather
   than shown at whatever size somebody's screenshot happened to be - a 3000px
   capture used to be the whole page. Captioned with its filename, because the
   node it came from and the file are matched by position when the editor left
   no alt text, and a reader should be able to see when that went wrong. */
.shot { margin:16px 0; }
.shot img { display:block; max-width:100%; height:auto; border:1px solid var(--line);
  border-radius:6px; background:var(--ground); }
.shot figcaption { color:var(--muted); font-size:12px; margin-top:6px; }
details { margin:16px 0; } summary { cursor:pointer; font-weight:600; }
.query { color:var(--muted); font-size:13px; }
.empty { padding:32px; text-align:center; background:var(--paper); }
@media(max-width:640px) {
  main { padding:0 12px 24px; margin-top:14px; }
  .issue { padding:22px 18px; } h2 { font-size:23px; }
}
@media print {
  body { background:white; font-size:11pt; }
  .top { background:white; color:#213248; padding:0 0 12px; }
  main { max-width:none; padding:0; margin:0; }
  .contents,.source,.query { display:none; }
  .issue { padding:0; border:0; box-shadow:none; }
  h2,h3,h4,.comment-header { break-after:avoid; }
  tr { break-inside:avoid; }
}
"""


def _escape(value):
    return html.escape(str(value or ""), quote=True)


def _link(url, label):
    """Allow navigation to web pages, never executable or local-file URLs."""
    url = str(url or "")
    try:
        parsed = urlsplit(url)
        allowed = (parsed.scheme.lower() in ("https", "http") and parsed.netloc
                   and not re.search(r"[\x00-\x20]", url))
    except ValueError:
        allowed = False
    if not allowed:
        return label
    return '<a href="%s" target="_blank" rel="noopener noreferrer">%s</a>' % (
        _escape(url), label)


def _inline(text):
    """Escaped plain text, with web links made clickable."""
    text = str(text or "")
    parts, start = [], 0
    for match in re.finditer(r'https?://[^\s<>"\']+', text):
        parts.append(_escape(text[start:match.start()]))
        url = match.group().rstrip(".,;!?)")
        parts.append(_link(url, _escape(url)))
        parts.append(_escape(match.group()[len(url):]))
        start = match.end()
    parts.append(_escape(text[start:]))
    return "".join(parts)


def _number(value, default, maximum):
    try:
        return max(1, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


class Pictures(object):
    """The issue's downloaded images, handed out to the body's media nodes.

    ADF keeps an embedded file under a media id that the attachment API never
    reports, so a node and a file cannot be matched by identity. Two things are
    tried instead, in order: the node's ``alt``, which the Jira editor usually
    fills in with the filename, and failing that the next image not yet placed.

    That second rule is a guess, and it is made deliberately rather than
    silently: both lists are in the order things were added, so it is almost
    always right, and every picture is captioned with the filename it came
    from - so a reader who is looking at the wrong one can see that they are.
    """

    def __init__(self, entries=()):
        self._left = [dict(one) for one in entries or ()]
        self._placed = []

    def take(self, alt=""):
        """The picture this node should show, or None when there is none left."""
        wanted = str(alt or "").strip().lower()
        found = None
        if wanted:
            for index, one in enumerate(self._left):
                if str(one.get("filename", "")).lower() == wanted:
                    found = self._left.pop(index)
                    break
        if found is None and self._left:
            found = self._left.pop(0)
        if found is not None:
            self._placed.append(str(found.get("filename") or ""))
        return found

    def placed(self):
        """The filenames the body found a place for, one entry per use."""
        return list(self._placed)


def _picture(attrs, pictures):
    """One embedded file: the picture itself when it was downloaded.

    The note it falls back to is what every attachment used to get, and it is
    still the right answer for a file this step did not fetch - a video, a log
    archive, or anything past the download budget.
    """
    found = pictures.take(attrs.get("alt")) if pictures is not None else None
    if not found or not found.get("src"):
        name = str((found or {}).get("filename") or attrs.get("alt") or "")
        return '<p class="muted">Attachment%s: view it in the original Jira ' \
               'issue.</p>' % (" " + _escape(name) if name else "")
    return '<figure class="shot"><img src="%s" alt="%s"><figcaption>%s' \
           '</figcaption></figure>' % (
               _escape(found["src"]), _escape(found.get("filename") or ""),
               _escape(found.get("filename") or ""))


def _adf(node, pictures=None):
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    attrs = node.get("attrs") or {}
    if kind == "text":
        text = _escape(node.get("text")).replace("\n", "<br>")
        tags = {"strong": "strong", "em": "em", "strike": "s", "code": "code", "underline": "u"}
        for mark in node.get("marks") or []:
            if mark.get("type") == "link":
                text = _link((mark.get("attrs") or {}).get("href"), text)
            elif mark.get("type") in tags:
                tag = tags[mark["type"]]
                text = "<%s>%s</%s>" % (tag, text, tag)
        return text
    if kind == "hardBreak":
        return "<br>"
    if kind == "rule":
        return "<hr>"
    if kind in ("mention", "status", "emoji"):
        text = attrs.get("text") or attrs.get("shortName") or ""
        return '<span class="mention">%s</span>' % _escape(text)
    if kind in ("inlineCard", "blockCard"):
        return _link(attrs.get("url"), _escape(attrs.get("url")))
    if kind in ("media", "mediaInline"):
        return _picture(attrs, pictures)
    inner = "".join(_adf(child, pictures) for child in node.get("content") or [])
    if kind == "heading":
        level = min(6, _number(attrs.get("level"), 1, 6) + 2)
        return "<h%d>%s</h%d>" % (level, inner, level)
    tags = {"paragraph": "p", "bulletList": "ul", "listItem": "li",
            "blockquote": "blockquote", "tableRow": "tr", "taskList": "ul"}
    if kind in tags:
        tag = tags[kind]
        return "<%s>%s</%s>" % (tag, inner or "<br>", tag)
    if kind == "orderedList":
        return '<ol start="%d">%s</ol>' % (_number(attrs.get("order"), 1, 100000), inner)
    if kind in ("tableCell", "tableHeader"):
        tag = "th" if kind == "tableHeader" else "td"
        return '<%s colspan="%d" rowspan="%d">%s</%s>' % (
            tag, _number(attrs.get("colspan"), 1, 100),
            _number(attrs.get("rowspan"), 1, 100), inner, tag)
    if kind == "table":
        return '<div class="table-wrap"><table>%s</table></div>' % inner
    if kind == "codeBlock":
        # Code is literal, including strings which happen to contain markup.
        text = "".join(str(child.get("text") or "") for child in node.get("content") or []
                       if isinstance(child, dict))
        return "<pre><code>%s</code></pre>" % _escape(text)
    if kind == "panel":
        return '<aside class="panel">%s</aside>' % inner
    if kind in ("expand", "nestedExpand"):
        return "<details open><summary>%s</summary>%s</details>" % (
            _escape(attrs.get("title") or "Details"), inner)
    if kind == "taskItem":
        return '<li><input type="checkbox" disabled%s> %s</li>' % (
            " checked" if attrs.get("state") == "DONE" else "", inner)
    return inner


def _plain(text):
    """Keep paragraphs, lists and tables readable in plain-text bodies."""
    lines = str(text or "").strip().splitlines()
    result, pending, list_kind = [], [], None

    def flush():
        nonlocal list_kind
        if pending:
            if list_kind:
                result.append("<%s>%s</%s>" % (list_kind, "".join(
                    "<li>%s</li>" % _inline(line) for line in pending), list_kind))
            else:
                result.append("<p>%s</p>" % "<br>".join(map(_inline, pending)))
        pending.clear()
        list_kind = None

    index = 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line:
            flush()
            continue
        if line.startswith("|") and line.endswith("|"):
            flush()
            rows = []
            while True:
                cells = [cell.strip() for cell in line[1:-1].split("|")]
                if not all(re.fullmatch(r":?-+:?", cell) for cell in cells):
                    tag = "td" if rows else "th"
                    rows.append("<tr>%s</tr>" % "".join(
                        "<%s>%s</%s>" % (tag, _inline(cell), tag) for cell in cells))
                if index >= len(lines) or not (lines[index].strip().startswith("|")
                                               and lines[index].strip().endswith("|")):
                    break
                line = lines[index].strip()
                index += 1
            result.append('<div class="table-wrap"><table>%s</table></div>' % "".join(rows))
            continue
        heading = re.match(r"^#{1,6}\s+(.+)$", line)
        # A numbered line opening with a Latin or a Ukrainian capital.
        section = re.match(r"^\d+(?:\.\d+)*\.?\s+[A-Z\u0410-\u042f\u0404\u0406\u0407\u0490]"
                           r".{0,100}$", line)
        if heading or section or line in ("---", "***"):
            flush()
            result.append("<hr>" if line in ("---", "***") else
                          "<h3>%s</h3>" % _inline(heading.group(1) if heading else line))
            continue
        bullet = re.match(r"^[-*]\s+(.+)$", line)
        numbered = re.match(r"^\d+[.)]\s+(.+)$", line)
        kind = "ul" if bullet else "ol" if numbered else None
        if kind != list_kind:
            flush()
        list_kind = kind
        pending.append((bullet or numbered).group(1) if kind else line)
    flush()
    return "\n".join(result)


def _body(value, pictures=None):
    return _adf(value, pictures) if isinstance(value, dict) else _plain(value)


def _attachments(files, pictures, placed=()):
    """The files the body did not place, listed under it. "" when there are none.

    Only the ones left over, because a picture the description embedded has
    already been read where the words put it, and showing it again below says
    nothing and doubles the page. What is left is exactly what a reader would
    otherwise never learn about: a file the body never refers to, one this step
    did not download, and anything that is not a picture at all. Without this,
    an issue with a log archive on it reads as an issue with nothing attached.
    """
    if not files:
        return ""
    shown = {str(one.get("filename") or ""): one.get("src", "")
             for one in pictures or []}
    # Counted rather than set-membership: two attachments may share a name, and
    # placing one of them must not hide the other.
    remaining = collections.Counter(placed)
    rows = []
    for one in files:
        name = str(one.get("filename") or "")
        if remaining.get(name):
            remaining[name] -= 1
            continue
        source = shown.get(name) or ""
        size = _size(one.get("size"))
        if source:
            rows.append('<figure class="shot"><img src="%s" alt="%s">'
                        '<figcaption>%s%s</figcaption></figure>'
                        % (_escape(source), _escape(name), _escape(name), size))
        else:
            # Named with what it is, because "an attachment" tells a reader
            # nothing about whether they need to go and open it.
            rows.append('<p class="muted">%s%s%s</p>' % (
                _escape(name) or "Attachment", size,
                " - " + _link(one.get("url"), "open in Jira")
                if one.get("url") else ""))
    if not rows:
        return ""                    # the body placed every one of them
    return '<div class="section-label">Attachments (%d)</div>%s' % (
        len(rows), "".join(rows))


def _size(value):
    """`` - 63.4 KB``, or nothing when the size is not known."""
    try:
        count = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if count <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return " - %s %s" % (("%.1f" % count).rstrip("0").rstrip("."), unit)
        count /= 1024.0
    return ""


def _date(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%d %b %Y, %H:%M %z")
    except ValueError:
        return str(value or "")


def render(answer, descriptions=None, comments=None, pictures=None):
    """Render normalized results, optionally with original ADF bodies alongside.

    ``pictures`` is ``{issue key: [{filename, mime, src}]}`` - the images the
    step downloaded, already as ``data:`` URIs, so this stays a renderer with
    no filesystem of its own. Without it every embedded file is a note saying
    to go and look at the issue, which is what this used to be.
    """
    issues = answer.get("issues") or []
    descriptions, comments = descriptions or {}, comments or {}
    pictures = pictures or {}
    sections, navigation = [], []
    for index, issue in enumerate(issues, 1):
        key = issue.get("key") or "Issue %d" % index
        title = issue.get("summary") or key
        navigation.append('<a href="#issue-%d">%s</a>' % (index, _escape(key)))
        metadata = []
        for name, label in (("status", "Status"), ("type", "Type"), ("priority", "Priority"),
                            ("assignee", "Assignee"), ("reporter", "Reporter"),
                            ("created", "Created"), ("updated", "Updated")):
            value = issue.get(name)
            if value:
                shown = _escape(_date(value) if name in ("created", "updated") else value)
                if name == "status":
                    shown = '<span class="badge">%s</span>' % shown
                metadata.append("<div><dt>%s</dt><dd>%s</dd></div>" % (label, shown))
        # One queue per issue, shared by the description and its comments: an
        # image pasted into a comment is as much "where does this belong" as
        # one in the body, and a second queue would hand the same file twice.
        queue = Pictures(pictures.get(key) or [])
        description = _body(descriptions.get(key, issue.get("description")),
                            queue)
        discussion = []
        rich_comments = comments.get(key) or []
        for number, comment in enumerate(issue.get("comments") or []):
            body = rich_comments[number] if number < len(rich_comments) else comment.get("body")
            discussion.append('<section class="comment"><div class="comment-header">'
                              '<strong>%s</strong><time>%s</time></div>'
                              '<div class="document">%s</div></section>' % (
                                  _escape(comment.get("author") or "Unknown author"),
                                  _escape(_date(comment.get("created"))),
                                  _body(body, queue)))
        # After the comments, not before: a picture pasted into one is placed
        # from the same queue, and asking what is left over before they have
        # had their turn lists files that are about to be shown.
        files = _attachments(issue.get("attachments") or [],
                             pictures.get(key) or [], queue.placed())
        sections.append('<article class="issue" id="issue-%d"><header class="issue-header">'
                        '<div class="issue-key">%s</div><h2>%s</h2>'
                        '<dl class="metadata">%s</dl></header>'
                        '<div class="section-label">Description</div><div class="document">%s</div>'
                        '%s'
                        '<div class="section-label">Comments (%d)</div>%s</article>' % (
                            index, _link(issue.get("url"), _escape(key)), _escape(title),
                            "".join(metadata), description or '<p class="muted">No description.</p>',
                            files,
                            len(discussion), "".join(discussion) or '<p class="muted">No comments in this result.</p>'))
    title = ("%s - %s" % (issues[0].get("key", ""), issues[0].get("summary", ""))
             if len(issues) == 1 else "Jira issues (%d)" % len(issues))
    return '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">' \
           '<meta name="viewport" content="width=device-width,initial-scale=1">' \
           '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src data:">' \
           '<title>%s</title><style>%s</style></head><body>' \
           '<header class="top"><div class="top-row"><div><div class="brand">QAVECTOR / JIRA</div>' \
           '<h1>%s</h1></div><a class="source" href="jira.json">JSON</a></div></header>' \
           '<main>%s%s<details class="query"><summary>Search query</summary><pre>%s</pre></details>' \
           '</main></body></html>\n' % (
               _escape(title), STYLE, "Issue details" if len(issues) == 1 else "Jira issues (%d)" % len(issues),
               '<nav class="contents">%s</nav>' % "".join(navigation) if len(issues) > 1 else "",
               "".join(sections) or '<p class="empty">No issues matched this search.</p>',
               _escape(answer.get("jql")))
