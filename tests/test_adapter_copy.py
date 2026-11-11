"""PlaywrightAdapter.page_light_copy: when the page is asked for its DOM, and
what the adapter itself knows that no DOM shows - a picture or a font fetched
since the last full copy."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

pytest.importorskip("playwright")

from adapters import playwright_adapter as pa  # noqa: E402

BASE = {"doc": "d1", "linked": [["http://shop.example.invalid/app.css", 12]]}


class FakeRequest:
    def __init__(self, url, kind):
        self.url = url
        self.resource_type = kind


class FakeSession:
    def send(self, method, params=None):
        assert method == "Page.captureSnapshot"
        return {"data": "an archive"}


class FakeContext:
    def new_cdp_session(self, page):
        return FakeSession()


class FakePage:
    """Answers the light copy's question as a page would: its DOM when it was
    given the full copy to compare itself with, only what it is like otherwise."""

    def __init__(self):
        self.handlers = {}
        self.context = FakeContext()
        self.asked = []

    def on(self, name, handler):
        self.handlers[name] = handler

    def emit(self, name, request):
        self.handlers[name](request)

    def evaluate(self, script, argument=None):
        if script.lstrip().startswith("([pixels, base])"):
            pixels, base = argument
            self.asked.append((script, base))
            answer = {"doc": "d1", "linked": BASE["linked"], "uses": [], "html": None,
                      "why": "" if base else "no full copy to read it against"}
            if base:
                answer.update(html="<html></html>", url="http://shop.example.invalid/")
            return answer
        return 0


def _adapter():
    page = FakePage()
    return pa.PlaywrightAdapter(None, None, page), page


def test_the_page_is_asked_for_its_dom_against_the_last_full_copy():
    adapter, page = _adapter()
    assert adapter.page_light_copy(None)["html"] is None
    assert adapter.page_light_copy(BASE)["html"] == "<html></html>"
    assert [base for _script, base in page.asked] == [None, BASE]


def test_what_was_typed_is_mirrored_and_put_back_by_the_one_script():
    # Built into the script, not evaluated from a string inside the page: a
    # page whose policy forbids eval must still be copied.
    adapter, page = _adapter()
    adapter.page_light_copy(BASE)
    script = page.asked[0][0]
    assert "__PREPARE__" not in script and "__RESTORE__" not in script
    assert "__qavCopyUndo" in script and "eval(" not in script


@pytest.mark.parametrize("kind", ["image", "font", "stylesheet", "media"])
def test_something_fetched_since_the_full_copy_means_no_light_copy(tmp_path, kind):
    adapter, page = _adapter()
    page.emit("request", FakeRequest("http://shop.example.invalid/new.bin", kind))
    answer = adapter.page_light_copy(BASE)
    # The DOM is not even written: asked without the full copy to compare with.
    assert page.asked[-1][1] is None
    assert answer["html"] is None and answer["why"].startswith("1 new picture(s)")


def test_the_page_s_own_calls_and_inline_pictures_do_not_count():
    adapter, page = _adapter()
    for url, kind in (("http://shop.example.invalid/web/dataset/call_kw", "fetch"),
                      ("http://shop.example.invalid/web/x", "xhr"),
                      ("data:image/png;base64,AAAA", "image"),
                      ("blob:http://shop.example.invalid/1", "image")):
        page.emit("request", FakeRequest(url, kind))
    assert adapter.page_light_copy(BASE)["html"]


def test_a_full_copy_holds_what_was_fetched_before_it_but_not_what_is_on_its_way(tmp_path):
    adapter, page = _adapter()
    done = FakeRequest("http://shop.example.invalid/a.png", "image")
    slow = FakeRequest("http://shop.example.invalid/b.png", "image")
    page.emit("request", done)
    page.emit("requestfinished", done)
    page.emit("request", slow)                       # still loading at the copy
    assert adapter.page_copy(str(tmp_path / "step_000.mhtml"))
    assert adapter.page_light_copy(BASE)["html"] is None
    page.emit("requestfinished", slow)
    # ...and only the next full copy can hold it.
    assert adapter.page_light_copy(BASE)["html"] is None
    assert adapter.page_copy(str(tmp_path / "step_001.mhtml"))
    assert adapter.page_light_copy(BASE)["html"]


# -- what Chrome's archive leaves out ---------------------------------------------
ARCHIVE = ('From: <Saved by Blink>\r\nMIME-Version: 1.0\r\nContent-Type: multipart/related;\r\n'
           '\ttype="text/html";\r\n\tboundary="----MultipartBoundary--one----"\r\n\r\n'
           '------MultipartBoundary--one----\r\nContent-Type: text/html\r\n'
           'Content-Location: http://shop.example.invalid/web\r\n\r\n'
           '<html><head><link rel="stylesheet" href="/app.css"></head><body><i class="fa"></i>'
           '</body></html>\r\n'
           '------MultipartBoundary--one----\r\nContent-Type: text/css\r\n'
           'Content-Location: http://shop.example.invalid/app.css\r\n\r\n'
           '.o_input { border-top-color: ; border-top-style: ; }\r\n'
           '------MultipartBoundary--one------\r\n')
AS_SERVED = b'@font-face{font-family:Icons;src:url(fonts/icons.woff2?v=4) format("woff2")}' \
            b'.o_input{border:1px solid var(--line);border-width:0 0 1px}'


class ServingPage(FakePage):
    """A page whose stylesheet and font can be read back from its cache."""

    def __init__(self):
        super().__init__()
        self.have = []
        self.context = self
        self.archives = 0

    def new_cdp_session(self, page):
        return self

    def send(self, method, params=None):
        self.archives += 1
        return {"data": ARCHIVE.replace("--one--", "--%d--" % self.archives)}

    def evaluate(self, script, argument=None):
        import base64

        if script.lstrip().startswith("async (have)"):
            self.have.append(list(argument))
            everything = [["http://shop.example.invalid/app.css", "text/css",
                           base64.b64encode(AS_SERVED).decode("ascii")],
                          ["http://shop.example.invalid/fonts/icons.woff2?v=4", "font/woff2",
                           base64.b64encode(b"wOF2" + bytes(range(256)) * 2).decode("ascii")]]
            return {"use": [one[0] for one in everything],
                    "add": [one for one in everything if one[0] not in argument]}
        return super().evaluate(script, argument)


def test_an_archive_carries_the_page_s_fonts_and_its_stylesheets_as_served(tmp_path):
    from engine import history

    page = ServingPage()
    adapter = pa.PlaywrightAdapter(None, None, page)
    assert adapter.page_copy(str(tmp_path / "step_000.mhtml"))
    raw = (tmp_path / "step_000.mhtml").read_bytes()
    parts = history._split(raw)
    assert [(part["type"], part["served"]) for part in parts] == [
        ("text/html", False), ("text/css", False), ("text/css", True), ("font/woff2", True)]
    assert history._body(parts[2]) == AS_SERVED
    # Shown, the page has the rule Chrome wrote out empty, and its icons' font.
    store = {}
    html, names = history.convert(raw, store)
    kinds = sorted(store[name][0] for name in names)
    assert kinds == ["font/woff2", "text/css"]
    css = [body for kind, body in store.values() if kind == "text/css"][0].decode("utf-8")
    assert "border:1px solid var(--line)" in css and "border-top-color: ;" not in css
    assert "src:url(qavres:" in css


def test_what_was_read_from_the_page_once_is_not_read_again(tmp_path):
    from engine import history

    page = ServingPage()
    adapter = pa.PlaywrightAdapter(None, None, page)
    adapter.page_copy(str(tmp_path / "step_000.mhtml"))
    adapter.page_copy(str(tmp_path / "step_001.mhtml"))
    assert page.have[0] == [] and len(page.have[1]) == 2
    # ...and the second archive, with a boundary of its own, holds them too.
    parts = history._split((tmp_path / "step_001.mhtml").read_bytes())
    assert [part["served"] for part in parts] == [False, False, True, True]


def test_an_archive_stays_chrome_s_when_the_page_cannot_be_read_from(tmp_path):
    page = ServingPage()
    adapter = pa.PlaywrightAdapter(None, None, page)
    serving = page.evaluate

    def broken(script, argument=None):
        if script.lstrip().startswith("async (have)"):
            raise RuntimeError("Execution context was destroyed")
        return serving(script, argument)

    page.evaluate = broken
    assert adapter.page_copy(str(tmp_path / "step_000.mhtml"))
    assert (tmp_path / "step_000.mhtml").read_bytes() == ARCHIVE.replace(
        "--one--", "--1--").encode("utf-8")


def test_where_the_page_was_scrolled_to_is_written_on_it_and_taken_off_again():
    # In the script that mirrors what was typed, so both kinds of copy have it.
    assert "data-qav-scroll" in pa._COPY_PREPARE_JS
    assert "keep(el, 'data-qav-scroll')" in pa._COPY_PREPARE_JS      # put back after


def test_a_page_that_cannot_be_asked_gets_a_full_copy():
    adapter, page = _adapter()

    def gone(script, argument=None):
        raise RuntimeError("Execution context was destroyed")

    page.evaluate = gone
    assert adapter.page_light_copy(BASE) is None
