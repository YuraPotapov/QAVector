"""A Jira Cloud description, edited with its formatting kept.

The core sends the description as Atlassian Document Format, already reduced
to what this editor can hold (``integrations/jira/adf.py``, ``editable``).
:func:`load` lays it into a QTextDocument and :func:`dump` reads the document
back into ADF. A document that is loaded and dumped untouched comes back
unchanged, and the tests hold this module to that. Before Jira sees what is
sent, the core checks it (``adf.clean``).

How each ADF thing is kept in the document:

    paragraph            a block
    heading              a block with a heading level
    bullet, ordered      blocks of a QTextList, nested by the list's indent
    checklist            a list marked as one; an item's box is its marker
    code block           blocks marked as code, one group per code block
    quote                blocks marked as quoted, one group per quote
    rule                 an empty block with a horizontal ruler
    more in a list item  a block that says which list level it continues
    bold, italic, ...    the character format's weight, italic, underline
    code                 characters marked as code
    link                 an anchor
    colour               kept beside the colour painted, so a link's is not one
    mention, emoji, card characters marked with the node they stand for
    hard break           a line separator inside a block

Nothing here asks Jira anything, and nothing here is about what the page does
with a description: that is ``jira.py``.
"""

import itertools
import json
import uuid

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QKeySequence, QShortcut, QTextBlockFormat,
                           QTextCharFormat, QTextCursor, QTextDocument,
                           QTextDocumentFragment, QTextFormat, QTextLength,
                           QTextListFormat)
from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QListWidget, QListWidgetItem,
                               QScrollArea, QTextEdit, QToolButton, QVBoxLayout)

from .. import icons, theme, widgets

# Properties of our own, beside Qt's, on character, block and list formats.
P_CODE = QTextFormat.UserProperty + 1         # characters: inline code
P_COLOR = QTextFormat.UserProperty + 2        # characters: a textColor mark
P_ATOM = QTextFormat.UserProperty + 3         # characters: a mention, emoji or card
P_ATOM_ID = QTextFormat.UserProperty + 4      # characters: which one, when two touch
P_CODE_BLOCK = QTextFormat.UserProperty + 10  # block: part of a code block
P_LANGUAGE = QTextFormat.UserProperty + 11    # block: the code block's language
P_QUOTE = QTextFormat.UserProperty + 12       # block: part of a quote
P_GROUP = QTextFormat.UserProperty + 13       # block: which code block or quote
P_CONTINUES = QTextFormat.UserProperty + 14   # block: more of a list item, at level n
P_TASK = QTextFormat.UserProperty + 20        # list: a checklist
P_LOCAL_ID = QTextFormat.UserProperty + 21    # list or block: a checklist's ids

LINE_BREAK = "\u2028"
#: Where Qt keeps a picture in the text. Pasted ones are left out: a picture
#: in a description is an attachment, which this editor does not make.
OBJECT = chr(0xFFFC)
#: Qt's paragraph sizes for headings, as its own HTML reader sets them.
HEADING_SIZE = {1: 3, 2: 2, 3: 1, 4: 0, 5: -1, 6: -1}
#: How a heading is set, short of bold: bold in a heading is a person's mark.
HEADING_WEIGHT = 600
BOLD = 700
ORDERED = (QTextListFormat.ListDecimal, QTextListFormat.ListLowerAlpha,
           QTextListFormat.ListUpperAlpha, QTextListFormat.ListLowerRoman,
           QTextListFormat.ListUpperRoman)
BULLETS = (QTextListFormat.ListDisc, QTextListFormat.ListCircle, QTextListFormat.ListSquare)
#: The tallest the editor grows; past it, it scrolls.
MAX_HEIGHT = 460


# -- how things look ----------------------------------------------------------
def _weight(fmt):
    weight = fmt.fontWeight()
    return int(getattr(weight, "value", weight) or 400)


def _mono_families():
    return [name.lower() for name in theme.FONT_MONO]


def code_format(fmt=None):
    fmt = QTextCharFormat(fmt) if fmt is not None else QTextCharFormat()
    fmt.setProperty(P_CODE, True)
    fmt.setFontFamilies(list(theme.FONT_MONO))
    fmt.setFontFixedPitch(True)
    fmt.setBackground(QColor(theme.NEUTRAL[200]))
    return fmt


def link_format(href, fmt=None):
    fmt = QTextCharFormat(fmt) if fmt is not None else QTextCharFormat()
    fmt.setAnchor(True)
    fmt.setAnchorHref(href)
    fmt.setForeground(QColor(theme.ACCENT))
    return fmt


#: Atoms made while editing are numbered past any a loaded document has.
_ATOMS = itertools.count(1)


def atom_format(node, base=None):
    """The characters of a mention, an emoji or a link card: they stand for
    the node, which is kept whole beside them."""
    fmt = QTextCharFormat(base) if base is not None else QTextCharFormat()
    attrs = node.get("attrs") or {}
    if node.get("type") == "inlineCard":
        fmt = link_format(str(attrs.get("url") or ""), fmt)
    elif node.get("type") == "mention":
        fmt.setBackground(QColor(theme.MARK_TINT))
    fmt.setProperty(P_ATOM, json.dumps(node, sort_keys=True))
    fmt.setProperty(P_ATOM_ID, next(_ATOMS))
    return fmt


def plain_format(fmt):
    """``fmt`` without what made it an atom or a link: the format to go on
    typing in, right after one."""
    plain = QTextCharFormat(fmt)
    for name in (P_ATOM, P_ATOM_ID):
        plain.clearProperty(name)
    if fmt.property(P_ATOM):
        plain.clearBackground()
        plain.setAnchor(False)
        plain.setAnchorHref("")
        plain.clearForeground()
    return plain


def _heading_block(level, fmt=None):
    fmt = QTextBlockFormat(fmt) if fmt is not None else QTextBlockFormat()
    fmt.setHeadingLevel(level)
    fmt.setTopMargin(8)
    fmt.setBottomMargin(2)
    return fmt


def _heading_format(level):
    fmt = QTextCharFormat()
    fmt.setProperty(QTextFormat.FontSizeAdjustment, HEADING_SIZE.get(level, 0))
    fmt.setFontWeight(HEADING_WEIGHT)
    return fmt


