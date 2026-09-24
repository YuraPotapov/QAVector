"""Approval formatting retains the evidence the decision is based on."""

from PySide6.QtCore import QUrl
from PySide6.QtGui import QTextDocument

from cms_gui.approvaltext import ApprovalText, review_markdown


def test_findings_and_recommendations_are_readable_without_losing_fields(qapp):
    detail = ("The revised plan.\n\nPackages:\n"
              "[{'severity': 'high', 'description': 'Cover the fallback', 'file': 'model.py', "
              "'line': 154, 'custom_evidence': 'the retry path'}]\n\n"
              "Where it lands:\n['Add a focused test', 'Keep the old behavior']\n\nStill open:\n[]")
    view = ApprovalText(detail)
    try:
        text = view.toPlainText()
        for value in ("The revised plan", "Packages", "High priority", "Cover the fallback",
                      "model.py", "154", "the retry path", "Add a focused test",
                      "Keep the old behavior", "Still open", "None reported"):
            assert value in text
        assert "'description':" not in text
        assert "['Add a focused test'" not in text
    finally:
        view.deleteLater()


def test_authored_markdown_and_diff_lines_remain_readable(qapp):
    view = ApprovalText("## Plan\n\n- **Check** the change\n- Keep `a_b` intact")
    try:
        assert "Check the change" in view.toPlainText()
        assert "**Check**" not in view.toPlainText()
        diff = "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new"
        view.document().setMarkdown(review_markdown(diff))
        assert diff in view.toPlainText()
    finally:
        view.deleteLater()


def test_unreadable_serialized_data_is_retained_instead_of_discarded():
    source = "Packages:\n[{'description': 'an interrupted record'"
    assert "[{'description': 'an interrupted record'" in review_markdown(source)


def test_document_cannot_load_local_or_remote_images_or_open_links(qapp):
    view = ApprovalText("![local](file:///private/file.png)\n\n[link](https://example.invalid)")
    try:
        for url in ("file:///private/file.png", "https://example.invalid/image.png"):
            assert view.loadResource(QTextDocument.ImageResource, QUrl(url)) is None
        assert not view.openLinks()
        assert not view.openExternalLinks()
    finally:
        view.deleteLater()


def test_code_blocks_are_not_reinterpreted_as_approval_sections():
    code = "```python\nRisk: high\n[{'description': 'example code'}]\n```"
    assert review_markdown(code) == code
