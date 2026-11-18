"""Code in what an agent writes: set apart, monospaced, roughly highlighted.

Qt's Markdown reader keeps a fenced block's lines, indentation and language
(``BlockCodeLanguage``) but draws it in the body font with nothing behind it,
and an indented block becomes a plain paragraph - code read as prose. This
gives every code block a ground of its own and the mono font, inline code the
same, and highlights what it can tell apart: a shell command as a command line
(the program, its flags, variables, strings, comments, operators), anything
else by the strings, numbers, comments and keywords most languages share.

Approximate on purpose - a reader, not a parser: a wrong colour on an odd line
costs nothing, a line that cannot be read does.
"""

import re

from PySide6.QtGui import (QColor, QFont, QSyntaxHighlighter, QTextBlockFormat, QTextCharFormat, QTextCursor,
                           QTextFormat, QTextFrameFormat, QTextOption)

from . import theme

SHELL = frozenset(("bash", "sh", "shell", "zsh", "console", "shell-session", "shellsession", "terminal",
                   "powershell", "ps1", "pwsh", "cmd", "bat", "fish"))
#: Words that start a command line: an unlabelled block starting with one is shell.
SHELL_WORDS = frozenset((
    "cd", "ls", "cat", "grep", "rg", "sed", "awk", "find", "echo", "export", "source", "git", "gh", "python",
    "python3", "pip", "pip3", "pytest", "npm", "npx", "yarn", "pnpm", "node", "make", "docker", "kubectl",
    "curl", "wget", "ssh", "scp", "rm", "cp", "mv", "mkdir", "chmod", "chown", "sudo", "apt", "apt-get",
    "brew", "tar", "head", "tail", "sort", "uniq", "wc", "xargs", "env", "go", "cargo", "uv", "poetry",
    "systemctl", "journalctl", "psql", "odoo-bin", "./", "bash", "sh"))
HASH_COMMENTS = frozenset(("python", "py", "yaml", "yml", "toml", "ruby", "rb", "perl", "r", "ini", "conf",
                           "dockerfile", "makefile", "cmake", "nginx", "properties"))
KEYWORDS = frozenset((
    # Python
    "def", "class", "return", "if", "elif", "else", "for", "while", "in", "not", "and", "or", "is", "import",
    "from", "as", "with", "try", "except", "finally", "raise", "pass", "break", "continue", "lambda", "yield",
    "async", "await", "None", "True", "False", "self", "global", "nonlocal", "assert", "del",
    # C-like, JavaScript, Go, Rust, Java
    "function", "const", "let", "var", "new", "this", "null", "undefined", "true", "false", "switch", "case",
    "default", "do", "throw", "catch", "typeof", "instanceof", "export", "extends", "implements", "interface",
    "type", "enum", "public", "private", "protected", "static", "void", "int", "float", "double", "bool",
    "string", "struct", "func", "package", "fn", "mut", "impl", "pub", "use", "match", "where",
    # SQL
    "SELECT", "FROM", "WHERE", "JOIN", "LEFT", "INNER", "ON", "GROUP", "BY", "ORDER", "INSERT", "INTO",
    "UPDATE", "SET", "DELETE", "VALUES", "AND", "OR", "NOT", "NULL", "AS", "LIMIT", "CREATE", "TABLE"))

_STRING = re.compile(r'"(?:[^"\\]|\\.)*"?|\'(?:[^\'\\]|\\.)*\'?|`[^`]*`?')
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
_WORD = re.compile(r"\b[A-Za-z_]\w*\b")
_SHELL_COMMAND = re.compile(r"(?:^|(?<=\|)|(?<=&&)|(?<=\|\|)|(?<=;)|(?<=\$\())\s*(?:\$\s+)?(?:sudo\s+)?"
                            r"(?:[A-Z_][A-Z0-9_]*=\S*\s+)*([\w./~-][\w./~+-]*)")
