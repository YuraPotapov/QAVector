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
    # One page for both: going from one to the other is the copy already on
    # show, with nothing to make, send or draw - and nothing written twice.
    assert copies.same_as(2, 1) and copies.view(2) == copies.view(1) == copies.view(0)
    assert copies.view(0).endswith("step_000.html")
    assert not [name for name in os.listdir(copies.dir) if "step_001" in name]
    assert not copies.same_as(4, 3)          # nothing to share


def test_forgetting_a_step_that_shared_a_copy_keeps_the_copy(tmp_path):
    copies = history.RunHistory(base=str(tmp_path)).scenario("s", "x")
    copies.capture(CopyingAdapter(), 0)
    copies.same_as(1, 0)
    copies.forget_from(1)
    assert copies.has(0) and not copies.has(1)
    assert copies.view(1) is None and copies.view(0).endswith("step_000.html")
    # Taken for itself the second time, it is a page of its own.
    copies.capture(CopyingAdapter(), 1)
    assert copies.view(1).endswith("step_001.html")


def test_a_page_made_from_a_copy_since_taken_again_is_not_kept(tmp_path, monkeypatch):
    # The worker is still making step 1's page when the scenario goes on from
    # an earlier step and step 1 is run - and copied - again.
    import threading

    copies = history.RunHistory(base=str(tmp_path)).scenario("s", "x")
    adapter = CopyingAdapter()
    copies.capture(adapter, 0)
    copies.capture(adapter, 1)
    reading, go_on = threading.Event(), threading.Event()
    convert = history.convert

    def slow(raw, store=None, known=None):
        made = convert(raw, store, known)
        reading.set()
        go_on.wait(5)
        return made

    monkeypatch.setattr(history, "convert", slow)
    copies.prepare([1])
    assert reading.wait(5)
    monkeypatch.setattr(history, "convert", convert)
    copies.forget_from(1)
    with open(copies.path(1), "wb") as handle:
        handle.write(MHTML.replace(b"typed by the scenario", b"typed the second time"))
    with copies._lock:
        copies._taken += 1
        copies._stamps[1] = copies._taken
        copies._positions.add(1)
    go_on.set()
    copies._worker.shutdown(wait=True)
    with open(copies.view(1), encoding="utf-8") as handle:
        assert "typed the second time" in handle.read()


# -- light copies ------------------------------------------------------------------
SITE = "http://shop.example.invalid"
LIGHT = ('<!DOCTYPE html><html lang="en"><head>'
         '<meta http-equiv="Content-Security-Policy" content="default-src \'self\'">'
         '<link rel="stylesheet" href="/web/css/app.css">'
         '<style id="own" data-qav-style="0">h1 { color: red; }</style></head><body>'
         '<img src="/web/img/logo.png" srcset="/web/img/logo@2x.png 2x">'
         '<input id="name" value="%s"><noscript><p>turn scripts on</p></noscript>'
         '<script>alert(1)</script>'
         '<my-widget><template shadowrootmode="open"><style>b { color: red }</style>'
         '<b>shadow</b></template></my-widget>'
         '<div id="__exec_hud_host__" aria-hidden="true"></div></body></html>')


def _light(typed="typed later", **more):
    return dict({"html": LIGHT % typed, "url": SITE + "/web/form",
                 "dynamic": {"0": "h1 { color: blue; }\n.new { background: url(\"img/bg.png\"); }"},
                 "adopted": ["form { outline: 4px solid green; }"]}, **more)


def test_a_light_copy_is_read_against_the_full_copy_s_parts():
    store, known = {}, {}
    full_html, full_names = history.convert(MHTML, store, known)
    html, names = history.convert_light(
        _light(), history._by_address(history._parts(MHTML)), store, known)
    assert 'value="typed later"' in html
    # The same stylesheet and picture as the full copy named, by the same names.
    assert set(names) <= set(full_names) and len(names) >= 2
    assert "shop.example.invalid" not in html and "/web/css/app.css" not in html
    # What a DOM holds and a copy must not.
    for gone in ("srcset", "noscript", "turn scripts on", "alert(1)", "Content-Security-Policy",
                 "__exec_hud_host__", "data-qav-style"):
        assert gone not in html
    # The rules script put into the page's own style, not the text it began with -
    # and a picture they name is read against the page.
    assert "color: blue" in html and "color: red; }</style></head>" not in html
    assert ".new { background: url(qavres:" in html
    # The adopted stylesheet comes after everything else; shadow styles stay.
    assert html.index("outline: 4px solid green") > html.index("<my-widget>")
    assert "<style>b { color: red }</style><b>shadow</b>" in html


