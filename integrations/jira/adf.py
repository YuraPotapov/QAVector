"""Atlassian Document Format: Cloud's bodies, as text, as HTML, and back.

Server sends and takes a description or a comment as a string. Cloud's v3 API
sends Atlassian Document Format - a tree of nodes where the words are in the
leaves - and refuses a plain string where it wants one. Everything that reads or
writes a body goes through here, so the two flavours are one decision.
"""

import time

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


# -- the description editor ---------------------------------------------------
#: What the description editor shows and gives back unchanged, with the
#: attributes it keeps. A node or a mark outside these is something the editor
#: cannot hold: a table, a picture, a panel, a colour behind the text.
EDITABLE_NODES = {
    "doc": (), "paragraph": (), "text": (), "hardBreak": (),
    "heading": ("level",), "bulletList": (), "orderedList": ("order",),
    "listItem": (), "codeBlock": ("language",), "blockquote": (), "rule": (),
    "taskList": ("localId",), "taskItem": ("localId", "state"),
    "mention": ("id", "text", "accessLevel"), "emoji": ("shortName", "id", "text"),
    "inlineCard": ("url",),
}
EDITABLE_MARKS = {"strong": (), "em": (), "strike": (), "underline": (), "code": (),
                  "link": ("href",), "textColor": ("color",)}

#: Where a node's children are words rather than blocks.
_INLINE_PARENTS = frozenset(("paragraph", "heading", "taskItem", "codeBlock"))
_INLINE = frozenset(("text", "hardBreak", "mention", "emoji", "inlineCard"))
_BLOCKS = frozenset(("paragraph", "heading", "bulletList", "orderedList", "codeBlock",
                     "blockquote", "rule", "taskList"))
#: Boxes around ordinary blocks: the box is lost, what is in it is kept.
_BOXES = frozenset(("panel", "expand", "nestedExpand", "layoutSection", "layoutColumn",
                    "bodiedExtension"))
_MEDIA = frozenset(("media", "mediaInline", "mediaSingle", "mediaGroup"))
#: What a person is told would be lost, by the node or mark that holds it.
_LOST_NAMES = {
    "table": "tables", "media": "pictures", "mediaInline": "pictures",
    "mediaSingle": "pictures", "mediaGroup": "pictures",
    "panel": "panels", "expand": "expandable sections", "nestedExpand": "expandable sections",
    "layoutSection": "columns", "layoutColumn": "columns", "status": "status labels",
    "date": "dates", "decisionList": "decisions", "decisionItem": "decisions",
    "blockCard": "link cards", "embedCard": "link cards", "extension": "macros",
    "inlineExtension": "macros", "bodiedExtension": "macros",
    "alignment": "alignment", "indentation": "indentation", "breakout": "wide blocks",
    "backgroundColor": "highlighting", "subsup": "subscript and superscript",
    "placeholder": "placeholders",
}


def editable(value):
    """A Cloud body as the description editor can hold it: ``(document, lost)``.

    ``lost`` names, for a person, what the editor cannot keep - "tables",
    "pictures" - and is empty when the document comes back from the
    editor exactly as it went in. Otherwise ``document`` is the body without
    those things. A table's rows become lines of text and a panel's content
    stays, so the words are still there. A string - Server's body - is not a
    document, and gives ``(None, [])``.
    """
    if value is None:
        return {"type": "doc", "version": 1, "content": []}, []
    if not isinstance(value, dict):
        return None, []
    lost = set()
    kept = _keep(value, lost, False)
    document = kept[0] if kept and kept[0].get("type") == "doc" else {
        "type": "doc", "version": 1, "content": kept}
    document.setdefault("version", 1)
    return document, sorted(lost)