def _list_format(kind, level, start=None, local_id=""):
    fmt = QTextListFormat()
    fmt.setIndent(level)
    if kind == "orderedList":
        fmt.setStyle(QTextListFormat.ListDecimal)
        if start and start > 1:
            fmt.setStart(start)
    else:
        fmt.setStyle(BULLETS[min(level, len(BULLETS)) - 1])
    if kind == "taskList":
        fmt.setProperty(P_TASK, True)
        if local_id:
            fmt.setProperty(P_LOCAL_ID, local_id)
    return fmt


def _code_block_format(group, language="", within=0):
    fmt = QTextBlockFormat()
    fmt.setProperty(P_CODE_BLOCK, True)
    fmt.setProperty(P_GROUP, group)
    if language:
        fmt.setProperty(P_LANGUAGE, language)
    fmt.setNonBreakableLines(True)
    fmt.setBackground(QColor(theme.NEUTRAL[100]))
    if within:
        fmt.setProperty(P_CONTINUES, within)
        fmt.setIndent(within)
    return fmt


def _code_text_format():
    fmt = QTextCharFormat()
    fmt.setFontFamilies(list(theme.FONT_MONO))
    fmt.setFontFixedPitch(True)
    return fmt


def _quote_format(group):
    fmt = QTextBlockFormat()
    fmt.setProperty(P_QUOTE, True)
    fmt.setProperty(P_GROUP, group)
    fmt.setLeftMargin(16)
    fmt.setBackground(QColor(theme.NEUTRAL[100]))
    return fmt


def _rule_format():
    fmt = QTextBlockFormat()
    fmt.setProperty(QTextFormat.BlockTrailingHorizontalRulerWidth,
                    QTextLength(QTextLength.PercentageLength, 100))
    return fmt


# -- ADF into a document ------------------------------------------------------
def load(document, doc):
    """Lay an ADF document into ``document``, replacing what was there."""
    document.clear()
    _Loader(document).blocks((doc or {}).get("content") or [])
    document.clearUndoRedoStacks()
    document.setModified(False)


class _Loader(object):
    def __init__(self, document):
        self.document = document
        self.cursor = QTextCursor(document)
        self.first = True
        self.group = 0
        self.atoms = 0

    def new_block(self, block_format=None, char_format=None):
        block_format = block_format if block_format is not None else QTextBlockFormat()
        char_format = char_format if char_format is not None else QTextCharFormat()
        if self.first:
            self.first = False
            self.cursor.setBlockFormat(block_format)
            self.cursor.setBlockCharFormat(char_format)
            self.cursor.setCharFormat(char_format)
        else:
            self.cursor.insertBlock(block_format, char_format)
        return self.cursor.block()

    def blocks(self, nodes, within=0):
        for node in nodes:
            if isinstance(node, dict):
                self.block(node, within)

    def block(self, node, within=0):
        kind = node.get("type")
        content = node.get("content") or []
        if kind in ("bulletList", "orderedList", "taskList"):
            self.list(node, within + 1)
        elif kind == "heading":
            level = int((node.get("attrs") or {}).get("level") or 1)
            base = _heading_format(level)
            self.new_block(_heading_block(level), base)
            self.inline(content, base)
        elif kind == "codeBlock":
            self.group += 1
            language = str((node.get("attrs") or {}).get("language") or "")
            text = "".join(str(one.get("text") or "") for one in content
                           if isinstance(one, dict))
            for line in text.split("\n"):
                self.new_block(_code_block_format(self.group, language, within),
                               _code_text_format())
                if line:
                    self.cursor.insertText(line, _code_text_format())
        elif kind == "blockquote":
            self.group += 1
            for child in content or [{"type": "paragraph", "content": []}]:
                self.new_block(_quote_format(self.group))
                self.inline((child or {}).get("content") or [])
        elif kind == "rule":
            self.new_block(_rule_format())
        else:
            fmt = QTextBlockFormat()
            if within:
                fmt.setProperty(P_CONTINUES, within)
                fmt.setIndent(within)
            self.new_block(fmt)
            self.inline(content)

    def list(self, node, level):
        kind = node.get("type")
        attrs = node.get("attrs") or {}
        fmt = _list_format(kind, level, attrs.get("order"), attrs.get("localId") or "")
        made = None
        for item in node.get("content") or []:
            if not isinstance(item, dict):
                continue
            sort = item.get("type")
            if sort == "taskList":
                self.list(item, level + 1)
                continue
            block_format = QTextBlockFormat()
            children = item.get("content") or []
            if sort == "taskItem":
                item_attrs = item.get("attrs") or {}
                block_format.setMarker(QTextBlockFormat.MarkerType.Checked
                                       if item_attrs.get("state") == "DONE"
                                       else QTextBlockFormat.MarkerType.Unchecked)
                if item_attrs.get("localId"):
                    block_format.setProperty(P_LOCAL_ID, str(item_attrs["localId"]))
            block = self.new_block(block_format)
            if made is None:
                made = self.cursor.createList(fmt)
            else:
                made.add(block)
            if sort == "taskItem":
                self.inline(children)
                continue
            rest = children
            if children and isinstance(children[0], dict) and \
                    children[0].get("type") == "paragraph":
                self.inline(children[0].get("content") or [])
                rest = children[1:]
            self.blocks(rest, within=level)

    def inline(self, nodes, base=None):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            kind = node.get("type")
            attrs = node.get("attrs") or {}
            if kind == "text" and node.get("text"):
                self.cursor.insertText(str(node["text"]), _marked(node.get("marks"), base))
            elif kind == "hardBreak":
                self.cursor.insertText(LINE_BREAK,
                                       QTextCharFormat(base) if base else QTextCharFormat())
            elif kind in ("mention", "emoji", "inlineCard"):
                shown = (attrs.get("text") if kind != "inlineCard" else attrs.get("url")) or \
                    attrs.get("shortName") or ""
                self.cursor.insertText(str(shown) or " ", atom_format(node, base))


