"""engine.history: the page copies a paused scenario is stepped back over."""

import base64
import os

from engine import history

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode("ascii")

# Shaped like Chrome's Page.captureSnapshot: the page quoted-printable, its
# stylesheet and pictures as parts of their own, an inline <style> moved to a
# cid: part, a frame as a page of its own.
MHTML = """From: <Saved by Blink>
Snapshot-Content-Location: http://shop.example.invalid/web/form
Subject: Form
MIME-Version: 1.0
Content-Type: multipart/related;
\ttype="text/html";
\tboundary="----MultipartBoundary--abc----"

------MultipartBoundary--abc----
Content-Type: text/html
Content-ID: <frame-main@mhtml.blink>
Content-Transfer-Encoding: quoted-printable
Content-Location: http://shop.example.invalid/web/form

<html><head><link rel=3D"stylesheet" href=3D"cid:css-1@mhtml.blink">=
<link rel=3D"stylesheet" href=3D"http://shop.example.invalid/web/css/app.css">=
<script>alert(1)</script></head><body>=
<img src=3D"http://shop.example.invalid/web/img/logo.png">=
<input id=3D"name" value=3D"typed by the scenario">=
<canvas width=3D"120" height=3D"40" data-qav-pixels=3D"data:image/png;base64,{png}"></canvas>=
<my-widget><template shadowmode=3D"open"><b>shadow</b></template></my-widget>=
<iframe src=3D"cid:frame-2@mhtml.blink"></iframe>=
<div style=3D"background: url(&quot;img/bg.png&quot;)">x</div>=
</body></html>
------MultipartBoundary--abc----
Content-Type: text/css
Content-Transfer-Encoding: quoted-printable
Content-Location: cid:css-1@mhtml.blink

h1 {{ color: red; }}
------MultipartBoundary--abc----
Content-Type: text/css
Content-Transfer-Encoding: quoted-printable
Content-Location: http://shop.example.invalid/web/css/app.css

body {{ background: url("../img/bg.png") repeat-x; }}
------MultipartBoundary--abc----
Content-Type: image/png
Content-Transfer-Encoding: base64
Content-Location: http://shop.example.invalid/web/img/bg.png

{png}
------MultipartBoundary--abc----
Content-Type: image/png
Content-Transfer-Encoding: base64
Content-Location: http://shop.example.invalid/web/img/logo.png

{png}
------MultipartBoundary--abc----
Content-Type: text/html
Content-ID: <frame-2@mhtml.blink>
Content-Transfer-Encoding: quoted-printable

<html><body><img src=3D"http://shop.example.invalid/web/img/logo.png"></body></html>
------MultipartBoundary--abc------
""".format(png=PNG).replace("\n", "\r\n").encode("ascii")


def _html():
    return history.to_html(MHTML)


def test_the_copy_keeps_what_was_typed_and_needs_nothing_from_the_server():
    html = _html()
    assert 'value="typed by the scenario"' in html
    # Not one address of the live site is left to fetch: the copy is the page
    # as it was, not the page as the server serves it now.
    assert "shop.example.invalid" not in html
    assert "cid:" not in html


def test_a_stylesheet_s_pictures_are_read_against_the_stylesheet():
    # "../img/bg.png" inside web/css/app.css is web/img/bg.png. Read against the
    # page's address it would be img/bg.png, which the archive does not hold.
    css = base64.b64decode(_html().split('href="data:text/css;base64,')[2].split('"')[0])
    assert b"url(data:image/png;base64," in css


def test_a_picture_named_in_a_style_attribute_is_built_in():
    # Chrome writes the attribute's inner quotes as &quot;.
    assert '<div style="background: url(data:image/png;base64,%s)">' % PNG in _html()


def test_scripts_are_dropped_and_a_canvas_becomes_its_picture():
    html = _html()
    assert "<script" not in html and "alert(1)" not in html
    assert "<canvas" not in html
    assert '<img src="data:image/png;base64,%s" width="120" height="40">' % PNG in html


