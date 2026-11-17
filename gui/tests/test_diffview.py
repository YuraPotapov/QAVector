"""A patch read file by file, and the words for tokens and context."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtGui import QColor                                      # noqa: E402

from cms_gui import diffview, theme                                  # noqa: E402
from cms_gui.pages import agentwords                                 # noqa: E402

PATCH = ("diff --git a/app.py b/app.py\nindex 1..2 100644\n--- a/app.py\n+++ b/app.py\n"
         "@@ -1,2 +1,2 @@\n one\n-two\n+TWO\n"
         "diff --git a/old.txt b/old.txt\ndeleted file mode 100644\n--- a/old.txt\n"
         "+++ /dev/null\n@@ -1 +0,0 @@\n-going\n"
         "diff --git a/a.py b/b.py\nsimilarity index 90%\nrename from a.py\nrename to b.py\n"
         "diff --git a/my file.txt b/my file.txt\nnew file mode 100644\n--- /dev/null\n"
         "+++ b/my file.txt\n@@ -0,0 +1 @@\n+hi\n")


def test_a_patch_is_split_into_the_files_it_is_about():
    parts = diffview.split(PATCH)
    assert list(parts) == ["app.py", "old.txt", "b.py", "my file.txt"]
    assert parts["app.py"].startswith("diff --git a/app.py") and "+TWO" in parts["app.py"]
    assert "-going" in parts["old.txt"] and "+hi" in parts["my file.txt"]
    assert diffview.split("") == {}


def test_the_totals_say_how_many_files_and_lines():
    assert diffview.summary_text({}) == "No files changed."
    assert diffview.summary_text({"files": [{}], "added": 1, "removed": 0}) == (
        "1 file changed · +1 -0")
    assert diffview.summary_text({"files": [{}, {}], "added": 12, "removed": 4}) == (
        "2 files changed · +12 -4")


def test_each_file_is_a_section_folded_shut_and_laid_out_when_opened(qapp):
    view = diffview.DiffView()
    view.set_changes({"files": [
        {"path": "app.py", "status": "modified", "added": 1, "removed": 1, "binary": False},
        {"path": "big.dat", "status": "written", "added": 0, "removed": 0, "binary": False}],
        "patch": PATCH, "added": 1, "removed": 1, "unkept": ["data"], "truncated": True})
    assert view.summary.text() == "2 files changed · +1 -1"
    assert "Too large to compare every turn: data" in view.note.text()
    assert "longer than what is kept" in view.note.text()
    app, big = view.sections
    assert (app.is_expanded(), app.text) == (False, None)
    view.set_all(True)
    assert app.text.toPlainText() == diffview.split(PATCH)["app.py"].rstrip("\n")
    assert big.text.text().startswith("Not compared")
    view.set_changes({})
    assert view.sections == [] and view.expand.isHidden()


def test_added_and_removed_lines_are_coloured_by_the_theme(qapp):
    text = diffview.PatchText(diffview.split(PATCH)["app.py"])
    document = text.document()

    def ground(line):
        block = document.findBlockByLineNumber(line)
        formats = block.layout().formats()
        return formats[0].format.background().color() if formats else None

    lines = text.toPlainText().splitlines()
    assert ground(lines.index("+TWO")) == QColor(theme.DIFF_ADD_BG)
    assert ground(lines.index("-two")) == QColor(theme.DIFF_DEL_BG)
    assert ground(lines.index(" one")) is None


def test_tokens_and_context_read_at_a_glance():
    assert [agentwords.count_text(n) for n in (0, 933, 1000, 9627, 24813, 1000000, 1250000)] == [
        "0", "933", "1k", "9.6k", "25k", "1M", "1.2M"]
    assert agentwords.context_text({"used": 24813, "window": 1000000}) == "25k of 1M"
    assert agentwords.context_text({"used": 900}) == "900"
    assert agentwords.context_text({}) == ""
    tokens = {"input": 10, "cache_read": 101029, "cache_write": 3948, "output": 2037,
              "thinking": 1415}
    assert agentwords.tokens_total(tokens) == 107024        # thinking is part of output
    assert agentwords.tokens_text(tokens) == "105k in (101k cached) · 2k out"
    assert agentwords.tokens_text({}) == ""
    assert agentwords.end_text({"steps": 2, "cost_usd": 0.04,
                                "context": {"used": 24813, "window": 200000}}) == (
        "2 steps · est. $0.04 · context 25k of 200k")