def _marked(marks, base=None):
    fmt = QTextCharFormat(base) if base else QTextCharFormat()
    for mark in marks or []:
        name = (mark or {}).get("type")
        attrs = (mark or {}).get("attrs") or {}
        if name == "strong":
            fmt.setFontWeight(BOLD)
        elif name == "em":
            fmt.setFontItalic(True)
        elif name == "underline":
            fmt.setFontUnderline(True)
        elif name == "strike":
            fmt.setFontStrikeOut(True)
        elif name == "code":
            fmt = code_format(fmt)
        elif name == "link":
            fmt = link_format(str(attrs.get("href") or ""), fmt)
    for mark in marks or []:
        if (mark or {}).get("type") == "textColor":
            color = str(((mark or {}).get("attrs") or {}).get("color") or "")
            fmt.setProperty(P_COLOR, color)
            fmt.setForeground(QColor(color))
    return fmt


# -- a document back into ADF -------------------------------------------------
def dump(document):
    """The document as ADF - the shape ``adf.clean`` checks and Jira takes."""
    return _Dumper().read(document)


class _Open(object):
    """A list being read: its level, which QTextList it is, and its node."""

    def __init__(self, level, key, node):
        self.level, self.key, self.node = level, key, node


class _Dumper(object):
    def __init__(self):
        self.content = []
        self.lists = []
        self.group = None  # (kind, group, node) of the code block or quote being read
        self.ids = set()

    def read(self, document):
        block = document.begin()
        while block.isValid():
            self.block(block)
            block = block.next()
        return {"type": "doc", "version": 1, "content": self.content}

    def block(self, block):
        fmt = block.blockFormat()
        listed = block.textList()
        if listed is not None:
            self.group = None
            self.list_item(block, listed)
            return
        within = fmt.property(P_CONTINUES)
        if within and self.lists:
            while self.lists and self.lists[-1].level > within:
                self.lists.pop()
            if self.lists and self.lists[-1].level == within:
                self.more_of_item(block, self.lists[-1])
                return
        self.lists = []
        if _code_block(fmt):
            self.code_into(self.content, block)
            return
        if fmt.property(P_QUOTE):
            paragraph = {"type": "paragraph", "content": _inline(block)}
            last = self.content[-1] if self.content else None
            if self._continues(last, "quote", fmt):
                last["content"].append(paragraph)
            else:
                node = {"type": "blockquote", "content": [paragraph]}
                self.content.append(node)
                self.group = ("quote", fmt.property(P_GROUP), id(node))
            return
        self.group = None
        if fmt.hasProperty(QTextFormat.BlockTrailingHorizontalRulerWidth):
            if block.text().strip():
                self.content.append({"type": "paragraph", "content": _inline(block)})
            self.content.append({"type": "rule"})
            return
        level = fmt.headingLevel()
        if level:
            self.content.append({"type": "heading", "attrs": {"level": min(6, level)},
                                 "content": _inline(block, heading=True)})
        else:
            self.content.append({"type": "paragraph", "content": _inline(block)})

    def list_item(self, block, listed):
        fmt = listed.format()
        level = max(1, fmt.indent())
        key = listed.objectIndex()
        marker = block.blockFormat().marker()
        task = bool(fmt.property(P_TASK)) or marker != QTextBlockFormat.MarkerType.NoMarker
        kind = "taskList" if task else (
            "orderedList" if fmt.style() in ORDERED else "bulletList")
        while self.lists and (self.lists[-1].level > level or (
                self.lists[-1].level == level and self.lists[-1].key != key)):
            self.lists.pop()
        if not self.lists or self.lists[-1].level < level:
            parent = self.lists[-1] if self.lists else None
            if parent is not None and parent.node["type"] == "taskList":
                kind = "taskList"  # a checklist holds checklists, nothing else
            node = {"type": kind, "content": []}
            if kind == "orderedList" and fmt.start() > 1:
                node["attrs"] = {"order": fmt.start()}
            if kind == "taskList":
                node["attrs"] = {"localId": self._id(fmt.property(P_LOCAL_ID))}
            if parent is None:
                self.content.append(node)
            elif parent.node["type"] == "taskList":
                parent.node["content"].append(node)
            else:
                parent.node["content"][-1]["content"].append(node)
            self.lists.append(_Open(level, key, node))
        open_list = self.lists[-1]
        inline = _inline(block)
        if open_list.node["type"] == "taskList":
            open_list.node["content"].append({"type": "taskItem", "attrs": {
                "localId": self._id(block.blockFormat().property(P_LOCAL_ID)),
                "state": "DONE" if marker == QTextBlockFormat.MarkerType.Checked else "TODO"},
                "content": inline})
        else:
            open_list.node["content"].append(
                {"type": "listItem", "content": [{"type": "paragraph", "content": inline}]})

    def more_of_item(self, block, open_list):
        """A paragraph or code under a list item, one level in: part of it."""
        items = [one for one in open_list.node["content"]
                 if one.get("type") in ("listItem", "taskItem")]
        if not items:
            return
        item = items[-1]
        fmt = block.blockFormat()
        if item["type"] == "taskItem":
            # A checklist item holds one line of words; more of it is a break.
            item["content"].append({"type": "hardBreak"})
            item["content"].extend(_inline(block))
            return
        if _code_block(fmt):
            self.code_into(item["content"], block)
            return
        self.group = None
        item["content"].append({"type": "paragraph", "content": _inline(block)})

    def code_into(self, holder, block):
        """A line of a code block: the next line of the one just read, if it
        is the same one, else the first of a new one."""
        fmt = block.blockFormat()
        text = block.text().replace(LINE_BREAK, "\n")
        last = holder[-1] if holder else None
        if self._continues(last, "code", fmt):
            joined = _code_text(last) + "\n" + text
            last["content"] = [{"type": "text", "text": joined}] if joined else []
            return
        node = _code_node(fmt, text)
        holder.append(node)
        self.group = ("code", fmt.property(P_GROUP), id(node))

    def _continues(self, last, kind, fmt):
        return (last is not None and self.group is not None
                and self.group == (kind, fmt.property(P_GROUP), id(last)))

    def _id(self, wanted):
        """A checklist id: the one it had, unless an item copied it on Enter."""
        wanted = str(wanted or "")
        if not wanted or wanted in self.ids:
            wanted = str(uuid.uuid4())
        self.ids.add(wanted)
        return wanted


def _code_block(fmt):
    return bool(fmt.property(P_CODE_BLOCK)) or fmt.nonBreakableLines()


def _code_node(fmt, text):
    node = {"type": "codeBlock", "content": [{"type": "text", "text": text}] if text else []}
    language = fmt.property(P_LANGUAGE)
    if language:
        node["attrs"] = {"language": str(language)}
    return node