def test_a_shadow_root_is_written_the_standard_way():
    assert '<template shadowrootmode="open">' in _html()


def test_a_frame_is_built_in_with_its_own_pictures():
    html = _html()
    frame = html.split('<iframe src="data:text/html;base64,')[1].split('"')[0]
    assert b"data:image/png;base64," in base64.b64decode(frame)


def test_an_archive_without_a_page_is_refused():
    try:
        history.to_html(b"Content-Type: text/plain\r\n\r\nnothing")
    except ValueError:
        return
    raise AssertionError("expected a ValueError")


# -- the folders -------------------------------------------------------------------
class CopyingAdapter:
    def __init__(self, works=True):
        self.works = works
        self.copied = []

    def page_copy(self, path):
        self.copied.append(os.path.basename(path))
        if self.works:
            with open(path, "wb") as handle:
                handle.write(MHTML)
        return self.works


def test_copies_are_kept_by_position_and_shown_as_html(tmp_path):
    run = history.RunHistory(base=str(tmp_path))
    copies = run.scenario("dev-agent", "smoke")
    adapter = CopyingAdapter()
    assert copies.capture(adapter, 0) and copies.capture(adapter, 1)
    assert adapter.copied == ["step_000.mhtml", "step_001.mhtml"]
    view = copies.view(1)
    assert view.endswith("step_001.html")
    with open(view, encoding="utf-8") as handle:
        assert "typed by the scenario" in handle.read()
    assert copies.view(1) == view            # made once
    assert copies.view(2) is None            # never taken
    run.close()


def test_a_page_that_cannot_be_copied_leaves_no_position(tmp_path):
    copies = history.RunHistory(base=str(tmp_path)).scenario("s", "x")
    assert not copies.capture(CopyingAdapter(works=False), 0)
    assert not copies.has(0) and copies.view(0) is None


def test_steps_taken_again_forget_what_they_left_the_first_time(tmp_path):
    copies = history.RunHistory(base=str(tmp_path)).scenario("s", "x")
    adapter = CopyingAdapter()
    for position in range(4):
        copies.capture(adapter, position)
    copies.view(3)
    copies.forget_from(2)
    assert [copies.has(p) for p in range(4)] == [True, True, False, False]
    assert sorted(os.listdir(copies.dir)) == ["step_000.mhtml", "step_001.mhtml"]


def test_a_scenario_run_twice_in_a_window_gets_a_second_folder(tmp_path):
    run = history.RunHistory(base=str(tmp_path))
    first, second = run.scenario("dev agent", "smoke"), run.scenario("dev agent", "smoke")
    assert first.dir != second.dir
    assert os.path.basename(os.path.dirname(first.dir)) == "dev_agent"


def test_a_finished_scenario_and_a_finished_run_leave_nothing(tmp_path):
    run = history.RunHistory(base=str(tmp_path))
    copies = run.scenario("s", "x")
    copies.capture(CopyingAdapter(), 0)
    copies.close()
    assert not os.path.exists(copies.dir)
    run.close()
    assert not os.path.exists(run.dir)
    run.close()                              # twice is harmless (atexit, finally)


def test_the_next_run_deletes_what_a_killed_run_left(tmp_path, monkeypatch):
    folder = history.root(str(tmp_path))
    for name in ("4242-1", "%d-2" % os.getpid(), "4343-3", "not-a-run"):
        os.makedirs(os.path.join(folder, name))
    monkeypatch.setattr(history, "_pid_alive", lambda pid: pid == 4343)
    assert history.sweep(str(tmp_path)) == 1
    assert sorted(os.listdir(folder)) == sorted(["%d-2" % os.getpid(), "4343-3", "not-a-run"])


def test_this_process_reads_as_alive():
    assert history._pid_alive(os.getpid())