def _keep(node, lost, inline):
    if not isinstance(node, dict):
        return []
    kind = node.get("type")
    if kind not in EDITABLE_NODES:
        lost.add(_LOST_NAMES.get(kind, str(kind)))
        return _instead(node, lost, inline)
    kept = {"type": kind}
    attrs = {name: value for name, value in (node.get("attrs") or {}).items()
             if name in EDITABLE_NODES[kind] and value is not None}
    if attrs:
        kept["attrs"] = attrs
    if kind == "text":
        kept["text"] = str(node.get("text") or "")
        if not kept["text"]:
            return []
    marks = []
    for mark in node.get("marks") or []:
        name = (mark or {}).get("type") if isinstance(mark, dict) else None
        if name in EDITABLE_MARKS:
            one = {"type": name}
            mark_attrs = {key: value for key, value in (mark.get("attrs") or {}).items()
                          if key in EDITABLE_MARKS[name]}
            if mark_attrs:
                one["attrs"] = mark_attrs
            marks.append(one)
        elif name:
            lost.add(_LOST_NAMES.get(name, name))
    if marks:
        kept["marks"] = marks
    if kind in _INLINE or kind == "rule":
        return [kept]
    content = []
    for child in node.get("content") or []:
        if kind == "blockquote" and isinstance(child, dict) and child.get("type") != "paragraph":
            # The editor quotes paragraphs; a list in a quote keeps its words.
            lost.add("lists in quotes")
            content.extend(_paragraphs(flatten(child)))
            continue
        content.extend(_keep(child, lost, kind in _INLINE_PARENTS))
    kept["content"] = content
    return [kept]


def _instead(node, lost, inline):
    """What stands in for a node the editor cannot hold: its words."""
    kind = node.get("type")
    if kind in _MEDIA:
        return []
    if kind in _BOXES:
        found = []
        for child in node.get("content") or []:
            found.extend(_keep(child, lost, inline))
        return found
    if kind == "table":
        rows = [flatten(row).strip() for row in node.get("content") or []]
        return [one for row in rows for one in _paragraphs(row)]
    text = " ".join(flatten(node).split()) if inline else flatten(node).strip()
    attrs = node.get("attrs") or {}
    if kind == "status":
        text = str(attrs.get("text") or "")
    elif kind == "date":
        try:
            text = time.strftime("%Y-%m-%d", time.gmtime(int(attrs.get("timestamp")) / 1000))
        except (TypeError, ValueError):
            text = ""
    if not text:
        return []
    return [{"type": "text", "text": text}] if inline else _paragraphs(text)


def _paragraphs(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": line}]}
            for line in str(text or "").split("\n") if line.strip()]


def clean(document):
    """A document from the description editor, checked before Jira sees it.

    Only what :data:`EDITABLE_NODES` and :data:`EDITABLE_MARKS` allow, in the
    places ADF allows it. A code mark keeps only a link beside it, which is
    the one mark ADF lets code carry. A link that is not a web or mail address
    is dropped. Ids a task list needs are made. An empty document is ``None``,
    which clears the description. Anything else is a :class:`ValueError` that
    says what was wrong.
    """
    if not isinstance(document, dict) or document.get("type") != "doc":
        raise ValueError("The description is not a document.")
    content = [_clean_block(one) for one in _list(document.get("content"), "doc")]
    if not any(_has_words(one) for one in content):
        return None
    return {"type": "doc", "version": 1, "content": content}


def _list(value, where):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("The description's %s holds something that is not a list." % where)
    return value