def _code_text(node):
    return "".join(one.get("text") or "" for one in node.get("content") or [])


def _inline(block, heading=False):
    nodes = []
    shown_atom = None
    it = block.begin()
    while not it.atEnd():
        fragment = it.fragment()
        it += 1
        if not fragment.isValid():
            continue
        fmt = fragment.charFormat()
        if fmt.isImageFormat():
            continue
        atom = fmt.property(P_ATOM)
        if atom:
            mark = (atom, fmt.property(P_ATOM_ID))
            if mark != shown_atom:
                try:
                    nodes.append(json.loads(atom))
                except ValueError:
                    pass
            shown_atom = mark
            continue
        shown_atom = None
        marks = _marks(fmt, heading)
        text = fragment.text().replace(OBJECT, "")
        for index, part in enumerate(text.split(LINE_BREAK)):
            if index:
                nodes.append({"type": "hardBreak"})
            if not part:
                continue
            last = nodes[-1] if nodes else None
            if last and last.get("type") == "text" and last.get("marks", []) == marks:
                last["text"] += part
            else:
                node = {"type": "text", "text": part}
                if marks:
                    node["marks"] = marks
                nodes.append(node)
    return nodes


def _marks(fmt, heading=False):
    marks = []
    code = bool(fmt.property(P_CODE)) or fmt.fontFixedPitch() or any(
        family.lower() in _mono_families() for family in fmt.fontFamilies() or [])
    if code:
        marks.append({"type": "code"})
    else:
        if _weight(fmt) >= BOLD:
            marks.append({"type": "strong"})
        if fmt.fontItalic():
            marks.append({"type": "em"})
        if fmt.fontUnderline() and not fmt.isAnchor():
            marks.append({"type": "underline"})
        if fmt.fontStrikeOut():
            marks.append({"type": "strike"})
    href = fmt.anchorHref() if fmt.isAnchor() else ""
    if href.startswith(("https://", "http://", "mailto:")):
        marks.append({"type": "link", "attrs": {"href": href}})
    color = fmt.property(P_COLOR)
    if color and not code:
        marks.append({"type": "textColor", "attrs": {"color": str(color)}})
    return marks


# -- mentions -----------------------------------------------------------------
#: Rows the list of people shows before it scrolls.
MENTION_ROWS = 7


class MentionList(QListWidget):
    """Who to mention, under the cursor.

    A child of the page, not a popup window. A popup takes the keyboard, and
    typing goes on while the people are read and while the list narrows. The
    text keeps the focus, and the keys that move in the list come from it.
    """

    #: ``{id, name}`` to mention, or ``{"search": query}`` to ask Jira for.
    chosen = Signal(dict)

    def __init__(self):
        super().__init__()
        self.setObjectName("jiraMentions")
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setUniformItemSizes(True)
        self.itemClicked.connect(self._clicked)
        self.hide()

    def fill(self, rows):
        """``[(text, choice or None)]``; a row without a choice is a note."""
        self.clear()
        for text, choice in rows:
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, choice)
            if choice is None:
                item.setFlags(Qt.NoItemFlags)
            self.addItem(item)
        first = next((index for index in range(self.count())
                      if self.item(index).data(Qt.UserRole) is not None), -1)
        self.setCurrentRow(first)

    def move_by(self, step):
        rows = [index for index in range(self.count())
                if self.item(index).data(Qt.UserRole) is not None]
        if not rows:
            return
        now = self.currentRow()
        at = rows.index(now) if now in rows else -1
        self.setCurrentRow(rows[(at + step) % len(rows)])

    def current_choice(self):
        item = self.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    def _clicked(self, item):
        choice = item.data(Qt.UserRole)
        if choice is not None:
            self.chosen.emit(choice)


def _host(widget):
    """Where the list of people is drawn: the content of the nearest scroll
    area, so it moves with the text; else the window."""
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QScrollArea) and parent.widget() is not None \
                and parent.widget().isAncestorOf(widget):
            return parent.widget()
        parent = parent.parentWidget()
    return widget.window()


