"""Readable approval documents without changing the question or its source.

Cycle variables embedded in a detail string can be Python/JSON lists. Render
those as findings and recommendations, and use Qt Markdown for authored prose.
This view never loads linked resources or executes document markup.
"""

import ast
import json
import re

from PySide6.QtGui import QTextCursor, QTextDocument
from PySide6.QtWidgets import QTextBrowser

from . import theme


def _literal(value):
    text = str(value)
    return re.sub(r"([\\`*_{}\[\]<>#|])", r"\\\1", text).replace("\n", "  \n")


def _code(text):
    runs = re.findall(r"`+", text)
    fence = "`" * max(3, max((len(one) + 1 for one in runs), default=3))
    return fence + "\n" + text + "\n" + fence


def _structured(text):
    if len(text) > 64000 or not text.startswith(("[", "{")):
        return None
    for parse in (json.loads, ast.literal_eval):
        try:
            value = parse(text)
            if isinstance(value, (list, dict)):
                return value
        except (ValueError, SyntaxError, TypeError, RecursionError, MemoryError):
            pass
    return None


def _items(value, depth=0):
    if depth > 4:
        return _code(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    if not value:
        return "None reported."
    if isinstance(value, list):
        parts = []
        for index, item in enumerate(value, 1):
            if isinstance(item, dict):
                severity = str(item.get("severity") or "").strip()
                heading = severity.capitalize() + " priority" if severity else "Item"
                parts.append("### %d. %s\n\n%s" % (
                    index, _literal(heading), _items(item, depth + 1)))
            elif isinstance(item, list):
                parts.append(_items(item, depth + 1))
            else:
                parts.append("- " + _literal(item))
        return "\n\n".join(parts)
    parts = []
    for key, item in value.items():
        if key == "description" and isinstance(item, str):
            parts.append(_literal(item))
            continue
        label = _literal(str(key).replace("_", " ").capitalize())
        if isinstance(item, (list, dict)):
            parts.append("**%s**\n\n%s" % (label, _items(item, depth + 1)))
        else:
            parts.append("**%s:** %s" % (label, _literal(item)))
    return "\n\n".join(parts)


def review_markdown(detail):
    text = str(detail or "").strip()
    if text.startswith(("diff --git ", "--- ", "+++ ", "@@ ")):
        return _code(text)
    parts = []
    fenced = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
            parts.append(line)
            continue
        if fenced:
            parts.append(line)
            continue
        structured = _structured(stripped)
        if structured is not None:
            parts.extend(("", _items(structured), ""))
        elif re.fullmatch(r"[A-Z][A-Za-z /-]{1,64}:", stripped):
            parts.extend(("", "## " + stripped[:-1], ""))
        elif re.match(r"^(Risk|First review|After the second pass):\s", stripped):
            label, value = stripped.split(":", 1)
            parts.extend(("", "**%s:** %s" % (label, _literal(value.strip())), ""))
        else:
            parts.append(line)
    return "\n".join(parts)


def review_facts(detail):
    found = []
    for name, label in (("Risk", "Risk"), ("First review", "First review"),
                        ("After the second pass", "Second review")):
        match = re.search(r"^%s:\s*([^\n]+)" % name, detail, re.MULTILINE)
        if match:
            found.append((label, match.group(1).strip()))
    return found


class ApprovalText(QTextBrowser):
    def __init__(self, detail, parent=None):
        super().__init__(parent)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setReadOnly(True)
        self.setStyleSheet("font-size: 14px;")
        self.document().setDocumentMargin(20)
        self.document().setDefaultStyleSheet(
            "p, li { line-height: 145%%; } "
            "h1, h2, h3 { margin-top: 22px; margin-bottom: 10px; } "
            "h2 { font-size: 18px; } h3 { font-size: 15px; } "
            "pre, code { font-family: %s; }" % theme.MONO_CSS)
        self.document().setMarkdown(
            review_markdown(detail),
            QTextDocument.MarkdownDialectGitHub | QTextDocument.MarkdownNoHTML)
        self.moveCursor(QTextCursor.Start)
        self.verticalScrollBar().setValue(0)

    def loadResource(self, kind, url):  # noqa: N802 - Qt API
        # Markdown images can name network URLs or local files. Approval needs
        # only the supplied text, never an implicit request or file read.
        return None
