"""Atlassian Document Format: Cloud's bodies, as text, as HTML, and back.

Server sends and takes a description or a comment as a string. Cloud's v3 API
sends Atlassian Document Format - a tree of nodes where the words are in the
leaves - and refuses a plain string where it wants one. Everything that reads or
writes a body goes through here, so the two flavours are one decision.
"""

from integrations.jira.client import CLOUD


def to_text(value, flavour):
    """A body's text.

    Server sends a string. Cloud sends a document tree, so the leaves are
    gathered and the structure is dropped. That loses formatting, which is the
    right thing to lose: what reads this is a report or an agent, and neither
    wants a document tree.
    """
    if isinstance(value, str):
        return value.strip()
    if flavour == CLOUD and isinstance(value, dict):
        return flatten(value).strip()
    return "" if value is None else str(value)


def to_html(value):
    """A body as safe HTML for reading: known nodes only, every text escaped.

    The reading copy a ``jira.issues`` step writes already renders a body this
    way, so the page shows a description exactly as that copy does.
    """
    from cycle import jira_report
    return jira_report._body(value)


def from_text(text, flavour):
    """``text`` in the shape a flavour takes for a description or a comment.

    A blank line starts a new paragraph and a single line break stays a break,
    so what somebody typed into a box keeps the shape they gave it.
    """
    text = str(text or "").replace("\r\n", "\n").strip()
    if flavour != CLOUD:
        return text
    paragraphs = []
    for block in [one for one in text.split("\n\n") if one.strip()]:
        content = []
        for index, line in enumerate(block.strip("\n").split("\n")):
            if index:
                content.append({"type": "hardBreak"})
            if line:
                content.append({"type": "text", "text": line})
        paragraphs.append({"type": "paragraph", "content": content})
    return {"type": "doc", "version": 1, "content": paragraphs}


#: The nodes :func:`from_text` writes. A document made of nothing else says
#: what its text says; anything more - a table, a list, a heading, a picture -
#: would be lost by replacing the document with its text.
PLAIN_NODES = frozenset(("doc", "paragraph", "text", "hardBreak"))


def is_plain(value):
    """Whether a body survives being edited as text without losing anything.

    A string always does. A document does when it holds only paragraphs of
    unmarked text - so an editor can warn before it flattens anything else.
    """
    if value is None or isinstance(value, str):
        return True
    if not isinstance(value, dict):
        return False
    stack = [value]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict) or node.get("type") not in PLAIN_NODES:
            return False
        if node.get("marks"):
            return False
        stack.extend(node.get("content") or [])
    return True


def flatten(node):
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
            text = flatten(one).strip()
            if text:
                mark = "%d. " % index if ordered else "- "
                rows.append(mark + text.replace("\n", "\n  "))
        return "\n".join(rows) + "\n" if rows else ""
    if kind in ("tableCell", "tableHeader"):
        # One cell, on one line: a cell that kept its paragraph breaks would
        # put the rest of its row on the next line and the table would stop
        # lining up at the first cell that had two sentences in it.
        return " ".join(
            " ".join(flatten(one).split()) for one in children).strip()
    if kind == "tableRow":
        return "| " + " | ".join(flatten(one) for one in children) + " |\n"

    inner = "".join(flatten(one) for one in children)
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