def test_a_page_s_own_style_names_its_pictures_from_the_page():
    # Chrome files a <style> under cid: with its url() as the page wrote it.
    # Read against the cid: address, the picture was never found.
    archive = MHTML.replace(b"h1 { color: red; }",
                            b'h1 { background: url("/web/img/bg.png"); }')
    assert archive != MHTML
    store = {}
    html, _names = history.convert(archive, store)
    own = _body(store, html, html.split('href="')[1])
    assert own.startswith("h1 { background: url(qavres:")
    # ...and recognised in the next copy, it is still read from the page.
    known = {}
    assert history.convert(archive, store, known) == history.convert(archive, store, known)


class LightAdapter(CopyingAdapter):
    """Takes light copies as a browser does: the page says what it is like,
    and writes its DOM when a full copy of the same document was taken."""

    def __init__(self):
        super().__init__()
        self.asked = []
        self.typed = "typed later"
        self.say = {}

    def page_light_copy(self, base=None):
        self.asked.append(base)
        facts = {"doc": "d1", "linked": [[SITE + "/web/css/app.css", 1001]],
                 "uses": [SITE + "/web/img/logo.png"], "html": None, "why": ""}
        facts.update(self.say)
        if not base:
            facts["why"] = facts["why"] or "no full copy to read it against"
        elif base["doc"] != facts["doc"]:
            facts["why"] = "a new document"
        if not facts["why"]:
            facts.update(_light(self.typed))
        return facts


def _light_history(tmp_path):
    copies = history.RunHistory(base=str(tmp_path)).scenario("s", "x")
    adapter = LightAdapter()
    return copies, adapter


def test_the_first_copy_is_full_and_the_ones_after_it_are_light(tmp_path):
    copies, adapter = _light_history(tmp_path)
    for position in range(3):
        assert copies.capture(adapter, position)
    assert adapter.copied == ["step_000.mhtml"]             # one archive, two DOMs
    assert copies.kinds == {"full": 1, "light": 2}
    assert sorted(name for name in os.listdir(copies.dir) if name.startswith("step")) == [
        "step_000.mhtml", "step_001.json", "step_002.json"]
    # The page asked what it was like when the archive was taken is what the
    # next copies are checked against.
    assert adapter.asked[0] is None and adapter.asked[1]["doc"] == "d1"
    with open(copies.view(2), encoding="utf-8") as handle:
        page = handle.read()
    assert 'value="typed later"' in page and "qavres:" not in page
    named = [part.split('"')[0] for part in page.split('"res/')[1:]]
    assert named and all(os.path.exists(os.path.join(copies.dir, "res", one)) for one in named)
    html, resources = history.lookup(copies.view(2))
    assert "qavres:" in html and resources


def test_a_light_copy_showing_a_picture_the_full_copy_lacks_is_a_full_copy(tmp_path):
    copies, adapter = _light_history(tmp_path)
    copies.capture(adapter, 0)
    adapter.say = {"uses": [SITE + "/web/img/logo.png#top", SITE + "/web/img/new.png"]}
    copies.capture(adapter, 1)
    assert adapter.copied == ["step_000.mhtml", "step_001.mhtml"]
    assert copies.reasons == {"no full copy to read it against": 1,     # the first
                              "a picture the full copy does not hold": 1}
    adapter.say = {"uses": [SITE + "/web/img/logo.png#top"]}      # part of one it holds
    copies.capture(adapter, 2)
    assert copies.kinds == {"full": 2, "light": 1}


def test_the_page_saying_why_not_is_a_full_copy_and_the_next_are_read_against_it(tmp_path):
    copies, adapter = _light_history(tmp_path)
    copies.capture(adapter, 0)
    adapter.say = {"why": "3 new picture(s), font(s) or stylesheet(s)"}
    copies.capture(adapter, 1)
    adapter.say = {"doc": "d2"}                                   # the page was left
    copies.capture(adapter, 2)
    adapter.say = {"doc": "d2"}
    copies.capture(adapter, 3)
    assert adapter.copied == ["step_000.mhtml", "step_001.mhtml", "step_002.mhtml"]
    assert copies._lights[3][0] == 2
    assert copies.reasons == {"no full copy to read it against": 1,
                              "new picture(s), font(s) or stylesheet(s)": 1,
                              "a new document": 1}