# -- the editor ---------------------------------------------------------------
class _Text(QTextEdit):
    """The text: lists nest with Tab, a heading ends at Enter, a checklist's
    box ticks on a click, what is pasted keeps only what Jira can hold, and
    "@" offers the people who can see the issue."""

    #: People to mention are wanted: "" for the first read, else a name.
    people_wanted = Signal(str)

    def __init__(self):
        super().__init__()
        self.setAcceptRichText(True)
        self.setTabChangesFocus(False)
        #: Server takes a mention as ``[~name]`` in plain text.
        self.wiki = False
        self.people = None       # None: not read yet for this issue
        self.searched = set()    # names Jira was asked for
        self.people_state = ""   # "", "loading", "searching", "failed"
        self.token = None        # (position of "@", what follows it)
        self.mentions = MentionList()
        self.mentions.chosen.connect(self._chosen)
        self.textChanged.connect(self._follow)
        self.cursorPositionChanged.connect(self._follow)

    # -- the people ------------------------------------------------------
    def set_people(self, people, query=""):
        known = {one["id"] for one in self.people or []}
        self.people = list(self.people or []) + [
            one for one in people or [] if one.get("id") and one["id"] not in known]
        if query:
            self.searched.add(query.lower())
        self.people_state = ""
        self._follow()

    def people_failed(self):
        self.people_state = "failed"
        self._follow()

    def forget_people(self):
        self.people, self.searched, self.people_state = None, set(), ""
        self._close()

    # -- following "@" ---------------------------------------------------
    def _find_token(self):
        cursor = self.textCursor()
        if cursor.hasSelection() or self.isReadOnly() or not self.hasFocus():
            return None
        block = cursor.block()
        before = block.text()[:cursor.positionInBlock()]
        at = before.rfind("@")
        if at < 0:
            return None
        if at and not before[at - 1].isspace() and before[at - 1] not in "([{\"'":
            return None
        query = before[at + 1:]
        if len(query) > 40 or query.count(" ") > 2 or LINE_BREAK in query:
            return None
        inside = QTextCursor(block)
        inside.setPosition(block.position() + at + 1)
        if inside.charFormat().property(P_ATOM):
            return None  # the "@" of a mention already made
        return block.position() + at, query

    def _follow(self, *_args):
        token = self._find_token()
        if token is None:
            self._close()
            return
        self.token = token
        if self.people is None and self.people_state != "loading":
            self.people_state = "loading"
            self.people_wanted.emit("")
        self._show()

    def _rows(self):
        query = self.token[1].strip().lower()
        found = [one for one in self.people or [] if query in one["name"].lower()]
        found.sort(key=lambda one: (not one["name"].lower().startswith(query),
                                    one["name"].lower()))
        rows = [(one["name"], one) for one in found]
        if self.people_state == "loading":
            rows.append(("Loading people…", None))
        elif self.people_state == "searching":
            rows.append(("Looking in Jira…", None))
        elif not found and query and query not in self.searched:
            rows.append(('Search Jira for "%s"' % self.token[1].strip(),
                         {"search": self.token[1].strip()}))
        elif not found and query:
            rows.append(("Nobody by that name can see this issue", None))
        elif self.people_state == "failed":
            rows.append(("People could not be read", None))
        elif not found:
            rows.append(("Type a name", None))
        return rows

    def _show(self):
        host = _host(self)
        if self.mentions.parentWidget() is not host:
            self.mentions.setParent(host)
        rows = self._rows()
        self.mentions.fill(rows)
        line = self.mentions.sizeHintForRow(0) if self.mentions.count() else 22
        height = min(len(rows), MENTION_ROWS) * max(line, 18) + 2 * self.mentions.frameWidth()
        width = 280
        rect = self.cursorRect()
        below = self.viewport().mapTo(host, rect.bottomLeft())
        above = self.viewport().mapTo(host, rect.topLeft())
        y = below.y() + 4
        if y + height > host.height() and above.y() - height - 4 >= 0:
            y = above.y() - height - 4
        x = max(0, min(below.x(), host.width() - width))
        self.mentions.setGeometry(x, y, width, height)
        self.mentions.show()
        self.mentions.raise_()

    def _close(self):
        self.token = None
        self.mentions.hide()

    def _chosen(self, choice):
        if self.token is None:
            return
        if "search" in choice:
            self.people_state = "searching"
            self.people_wanted.emit(choice["search"])
            self._show()
            return
        start, _query = self.token
        cursor = self.textCursor()
        end = cursor.position()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.KeepAnchor)
        plain = plain_format(cursor.charFormat())
        cursor.beginEditBlock()
        if self.wiki:
            cursor.insertText("[~%s] " % choice["id"], plain)
        else:
            node = {"type": "mention", "attrs": {"id": choice["id"],
                                                 "text": "@" + choice["name"]}}
            cursor.insertText("@" + choice["name"], atom_format(node, plain))
            cursor.insertText(" ", plain)
        cursor.endEditBlock()
        self.setTextCursor(cursor)
        self.setCurrentCharFormat(plain)
        self._close()

    # -- keys --------------------------------------------------------------
    def event(self, event):
        # Escape closes the list of people before anything around the editor
        # takes it as "stop editing".
        if (event.type() == QEvent.ShortcutOverride and self.mentions.isVisible()
                and event.key() == Qt.Key_Escape):
            event.accept()
            return True
        return super().event(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self._close()

    def keyPressEvent(self, event):
        key = event.key()
        if self.mentions.isVisible():
            if key in (Qt.Key_Down, Qt.Key_Up):
                self.mentions.move_by(1 if key == Qt.Key_Down else -1)
                return
            if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab):
                choice = self.mentions.current_choice()
                if choice is not None:
                    self._chosen(choice)
                    return
            if key == Qt.Key_Escape:
                self._close()
                return
        cursor = self.textCursor()
        if key in (Qt.Key_Backspace, Qt.Key_Delete) and not cursor.hasSelection() \
                and _remove_atom(cursor, forward=key == Qt.Key_Delete):
            return
        if event.text() and event.text().isprintable() and \
                self.currentCharFormat().property(P_ATOM):
            # Typing after a mention is typing text, not more of the mention.
            self.setCurrentCharFormat(plain_format(self.currentCharFormat()))
        if key in (Qt.Key_Tab, Qt.Key_Backtab) and cursor.currentList() is not None:
            shift(self, -1 if key == Qt.Key_Backtab or
                  event.modifiers() & Qt.ShiftModifier else 1)
            return
        if key in (Qt.Key_Return, Qt.Key_Enter) and not event.modifiers() & Qt.ShiftModifier:
            fmt = cursor.blockFormat()
            if fmt.headingLevel() and cursor.atBlockEnd():
                cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
                self.setTextCursor(cursor)
                return
            if (_code_block(fmt) or fmt.property(P_QUOTE)) and not cursor.block().text() \
                    and not _same_group(cursor.block().next(), fmt):
                # Enter on an empty last line leaves the code block or quote.
                set_block_kind(self, "text")
                return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        cursor = self.cursorForPosition(event.position().toPoint())
        block = cursor.block()
        marker = block.blockFormat().marker()
        if marker != QTextBlockFormat.MarkerType.NoMarker and block.textList() is not None:
            start = QTextCursor(block)
            left = self.cursorRect(start).left()
            x = event.position().x()
            if left - 24 <= x < left:
                fmt = block.blockFormat()
                fmt.setMarker(QTextBlockFormat.MarkerType.Unchecked
                              if marker == QTextBlockFormat.MarkerType.Checked
                              else QTextBlockFormat.MarkerType.Checked)
                start.setBlockFormat(fmt)
                return
        super().mousePressEvent(event)

    def insertFromMimeData(self, source):
        """Pasted formatting goes through ADF and back: what Jira cannot hold
        is left behind here, not found out on Save."""
        if source.hasHtml() and not self.wiki:
            pasted = QTextDocument()
            pasted.setHtml(source.html())
            clean = QTextDocument()
            load(clean, dump(pasted))
            self.textCursor().insertFragment(QTextDocumentFragment(clean))
            self.ensureCursorVisible()
            return
        if source.hasText():
            self.textCursor().insertText(source.text())
            self.ensureCursorVisible()
            return
        super().insertFromMimeData(source)


