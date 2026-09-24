"""Readable Jira artifacts preserve structure and never execute task content."""

from copy import deepcopy
from html.parser import HTMLParser

import pytest

from cycle.jira_report import render


def text(value, **extra):
    return dict(type="text", text=value, **extra)


def node(kind, *children, **attrs):
    return {"type": kind, "content": list(children), "attrs": attrs}


def answer(description="", **extra):
    issue = dict(key="QA-1", summary="A readable task", description=description,
                 url="https://jira.example/browse/QA-1", status="In Progress",
                 assignee="Tester", created="2026-09-22T19:00:00.000+0300", comments=[])
    issue.update(extra)
    return {"jql": "project = QA", "issues": [issue]}


class Parsed(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.tags, self.words = [], []
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.words.append(data)


def test_adf_preserves_headings_marks_lists_tables_and_code():
    body = node("doc",
                node("heading", text("Goal"), level=1),
                node("paragraph", text("Important", marks=[{"type": "strong"}]),
                     text(" - details", marks=[{"type": "em"}])),
                node("bulletList", node("listItem", node("paragraph", text("First"))),
                     node("listItem", node("orderedList", node("listItem",
                          node("paragraph", text("Nested"))), order=3))),
                node("table", node("tableRow",
                     node("tableHeader", node("paragraph", text("Field"))),
                     node("tableHeader", node("paragraph", text("Rule")))),
                     node("tableRow", node("tableCell", node("paragraph", text("status"))),
                          node("tableCell", node("paragraph", text("paid"))))),
                node("codeBlock", text('if value < 2:\n    print("ok")')))
    page = render(answer(), descriptions={"QA-1": body})
    for fragment in ("<h3>Goal</h3>", "<strong>Important</strong>", "<em> - details</em>",
                     '<ol start="3">', "<table>", '<th colspan="1" rowspan="1">',
                     '<td colspan="1" rowspan="1">', "if value &lt; 2:"):
        assert fragment in page
    assert [tag for tag, attrs in Parsed(page).tags].count("li") == 3


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,bad",
                                 "file:///etc/passwd", "//evil.example/", "java\nscript:alert(1)"])
def test_issue_content_cannot_create_active_markup_or_unsafe_links(url):
    payload = '<script>alert("bad")</script>'
    body = node("doc", node("paragraph", text(payload, marks=[
        {"type": "link", "attrs": {"href": url}}])))
    data = answer(summary=payload, url=url, comments=[{"author":payload,"body":payload}])
    parsed = Parsed(render(data, descriptions={"QA-1": body}))
    assert not any(tag in ("script", "img", "iframe", "object", "embed") for tag, attrs in parsed.tags)
    assert all(attrs.get("href", "jira.json") == "jira.json" for tag, attrs in parsed.tags if tag == "a")
    assert payload in parsed.words


def test_reading_copy_is_standalone_and_keeps_navigation_and_comments():
    data = answer("Some details.", comments=[{"author":"Reviewer", "created":"2026-09-22T19:01:00+03:00",
                                             "body":"See https://docs.example/spec"}])
    data["issues"].append(dict(data["issues"][0], key="QA-2", summary="Second task"))
    before = deepcopy(data)
    page = render(data)
    parsed = Parsed(page)
    links = [attrs["href"] for tag, attrs in parsed.tags if tag == "a"]
    assert "#issue-1" in links and "#issue-2" in links
    assert "https://docs.example/spec" in links
    assert "jira.json" in links
    assert "Reviewer" in parsed.words
    assert "22 Sep 2026, 19:01 +0300" in parsed.words
    assert "Comments (1)" in parsed.words
    assert not any(tag in ("script", "link", "img") for tag, attrs in parsed.tags)
    assert data == before


def test_plain_bodies_remain_readable_with_lists_and_tables():
    # A numbered section heading may open with a Cyrillic capital, as the
    # sections of a Ukrainian task do. "Goal", escaped to keep the source English.
    goal = "\u041c\u0435\u0442\u0430"
    page = render(answer("1. %s\nTask description.\n\n- first\n- second\n\n"
                         "| Name | Value |\n|---|---|\n| state | active |" % goal))
    assert "<h3>1. %s</h3>" % goal in page
    assert "<p>Task description.</p>" in page
    assert "<li>first</li>" in page
    assert "<th>Name</th>" in page and "<td>active</td>" in page


def test_unknown_document_nodes_keep_their_text():
    body = node("doc", node("futureExtension", node("paragraph", text("Still readable"))))
    assert "Still readable" in render(answer(), descriptions={"QA-1":body})


def test_empty_search_has_a_readable_empty_state():
    page = render({"jql":"project = QA", "issues":[]})
    assert "No issues matched this search." in page