def test_going_back_past_the_full_copy_takes_another(tmp_path):
    copies, adapter = _light_history(tmp_path)
    for position in range(4):
        copies.capture(adapter, position)
    copies.forget_from(2)                    # the full copy at 0 stays
    copies.capture(adapter, 2)
    assert adapter.copied == ["step_000.mhtml"]
    copies.forget_from(0)                    # ...and here it goes
    assert not [name for name in os.listdir(copies.dir) if name.startswith("step")]
    copies.capture(adapter, 0)
    copies.capture(adapter, 1)
    assert adapter.copied == ["step_000.mhtml", "step_000.mhtml"]
    assert copies.view(1).endswith("step_001.html")


def test_a_step_that_changed_nothing_shares_a_light_copy_too(tmp_path):
    copies, adapter = _light_history(tmp_path)
    copies.capture(adapter, 0)
    copies.capture(adapter, 1)
    assert copies.same_as(2, 1) and copies.view(2) == copies.view(1)
    assert copies.view(1).endswith("step_001.html")


def test_an_adapter_that_takes_no_light_copies_gets_full_ones(tmp_path):
    copies = history.RunHistory(base=str(tmp_path)).scenario("s", "x")
    adapter = CopyingAdapter()
    copies.capture(adapter, 0)
    copies.capture(adapter, 1)
    assert adapter.copied == ["step_000.mhtml", "step_001.mhtml"]
    assert copies.kinds == {"full": 2}


# -- reading an archive ---------------------------------------------------------
def test_chrome_s_archive_is_read_as_the_standard_parser_reads_it():
    direct = history._split(MHTML)
    by_email = history._parts_by_email(MHTML)
    assert len(direct) == len(by_email) == 6
    for ours, theirs in zip(direct, by_email):
        assert history._body(ours) == theirs["body"]
        assert [ours[key] for key in ("location", "cid", "type", "charset")] == [
            theirs[key] for key in ("location", "cid", "type", "charset")]


def test_an_archive_not_shaped_like_chrome_s_is_still_read():
    # Bare line ends, as no browser writes them: the standard parser's job.
    html, names = history.convert(MHTML.replace(b"\r\n", b"\n"))
    assert "typed by the scenario" in html and names


def test_a_boundary_s_text_inside_a_part_does_not_end_it():
    page = (b'Content-Type: multipart/related; boundary="b"\r\n\r\n'
            b"--b\r\nContent-Type: text/html\r\nContent-Location: http://x.invalid/\r\n\r\n"
            b"<p style=\"color: var(--b)\">--b in a line</p>\r\n"
            b"--bold starts a line\r\n--b--not the end\r\n<i>still the page</i>\r\n--b--\r\n")
    html = history.convert(page)[0]
    assert "--b in a line</p>" in html and "--bold starts a line" in html
    assert "--b--not the end" in html and html.rstrip().endswith("<i>still the page</i>")
    assert len(history._split(page)) == 1


def test_a_part_seen_before_is_recognised_and_gives_the_same_page():
    store, known = {}, {}
    first = history.convert(MHTML, store, known)
    read = []
    body = history._body

    def counting(part):
        if "body" not in part:
            read.append(part["type"])
        return body(part)

    history._body = counting
    try:
        again = history.convert(MHTML, store, known)
    finally:
        history._body = body
    assert again == first == history.convert(MHTML, {})
    # Only the page itself was decoded: the stylesheets, the pictures and the
    # frame are the ones the first copy held.
    assert read == ["text/html"]


def test_a_stylesheet_whose_picture_changed_is_read_again():
    store, known = {}, {}
    history.convert(MHTML, store, known)
    other = base64.b64encode(b"\x89PNG\r\n\x1a\nother").decode("ascii")
    # bg.png holds something else now; app.css, byte for byte the same, does not
    # name the same picture any more.
    changed = MHTML.replace(("bg.png\r\n\r\n" + PNG).encode("ascii"),
                            ("bg.png\r\n\r\n" + other).encode("ascii"))
    assert changed != MHTML
    assert history.convert(changed, store, known) == history.convert(changed, {})


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