def _remove_atom(cursor, forward=False):
    """Backspace or Delete next to a mention takes the whole of it."""
    probe = QTextCursor(cursor)
    if forward:
        if probe.atEnd():
            return False
        probe.movePosition(QTextCursor.NextCharacter)
    fmt = probe.charFormat()
    atom = fmt.property(P_ATOM_ID)
    if not fmt.property(P_ATOM) or atom is None:
        return False
    block = probe.block()
    it = block.begin()
    start = end = None
    while not it.atEnd():
        fragment = it.fragment()
        it += 1
        if fragment.isValid() and fragment.charFormat().property(P_ATOM_ID) == atom:
            start = fragment.position() if start is None else start
            end = fragment.position() + fragment.length()
    if start is None:
        return False
    whole = QTextCursor(cursor)
    whole.setPosition(start)
    whole.setPosition(end, QTextCursor.KeepAnchor)
    whole.removeSelectedText()
    return True


def _same_group(block, fmt):
    if not block.isValid():
        return False
    other = block.blockFormat()
    return (other.property(P_GROUP) == fmt.property(P_GROUP)
            and (_code_block(other) or bool(other.property(P_QUOTE))))


def shift(editor, step):
    """Move the list item at the cursor one level in or out. Out of the first
    level, it is a paragraph again."""
    cursor = editor.textCursor()
    listed = cursor.currentList()
    if listed is None:
        return
    fmt = QTextListFormat(listed.format())
    level = max(1, fmt.indent()) + step
    block = cursor.block()
    listed.remove(block)
    plain = cursor.blockFormat()
    plain.setIndent(0)
    plain.setObjectIndex(-1)
    if level < 1:
        plain.setMarker(QTextBlockFormat.MarkerType.NoMarker)
    cursor.setBlockFormat(plain)
    if level < 1:
        return
    # Join a list at that level just above, if there is one; else start one.
    above = block.previous()
    while above.isValid() and above.textList() is not None:
        other = above.textList()
        if max(1, other.format().indent()) == level:
            other.add(block)
            return
        if max(1, other.format().indent()) < level:
            break
        above = above.previous()
    kind = ("taskList" if fmt.property(P_TASK) else
            "orderedList" if fmt.style() in ORDERED else "bulletList")
    cursor.createList(_list_format(kind, level))


def toggle_list(editor, kind):
    """Make the blocks at the cursor a list of ``kind``, or - if they are one
    already - paragraphs again."""
    cursor = editor.textCursor()
    listed = cursor.currentList()
    if listed is not None:
        fmt = listed.format()
        now = ("taskList" if fmt.property(P_TASK) else
               "orderedList" if fmt.style() in ORDERED else "bulletList")
        blocks = _selected_blocks(cursor)
        for block in blocks:
            if block.textList() is not None:
                block.textList().remove(block)
            one = QTextCursor(block)
            plain = one.blockFormat()
            plain.setIndent(0)
            plain.setObjectIndex(-1)
            plain.setMarker(QTextBlockFormat.MarkerType.NoMarker)
            one.setBlockFormat(plain)
        if now == kind:
            return
    set_block_kind(editor, "text")
    cursor = editor.textCursor()
    cursor.beginEditBlock()
    if kind == "taskList":
        for block in _selected_blocks(cursor):
            one = QTextCursor(block)
            fmt = one.blockFormat()
            fmt.setMarker(QTextBlockFormat.MarkerType.Unchecked)
            one.setBlockFormat(fmt)
    cursor.createList(_list_format(kind, 1))
    cursor.endEditBlock()


def _selected_blocks(cursor):
    document = cursor.document()
    first = document.findBlock(cursor.selectionStart())
    last = document.findBlock(cursor.selectionEnd())
    blocks, block = [], first
    while block.isValid():
        blocks.append(block)
        if block == last:
            break
        block = block.next()
    return blocks


def set_block_kind(editor, kind, level=0):
    """Make the blocks at the cursor text, a heading, code or a quote. Text
    keeps a list item in its list; the others take it out."""
    cursor = editor.textCursor()
    cursor.beginEditBlock()
    group = _new_group(cursor.document())
    for block in _selected_blocks(cursor):
        one = QTextCursor(block)
        if kind != "text" and block.textList() is not None:
            block.textList().remove(block)
        fmt = _plain_block_format(one.blockFormat(), keep_list=kind == "text")
        if kind == "heading":
            fmt = _heading_block(level, fmt)
        elif kind == "code":
            fmt = _code_block_format(group)
        elif kind == "quote":
            fmt = _quote_format(group)
        one.setBlockFormat(fmt)
        _restyle_words(one, block, kind, level)
    cursor.endEditBlock()


def _plain_block_format(fmt, keep_list=True):
    """A block's format with nothing of a heading, code, quote or rule left."""
    if not keep_list or fmt.objectIndex() < 0:
        plain = QTextBlockFormat()
        if fmt.property(P_CONTINUES):
            plain.setProperty(P_CONTINUES, fmt.property(P_CONTINUES))
            plain.setIndent(fmt.indent())
        return plain
    plain = QTextBlockFormat(fmt)
    plain.setHeadingLevel(0)
    plain.setTopMargin(0)
    plain.setBottomMargin(0)
    for name in (P_CODE_BLOCK, P_LANGUAGE, P_QUOTE, P_GROUP,
                 QTextFormat.BlockTrailingHorizontalRulerWidth):
        plain.clearProperty(name)
    plain.setNonBreakableLines(False)
    plain.clearBackground()
    plain.setLeftMargin(0)
    return plain


def _new_group(document):
    used, block = 0, document.begin()
    while block.isValid():
        value = block.blockFormat().property(P_GROUP)
        if isinstance(value, int):
            used = max(used, value)
        block = block.next()
    return used + 1


def _restyle_words(cursor, block, kind, level):
    """The size and weight a block's words are shown at, for what it now is."""
    it = block.begin()
    spans = []
    while not it.atEnd():
        fragment = it.fragment()
        it += 1
        if fragment.isValid():
            spans.append((fragment.position(), fragment.length(), fragment.charFormat()))
    for position, length, fmt in spans:
        cursor.setPosition(position)
        cursor.setPosition(position + length, QTextCursor.KeepAnchor)
        fresh = QTextCharFormat(fmt)
        fresh.setProperty(QTextFormat.FontSizeAdjustment,
                          HEADING_SIZE.get(level, 0) if kind == "heading" else 0)
        bold = _weight(fmt) >= BOLD
        fresh.setFontWeight(BOLD if bold else HEADING_WEIGHT if kind == "heading" else 400)
        if kind == "code":
            # A code block's text carries no marks of its own.
            fresh = _code_text_format()
        elif fmt.fontFixedPitch() and not fmt.property(P_CODE):
            # Out of a code block: its lines are words again.
            fresh.setFontFixedPitch(False)
            fresh.setFontFamilies(list(theme.FONT_BODY))
        cursor.setCharFormat(fresh)
    block_char = QTextCharFormat()
    if kind == "heading":
        block_char = _heading_format(level)
    elif kind == "code":
        block_char = _code_text_format()
    cursor.setPosition(block.position())
    cursor.setBlockCharFormat(block_char)


