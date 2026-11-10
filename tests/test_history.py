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
<div id=3D"__exec_hud_host__" aria-hidden=3D"true"><template shadowmode=3D"open">=
<div class=3D"wrap">the HUD</div></template></div>=
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
{many}
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
""".format(png=PNG, many="".join(".r{0} {{ background: url(../img/bg.png); }}\n".format(i)
                                   for i in range(1000))).replace("\n", "\r\n").encode("ascii")


def _converted():
    store = {}
    html, names = history.convert(MHTML, store)
    return html, names, store


def _body(store, html, ref_in):
    """The resource ``ref_in`` names, as text."""
    name = history.REF_PATTERN.search(ref_in).group(1)
    return store[name][1].decode("utf-8")


def test_the_copy_keeps_what_was_typed_and_names_nothing_on_the_server():
    html, _names, store = _converted()
    assert 'value="typed by the scenario"' in html
    # Not one address of the live site is left to fetch: the copy is the page
    # as it was, not the page as the server serves it now.
    texts = [html] + [body.decode("utf-8") for kind, body in store.values()
                      if kind in history.TEXT_TYPES]
    assert not any("shop.example.invalid" in text or "cid:" in text for text in texts)


def test_a_resource_named_a_thousand_times_is_kept_once():
    # Built into every place that named it, one Odoo page grew from 4 MB to 43.
    html, names, store = _converted()
    stylesheet = [body for kind, body in store.values() if kind == "text/css"
                  and b".r999" in body][0]
    # A thousand short names of one picture, not a thousand copies of it.
    assert b"data:" not in stylesheet and stylesheet.count(history.REF.encode()) == 1001
    pictures = [name for name in names if store[name][0] == "image/png"]
    # logo.png and bg.png hold the same bytes: one resource, not two.
    assert len(pictures) == 1


def test_whatever_a_resource_names_comes_before_it():
    html, names, store = _converted()
    for position, name in enumerate(names):
        kind, body = store[name]
        if kind in history.TEXT_TYPES:
            for named in history.REF_PATTERN.findall(body.decode("utf-8")):
                assert names.index(named) < position


def test_a_stylesheet_s_pictures_are_read_against_the_stylesheet():
    # "../img/bg.png" inside web/css/app.css is web/img/bg.png. Read against the
    # page's address it would be img/bg.png, which the archive does not hold.
    html, _names, store = _converted()
    css = _body(store, html, html.split('href="')[2])
    assert css.startswith("body { background: url(qavres:")


def test_a_picture_named_in_a_style_attribute_is_a_resource_too():
    # Chrome writes the attribute's inner quotes as &quot;.
    html, _names, _store = _converted()
    assert '<div style="background: url(qavres:' in html


def test_scripts_are_dropped_and_a_canvas_becomes_its_picture():
    html, _names, _store = _converted()
    assert "<script" not in html and "alert(1)" not in html
    assert "<canvas" not in html
    assert '<img src="data:image/png;base64,%s" width="120" height="40">' % PNG in html


def test_a_shadow_root_is_written_the_standard_way():
    assert '<template shadowrootmode="open">' in _converted()[0]


def test_the_hud_is_left_out_of_the_copy():
    html = _converted()[0]
    assert "__exec_hud_host__" not in html and "the HUD" not in html


def test_a_frame_is_a_resource_naming_its_own_pictures():
    html, _names, store = _converted()
    frame = _body(store, html, html.split('<iframe src="')[1])
    assert history.REF in frame and "<img" in frame


def test_an_archive_without_a_page_is_refused():
    try:
        history.convert(b"Content-Type: text/plain\r\n\r\nnothing")
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
        page = handle.read()
    assert "typed by the scenario" in page and "qavres:" not in page
    # The GUI reads it from disk: what it names is beside it, in res/, and a
    # stylesheet's own pictures are named from there.
    named = [part.split('"')[0] for part in page.split('"res/')[1:]]
    assert named and all(os.path.exists(os.path.join(copies.dir, "res", one)) for one in named)
    css = [one for one in named if one.endswith(".css") and
           "url(" in open(os.path.join(copies.dir, "res", one)).read()]
    assert css and "qavres:" not in open(os.path.join(copies.dir, "res", css[0])).read()
    assert copies.view(1) == view            # made once
    assert copies.view(2) is None            # never taken
    html, resources = history.lookup(view)
    assert "qavres:" in html and [kind for _n, kind, _b in resources]
    run.close()
    assert history.lookup(view) is None


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
    # The resources stay: the next copies will name most of them again.
    assert sorted(os.listdir(copies.dir)) == ["res", "step_000.mhtml", "step_001.mhtml"]


def test_a_step_that_leaves_the_page_as_it_was_shares_the_copy_before_it(tmp_path):
    copies = history.RunHistory(base=str(tmp_path)).scenario("s", "x")
    adapter = CopyingAdapter()
    copies.capture(adapter, 0)
    assert copies.same_as(1, 0) and adapter.copied == ["step_000.mhtml"]
    assert copies.view(1).endswith("step_001.html")
    assert not copies.same_as(3, 2)          # nothing to share


def test_steps_are_made_ready_in_the_background(tmp_path):
    copies = history.RunHistory(base=str(tmp_path)).scenario("s", "x")
    adapter = CopyingAdapter()
    for position in range(3):
        copies.capture(adapter, position)
    copies.prepare([2, 1, 0])
    copies._worker.shutdown(wait=True)
    assert sorted(copies._pages) == [0, 1, 2]
    assert os.path.exists(copies.path(1, "html"))


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