_SHELL_FLAG = re.compile(r"(?<![\w-])--?[A-Za-z][\w-]*")
_SHELL_VARIABLE = re.compile(r"\$\{[^}]*\}|\$[A-Za-z_]\w*|\$[0-9#?@*$!]")
_SHELL_OPERATOR = re.compile(r"\|\||&&|\||;|>>|2>&1|>|<")
_PROMPT = re.compile(r"^\s*\$\s")


def language_of(block):
    """A block's code language: ``""`` for code of no known language, None
    for a block that is not code at all."""
    shape = block.blockFormat()
    if not shape.hasProperty(QTextFormat.BlockCodeLanguage) and not shape.nonBreakableLines():
        return None
    return str(shape.property(QTextFormat.BlockCodeLanguage) or "").strip().lower()


def guess(lines):
    """The language of a block nobody labelled, from its first line."""
    first = next((line.strip() for line in lines if line.strip()), "")
    if _PROMPT.match(first) or first.split(" ", 1)[0] in SHELL_WORDS or first.startswith("./"):
        return "bash"
    if first.startswith(("def ", "class ", "import ", "from ", "@")):
        return "python"
    return ""


def _shaped(colour, bold=False, italic=False):
    found = QTextCharFormat()
    found.setForeground(QColor(colour))
    if bold:
        found.setFontWeight(QFont.DemiBold)
    if italic:
        found.setFontItalic(True)
    return found


class CodeHighlighter(QSyntaxHighlighter):
    """Colours code blocks by their language; leaves every other block be."""

    def __init__(self, document):
        super().__init__(document)
        self.restyle()

    def restyle(self):
        """Read the theme's inks now: the light or the dark one."""
        self.command = _shaped(theme.ACCENT, bold=True)
        self.flag = _shaped(theme.AUTO)
        self.variable = _shaped(theme.WARN_INK)
        self.operator = _shaped(theme.NEUTRAL[600], bold=True)
        self.string = _shaped(theme.DIFF_ADD_INK)
        self.number = _shaped(theme.WARN_INK)
        self.keyword = _shaped(theme.AUTO, bold=True)
        self.comment = _shaped(theme.NEUTRAL[600], italic=True)
        self.prompt = _shaped(theme.NEUTRAL[500])
        self.rehighlight()

    def highlightBlock(self, text):             # noqa: N802 - Qt's own spelling
        language = language_of(self.currentBlock())
        if language is None:
            return
        if language in SHELL:
            self._shell(text)
        else:
            self._generic(text, language)

    def _paint(self, pattern, text, shape, group=0):
        for found in pattern.finditer(text):
            start, end = found.span(group)
            if end > start:
                self.setFormat(start, end - start, shape)

    def _strings_and_comment(self, text, comment):
        """Strings over everything, and a comment from where one starts outside
        a string. Returns nothing; what it paints is the last word."""
        spans = [found.span() for found in _STRING.finditer(text)]
        for start, end in spans:
            self.setFormat(start, end - start, self.string)
        if not comment:
            return
        for found in re.finditer(comment, text):
            at = found.start()
            if not any(start <= at < end for start, end in spans):
                self.setFormat(at, len(text) - at, self.comment)
                return

    def _shell(self, text):
        prompt = _PROMPT.match(text)
        if prompt:
            self.setFormat(0, prompt.end(), self.prompt)
        self._paint(_SHELL_OPERATOR, text, self.operator)
        self._paint(_SHELL_FLAG, text, self.flag)
        self._paint(_SHELL_COMMAND, text, self.command, group=1)
        self._paint(_SHELL_VARIABLE, text, self.variable)
        self._strings_and_comment(text, r"(?:^|(?<=\s))#")

    def _generic(self, text, language):
        self._paint(_NUMBER, text, self.number)
        for found in _WORD.finditer(text):
            if found.group() in KEYWORDS:
                self.setFormat(found.start(), found.end() - found.start(), self.keyword)
        if language in HASH_COMMENTS:
            comment = r"#"
        elif language in ("sql", "lua", "haskell"):
            comment = r"--"
        elif language in ("json",):
            comment = None
        else:
            comment = r"//|#(?!\[)" if not language else r"//"
        self._strings_and_comment(text, comment)