# ----------------------------------------------------------- attached files
# A picture in a description used to render as "view it in the original Jira
# issue", which is the one place a reader of an offline copy cannot go. The
# step downloads them now, so the reading copy shows them where the words put
# them - which is the whole of what makes a screenshot mean anything.
SHOT = "data:image/png;base64,iVBORw0KGgo="


def picture(filename="screenshot.png", src=SHOT):
    return {"filename": filename, "mime": "image/png", "src": src}


def attached(filename="screenshot.png", mime="image/png", size=1024, path="p"):
    return {"filename": filename, "mime": mime, "size": size, "path": path,
            "url": "https://jira.example/secure/attachment/1/" + filename}


def media(**attrs):
    return node("mediaSingle", node("media", **dict({"type": "file"}, **attrs)))


def test_a_picture_is_shown_where_the_description_put_it():
    page = render(answer(node("doc", text("Looks like:"), media()),
                         attachments=[attached()]),
                  pictures={"QA-1": [picture()]})

    images = [one for one in Parsed(page).tags if one[0] == "img"]
    assert len(images) == 1
    assert images[0][1]["src"] == SHOT
    assert "view it in the original Jira issue" not in page


def test_a_shown_picture_is_captioned_with_the_file_it_came_from():
    """ADF keeps an embedded file under an id the attachment API never
    reports, so the two are matched by position. A reader looking at the wrong
    picture has to be able to see that they are."""
    page = render(answer(node("doc", media()), attachments=[attached()]),
                  pictures={"QA-1": [picture()]})
    assert "<figcaption>screenshot.png</figcaption>" in page


def test_alt_text_chooses_the_file_when_the_editor_wrote_one():
    page = render(answer(node("doc", media(alt="second.png")),
                         attachments=[attached(), attached("second.png")]),
                  pictures={"QA-1": [picture(), picture("second.png")]})
    assert "<figcaption>second.png</figcaption>" in page


def test_a_file_the_body_never_showed_is_listed_under_it():
    """Otherwise an issue with a log archive on it reads as one with nothing
    attached: a file nothing refers to appears in the body nowhere at all."""
    page = render(answer(node("doc", text("No pictures here.")),
                         attachments=[attached("trace.zip", "application/zip",
                                               size=2048, path="")]))

    assert "Attachments (1)" in page
    assert "trace.zip" in page


def test_a_picture_already_shown_is_not_listed_again():
    page = render(answer(node("doc", media()), attachments=[attached()]),
                  pictures={"QA-1": [picture()]})
    assert "Attachments (" not in page


def test_two_files_of_one_name_are_both_accounted_for():
    """Counted, not looked up: placing one must not hide the other."""
    page = render(answer(node("doc", media()),
                         attachments=[attached(), attached()]),
                  pictures={"QA-1": [picture(), picture()]})
    assert "Attachments (1)" in page


def test_a_file_that_was_not_downloaded_keeps_the_note_it_always_had():
    page = render(answer(node("doc", media()),
                         attachments=[attached("clip.mp4", "video/mp4")]))
    assert "view it in the original Jira issue" in page


def test_an_issue_with_nothing_attached_gains_no_section():
    page = render(answer(node("doc", text("Plain."))))
    assert "Attachments (" not in page


def test_a_picture_in_a_comment_comes_from_the_same_queue():
    """One pasted into a comment is as much "where does this belong" as one in
    the body, and a second queue would hand the same file out twice."""
    page = render(answer(node("doc", text("See below.")),
                         attachments=[attached()],
                         comments=[{"author": "A", "created": "", "body": ""}]),
                  comments={"QA-1": [node("doc", media())]},
                  pictures={"QA-1": [picture()]})

    assert len([one for one in Parsed(page).tags if one[0] == "img"]) == 1
    assert "Attachments (" not in page


def test_the_policy_admits_embedded_pictures_and_nothing_else():
    """Data URIs only: the page must still load no remote or local file, which
    is what makes it safe to open a document somebody else wrote."""
    page = render(answer(node("doc", media()), attachments=[attached()]),
                  pictures={"QA-1": [picture()]})
    policy = [one[1] for one in Parsed(page).tags
              if one[0] == "meta" and "Content-Security-Policy"
              in str(one[1].get("http-equiv"))][0]["content"]

    assert "img-src data:" in policy
    assert "default-src 'none'" in policy


def test_a_picture_cannot_smuggle_markup_through_its_filename():
    page = render(answer(node("doc", media()),
                         attachments=[attached('<script>x</script>.png')]),
                  pictures={"QA-1": [picture('<script>x</script>.png')]})
    assert "<script>" not in page