def _clean_block(node, allowed=_BLOCKS):
    kind = node.get("type") if isinstance(node, dict) else None
    if kind not in allowed:
        raise ValueError("The description has %s where it may not." % (kind or "nothing"))
    if kind in ("paragraph", "heading"):
        made = {"type": kind, "content": _clean_inline(node.get("content"), kind)}
        if kind == "heading":
            level = (node.get("attrs") or {}).get("level")
            if not isinstance(level, int) or not 1 <= level <= 6:
                raise ValueError("A heading's level must be 1 to 6.")
            made["attrs"] = {"level": level}
        return made
    if kind == "rule":
        return {"type": "rule"}
    if kind == "codeBlock":
        texts = []
        for one in _list(node.get("content"), kind):
            if not isinstance(one, dict) or one.get("type") != "text":
                raise ValueError("A code block holds text only.")
            if one.get("text"):
                texts.append({"type": "text", "text": str(one["text"])})
        made = {"type": "codeBlock", "content": texts}
        language = (node.get("attrs") or {}).get("language")
        if isinstance(language, str) and language:
            made["attrs"] = {"language": language}
        return made
    if kind == "blockquote":
        inner = [_clean_block(one, ("paragraph",)) for one in _list(node.get("content"), kind)]
        return {"type": "blockquote", "content": inner or [{"type": "paragraph", "content": []}]}
    if kind in ("bulletList", "orderedList"):
        items = [_clean_item(one) for one in _list(node.get("content"), kind)]
        if not items:
            raise ValueError("A list needs at least one item.")
        made = {"type": kind, "content": items}
        order = (node.get("attrs") or {}).get("order")
        if kind == "orderedList" and isinstance(order, int) and order > 1:
            made["attrs"] = {"order": order}
        return made
    # taskList: items, and lists of items one level in.
    import uuid
    items = []
    for one in _list(node.get("content"), kind):
        sort = one.get("type") if isinstance(one, dict) else None
        if sort == "taskList":
            items.append(_clean_block(one, ("taskList",)))
        elif sort == "taskItem":
            attrs = one.get("attrs") or {}
            items.append({"type": "taskItem",
                          "attrs": {"localId": str(attrs.get("localId") or uuid.uuid4()),
                                    "state": "DONE" if attrs.get("state") == "DONE" else "TODO"},
                          "content": _clean_inline(one.get("content"), "taskItem")})
        else:
            raise ValueError("A checklist holds checklist items only.")
    if not items:
        raise ValueError("A checklist needs at least one item.")
    return {"type": "taskList",
            "attrs": {"localId": str((node.get("attrs") or {}).get("localId") or uuid.uuid4())},
            "content": items}


def _clean_item(node):
    if not isinstance(node, dict) or node.get("type") != "listItem":
        raise ValueError("A list holds list items only.")
    inner = [_clean_block(one, ("paragraph", "bulletList", "orderedList", "codeBlock"))
             for one in _list(node.get("content"), "list item")]
    if not inner or inner[0]["type"] != "paragraph":
        inner.insert(0, {"type": "paragraph", "content": []})
    return {"type": "listItem", "content": inner}


def _clean_inline(content, where):
    made = []
    for node in _list(content, where):
        kind = node.get("type") if isinstance(node, dict) else None
        attrs = (node.get("attrs") or {}) if kind else {}
        if kind == "text":
            text = str(node.get("text") or "")
            if not text:
                continue
            one = {"type": "text", "text": text}
            marks = _clean_marks(node.get("marks"))
            if marks:
                one["marks"] = marks
            made.append(one)
        elif kind == "hardBreak":
            made.append({"type": "hardBreak"})
        elif kind == "mention" and attrs.get("id"):
            made.append({"type": "mention", "attrs": {
                key: str(attrs[key]) for key in ("id", "text", "accessLevel")
                if attrs.get(key)}})
        elif kind == "emoji" and attrs.get("shortName"):
            made.append({"type": "emoji", "attrs": {
                key: str(attrs[key]) for key in ("shortName", "id", "text") if attrs.get(key)}})
        elif kind == "inlineCard" and _web(attrs.get("url")):
            made.append({"type": "inlineCard", "attrs": {"url": str(attrs["url"])}})
        else:
            raise ValueError("The description has %s inside a line of text."
                             % (kind or "something unknown"))
    return made


def _clean_marks(marks):
    kept, names = [], set()
    for mark in _list(marks, "text"):
        name = mark.get("type") if isinstance(mark, dict) else None
        if name not in EDITABLE_MARKS or name in names:
            continue
        attrs = mark.get("attrs") or {}
        if name == "link":
            if not _web(attrs.get("href"), mail=True):
                continue
            kept.append({"type": "link", "attrs": {"href": str(attrs["href"])}})
        elif name == "textColor":
            color = str(attrs.get("color") or "")
            if len(color) != 7 or not color.startswith("#"):
                continue
            kept.append({"type": "textColor", "attrs": {"color": color.lower()}})
        else:
            kept.append({"type": name})
        names.add(name)
    if "code" in names:
        kept = [one for one in kept if one["type"] in ("code", "link")]
    return kept


def _web(address, mail=False):
    address = str(address or "")
    return address.startswith(("https://", "http://")) or (mail and address.startswith("mailto:"))


def _has_words(node):
    if node.get("type") in ("rule",):
        return True
    if node.get("type") in _INLINE:
        return node.get("type") != "hardBreak"
    return any(_has_words(one) for one in node.get("content") or [] if isinstance(one, dict))