def style(document):
    """Give the code in ``document`` a frame of its own - ground, border,
    padding - and the mono font. Once per document: :func:`restyle` takes a
    theme change. An unlabelled block's guessed language is written into it,
    where the highlighter reads it."""
    runs, run = [], []
    block = document.begin()
    while block.isValid():
        language = language_of(block)
        if language is not None and (not run or language_of(run[-1]) == language):
            run.append(block)
        else:
            if run:
                runs.append(run)
            run = [block] if language is not None else []
            if language is None:
                _inline(block)
        block = block.next()
    if run:
        runs.append(run)
    # From the last up: a frame put round a run moves the positions after it.
    for run in reversed(runs):
        language = language_of(run[0])
        if language == "":
            language = guess(one.text() for one in run)
        start, end = run[0].position(), run[-1].position() + run[-1].length() - 1
        shape = run[0].blockFormat()
        shape.setProperty(QTextFormat.BlockCodeLanguage, language)
        # Wrapped, not cut off: the feed never scrolls sideways, and a line
        # cut at the edge is a line nobody can read.
        shape.setNonBreakableLines(False)
        shape.setTopMargin(0)
        shape.setBottomMargin(0)
        cursor = QTextCursor(document)
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.KeepAnchor)
        cursor.mergeCharFormat(_mono())
        frame = cursor.insertFrame(_frame())
        # The frame takes its lines in, but not the first one's format; and Qt
        # keeps an empty line on either side of a frame, which is no line here.
        inside = document.findBlock(frame.firstPosition())
        while inside.isValid() and inside.position() <= frame.lastPosition():
            QTextCursor(inside).setBlockFormat(shape)
            inside = inside.next()
        for edge in (document.findBlock(frame.firstPosition() - 1),
                     document.findBlock(frame.lastPosition() + 1)):
            if edge.isValid() and not edge.text() and document.frameAt(edge.position()) is not frame:
                _collapse(edge)
    option = document.defaultTextOption()
    option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
    document.setDefaultTextOption(option)


def _collapse(block):
    shape = QTextBlockFormat()
    shape.setLineHeight(1.0, QTextBlockFormat.LineHeightTypes.FixedHeight.value)
    cursor = QTextCursor(block)
    cursor.setBlockFormat(shape)
    tiny = QTextCharFormat()
    tiny.setFontPointSize(1)
    cursor.setBlockCharFormat(tiny)


def restyle(document):
    """The theme changed: the code frames and inline code take its colours."""
    for frame in document.rootFrame().childFrames():
        frame.setFrameFormat(_frame())
    block = document.begin()
    while block.isValid():
        if language_of(block) is None:
            _inline(block)
        block = block.next()


def _frame():
    shape = QTextFrameFormat()
    shape.setBackground(QColor(theme.SURFACE))
    shape.setBorder(1)
    shape.setBorderBrush(QColor(theme.DIVIDER))
    shape.setBorderStyle(QTextFrameFormat.BorderStyle_Solid)
    shape.setPadding(8)
    shape.setTopMargin(4)
    shape.setBottomMargin(6)
    return shape


def _mono(inline=False):
    shape = QTextCharFormat()
    shape.setFontFamilies(theme.FONT_MONO)
    shape.setFontFixedPitch(True)
    shape.setProperty(QTextFormat.FontSizeAdjustment, 0)
    if inline:
        shape.setBackground(QColor(theme.SURFACE))
    return shape


def _inline(block):
    """`Inline code`: Qt marks it fixed-pitch and nothing more."""
    places = []
    found = block.begin()
    while not found.atEnd():
        fragment = found.fragment()
        if fragment.isValid() and fragment.charFormat().fontFixedPitch():
            places.append((fragment.position(), fragment.length()))
        found += 1
    for start, length in places:
        cursor = QTextCursor(block.document())
        cursor.setPosition(start)
        cursor.setPosition(start + length, QTextCursor.KeepAnchor)
        cursor.mergeCharFormat(_mono(inline=True))