class DescriptionEditor(QFrame):
    """A description or a comment with its formatting, and a bar of what can
    be done to it. On Server it is text, with the bar put away: Server keeps a
    description or a comment as text."""

    #: Something in the text or its formatting changed.
    changed = Signal()
    #: People to mention are wanted: "" for the first read, else a name.
    people_wanted = Signal(str)

    STYLES = (("Text", 0), ("Heading 1", 1), ("Heading 2", 2), ("Heading 3", 3),
              ("Heading 4", 4), ("Heading 5", 5), ("Heading 6", 6))

    def __init__(self, parent=None, name="Description", placeholder="No description",
                 shortest=110):
        super().__init__(parent)
        self.setObjectName("jiraDescriptionEditor")
        self._shortest = shortest
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        self.bar = QFrame()
        self.bar.setObjectName("jiraFormatBar")
        row = QHBoxLayout(self.bar)
        row.setContentsMargins(4, 3, 4, 3)
        row.setSpacing(2)
        self.style_box = widgets.capped(QComboBox(), 140)
        self.style_box.setAccessibleName("Paragraph style")
        self.style_box.setToolTip("Paragraph style")
        for text, level in self.STYLES:
            self.style_box.addItem(text, level)
        self.style_box.activated.connect(self._style_picked)
        row.addWidget(self.style_box)
        row.addSpacing(6)
        self.buttons = {}
        for name, text, tip in (("bold", "B", "Bold (Ctrl+B)"),
                                ("italic", "I", "Italic (Ctrl+I)"),
                                ("underline", "U", "Underline (Ctrl+U)"),
                                ("strike", "S", "Strikethrough (Ctrl+Shift+S)"),
                                ("code", "", "Code (Ctrl+Shift+M)")):
            self.buttons[name] = self._button(row, name, text, tip, checkable=True)
        row.addSpacing(6)
        for name, tip in (("list_bullet", "Bulleted list (Ctrl+Shift+8)"),
                          ("list_number", "Numbered list (Ctrl+Shift+7)"),
                          ("checklist", "Checklist"),
                          ("link", "Link"),
                          ("code_block", "Code block"),
                          ("quote", "Quote")):
            self.buttons[name] = self._button(row, name, "", tip,
                                              checkable=name not in ("link",))
        row.addSpacing(6)
        self.buttons["clear"] = self._button(row, "clear_format", "",
                                             "Clear formatting (Ctrl+\\)")
        row.addStretch(1)
        column.addWidget(self.bar)
        self.text = _Text()
        self.text.setObjectName("jiraDescriptionText")
        self.text.setAccessibleName(name)
        self.text.setPlaceholderText(placeholder)
        self.text.textChanged.connect(self.changed)
        self.text.people_wanted.connect(self.people_wanted)
        self.text.cursorPositionChanged.connect(self._show_state)
        self.text.currentCharFormatChanged.connect(self._show_state)
        self.text.document().documentLayout().documentSizeChanged.connect(self._fit)
        column.addWidget(self.text)
        for keys, act in (("Ctrl+B", lambda: self.toggle("bold")),
                          ("Ctrl+I", lambda: self.toggle("italic")),
                          ("Ctrl+U", lambda: self.toggle("underline")),
                          ("Ctrl+Shift+S", lambda: self.toggle("strike")),
                          ("Ctrl+Shift+M", lambda: self.toggle("code")),
                          ("Ctrl+Shift+8", lambda: self.act("list_bullet")),
                          ("Ctrl+Shift+7", lambda: self.act("list_number")),
                          ("Ctrl+\\", lambda: self.act("clear"))):
            QShortcut(QKeySequence(keys), self.text, act).setContext(Qt.WidgetShortcut)
        self._baseline = None
        self._fit()

    def _button(self, row, name, text, tip, checkable=False):
        button = QToolButton()
        button.setObjectName("jiraFormat_" + name)
        button.setToolTip(tip)
        button.setAccessibleName(tip.split(" (")[0])
        button.setCheckable(checkable)
        button.setFocusPolicy(Qt.NoFocus)
        if text:
            button.setText(text)
            font = QFont(button.font())
            font.setBold(name == "bold")
            font.setItalic(name == "italic")
            font.setUnderline(name == "underline")
            font.setStrikeOut(name == "strike")
            button.setFont(font)
        else:
            button.setIcon(icons.icon(name, theme.NEUTRAL[800]))
        key = {"clear_format": "clear"}.get(name, name)
        if name in ("bold", "italic", "underline", "strike", "code"):
            button.clicked.connect(lambda _c=False, n=key: self.toggle(n))
        else:
            button.clicked.connect(lambda _c=False, n=key: self.act(n))
        row.addWidget(button)
        return button

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.StyleChange, QEvent.PaletteChange):
            for name, button in self.buttons.items():
                if not button.text():
                    button.setIcon(icons.icon({"clear": "clear_format"}.get(name, name),
                                              theme.NEUTRAL[800]))

    # -- what it holds --------------------------------------------------------
    def set_document(self, doc):
        load(self.text.document(), doc)
        self._baseline = dump(self.text.document())
        self.text.moveCursor(QTextCursor.Start)
        self._show_state()
        self._fit()

    def document(self):
        """What is in the editor, as ADF."""
        return dump(self.text.document())

    def changed_document(self):
        """The document, if it differs from what was set; else None."""
        if not self.text.document().isModified():
            return None
        now = self.document()
        return None if now == self._baseline else now

    def setReadOnly(self, read_only):
        self.text.setReadOnly(read_only)
        self.bar.setEnabled(not read_only)

    def set_plain(self, plain):
        """Text only - Server's way - with mentions written as ``[~name]``."""
        self.bar.setVisible(not plain)
        self.text.wiki = plain
        self.text.setAcceptRichText(not plain)

    def is_plain(self):
        return self.text.wiki

    def body(self):
        """What to send: the document, or on Server the text."""
        return self.text.toPlainText() if self.text.wiki else self.document()

    def toPlainText(self):
        return self.text.toPlainText().replace(LINE_BREAK, "\n")

    def setPlainText(self, text):
        self.text.setPlainText(text)

    def clear(self):
        self.text.clear()
        self.text.setCurrentCharFormat(QTextCharFormat())

    # -- people to mention ------------------------------------------------------
    def set_people(self, people, query=""):
        self.text.set_people(people, query)

    def people_failed(self):
        self.text.people_failed()

    def forget_people(self):
        self.text.forget_people()

    def setFocus(self, *args):
        self.text.setFocus(*args)

    # -- doing things to it ---------------------------------------------------
    def toggle(self, name):
        if self.text.isReadOnly():
            return
        fmt = self.text.currentCharFormat()
        change = QTextCharFormat()
        heading = self.text.textCursor().blockFormat().headingLevel()
        if name == "bold":
            change.setFontWeight((HEADING_WEIGHT if heading else 400)
                                 if _weight(fmt) >= BOLD else BOLD)
        elif name == "italic":
            change.setFontItalic(not fmt.fontItalic())
        elif name == "underline":
            change.setFontUnderline(not fmt.fontUnderline())
        elif name == "strike":
            change.setFontStrikeOut(not fmt.fontStrikeOut())
        elif name == "code":
            if fmt.property(P_CODE) or fmt.fontFixedPitch():
                change.setProperty(P_CODE, False)
                change.setFontFixedPitch(False)
                change.setFontFamilies(list(theme.FONT_BODY))
                change.setBackground(QBrush())
            else:
                change = code_format(change)
        self.text.mergeCurrentCharFormat(change)
        self._show_state()

    def act(self, name):
        if self.text.isReadOnly():
            return
        if name == "list_bullet":
            toggle_list(self.text, "bulletList")
        elif name == "list_number":
            toggle_list(self.text, "orderedList")
        elif name == "checklist":
            toggle_list(self.text, "taskList")
        elif name in ("code_block", "quote"):
            fmt = self.text.textCursor().blockFormat()
            on = _code_block(fmt) if name == "code_block" else bool(fmt.property(P_QUOTE))
            set_block_kind(self.text, "text" if on else
                           ("code" if name == "code_block" else "quote"))
        elif name == "link":
            self._link()
        elif name == "clear":
            self._clear()
        self._show_state()
        self.text.setFocus()

    def _style_picked(self, index):
        level = self.style_box.itemData(index) or 0
        set_block_kind(self.text, "heading" if level else "text", level)
        self.text.setFocus()
        self._show_state()

    def _link(self):
        cursor = self.text.textCursor()
        fmt = cursor.charFormat()
        current = fmt.anchorHref() if fmt.isAnchor() else ""
        if not cursor.hasSelection() and current:
            # Inside a link with nothing selected: the whole link is meant.
            cursor.select(QTextCursor.WordUnderCursor)
        href = widgets.ask_text(self, "Link", "Address the text links to",
                                value=current or "https://",
                                detail="Leave it empty to take the link off.")
        if href is None:
            return
        href = href.strip()
        if href in ("https://", "http://"):
            href = ""
        if href and not href.startswith(("https://", "http://", "mailto:")):
            href = "https://" + href
        if not cursor.hasSelection():
            if not href:
                return
            cursor.insertText(href, link_format(href, QTextCharFormat()))
            cursor.setCharFormat(QTextCharFormat())
            self.text.setTextCursor(cursor)
            return
        change = QTextCharFormat()
        if href:
            change = link_format(href, change)
        else:
            change.setAnchor(False)
            change.setAnchorHref("")
            change.setForeground(QBrush())
        cursor.mergeCharFormat(change)

    def _clear(self):
        cursor = self.text.textCursor()
        if not cursor.hasSelection():
            cursor.select(QTextCursor.BlockUnderCursor)
        heading = cursor.blockFormat().headingLevel()
        plain = QTextCharFormat()
        plain.setFontWeight(HEADING_WEIGHT if heading else 400)
        plain.setFontItalic(False)
        plain.setFontUnderline(False)
        plain.setFontStrikeOut(False)
        plain.setProperty(P_CODE, False)
        plain.setProperty(P_COLOR, "")
        plain.setFontFixedPitch(False)
        plain.setFontFamilies(list(theme.FONT_BODY))
        plain.setBackground(QBrush())
        cursor.mergeCharFormat(plain)

    def _show_state(self, *_args):
        fmt = self.text.currentCharFormat()
        block = self.text.textCursor().blockFormat()
        listed = self.text.textCursor().currentList()
        kind = ""
        if listed is not None:
            lf = listed.format()
            kind = ("checklist" if lf.property(P_TASK) else
                    "list_number" if lf.style() in ORDERED else "list_bullet")
        states = {
            "bold": _weight(fmt) >= BOLD, "italic": fmt.fontItalic(),
            "underline": fmt.fontUnderline() and not fmt.isAnchor(),
            "strike": fmt.fontStrikeOut(),
            "code": bool(fmt.property(P_CODE)) or fmt.fontFixedPitch(),
            "list_bullet": kind == "list_bullet", "list_number": kind == "list_number",
            "checklist": kind == "checklist", "code_block": _code_block(block),
            "quote": bool(block.property(P_QUOTE)),
        }
        for name, on in states.items():
            button = self.buttons[name]
            button.blockSignals(True)
            button.setChecked(bool(on))
            button.blockSignals(False)
        index = self.style_box.findData(block.headingLevel() or 0)
        self.style_box.blockSignals(True)
        self.style_box.setCurrentIndex(max(0, index))
        self.style_box.blockSignals(False)

    def _fit(self, *_args):
        """As tall as its text, up to :data:`MAX_HEIGHT`; past that it scrolls."""
        height = int(self.text.document().size().height()) + 2 * self.text.frameWidth() + 6
        self.text.setFixedHeight(max(self._shortest, min(MAX_HEIGHT, height)))
