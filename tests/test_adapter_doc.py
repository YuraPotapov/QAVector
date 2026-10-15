"""PlaywrightAdapter.doc_capture: the guide's picture and where its elements are."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

pytest.importorskip("playwright")

from adapters import playwright_adapter as pa  # noqa: E402

STATE = {"key": "https://shop.example.invalid/odoo/orders/*", "title": "New order",
         "dialog": "", "width": 1000, "height": 500}


class FakeHandle:
    """An element the page handed out; it stays the one element it was."""

    def __init__(self, page, selector):
        self.page, self.selector = page, selector
        self.element = page.elements.get(selector)
        self.disposed = False

    def evaluate(self, script):
        self.page.log.append(("measure", self.selector))
        return self.element

    def dispose(self):
        self.disposed = True


class FakeLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    @property
    def first(self):
        return self

    def wait_for(self, state=None, timeout=None):
        self.page.log.append(("wait_for", self.selector, timeout))
        if self.selector not in self.page.elements:
            raise TimeoutError("not there")

    def scroll_into_view_if_needed(self, timeout=None):
        self.page.log.append(("scroll", self.selector))

    def element_handle(self, timeout=None):
        if self.selector not in self.page.elements:
            raise TimeoutError("not there")
        return FakeHandle(self.page, self.selector)


class FakePage:
    def __init__(self, elements=None, style_ok=True):
        self.elements = elements or {}
        self.style_ok = style_ok
        self.log = []
        self.focused = None
        self.redrawn = set()          # selectors whose element was replaced

    def on(self, name, handler):
        pass

    def locator(self, selector):
        return FakeLocator(self, selector)

    def query_selector(self, selector):
        # Never waits: whatever matches now, or nothing.
        self.log.append(("query", selector))
        if selector not in self.elements:
            return None
        self.redrawn.discard(selector)
        return FakeHandle(self, selector)

    def evaluate(self, script, arg=None):
        if isinstance(arg, list):
            self.log.append(("measure_all", [h.selector if h else None for h in arg]))
            return [None if h is None or h.selector in self.redrawn
                    or h.selector not in self.elements else self.elements[h.selector]
                    for h in arg]
        if "activeElement" in script:
            return self.focused
        self.log.append(("state",))
        return dict(STATE)

    def screenshot(self, path=None, style=None, **kwargs):
        if style is not None and not self.style_ok:
            raise TypeError("screenshot() got an unexpected keyword argument 'style'")
        self.log.append(("shot", os.path.basename(path), style))


def _capture(page, targets, focus=False, adapter=None):
    adapter = adapter or pa.PlaywrightAdapter(None, None, page)
    return adapter.doc_capture("/tmp/x/step_001.png", targets, focus=focus)


def test_boxes_come_back_as_fractions_of_the_picture():
    page = FakePage({"#save": {"label": "Save", "secret": False,
                               "box": [800, 50, 100, 25]}})
    answer = _capture(page, ["#save"])
    assert answer["page"] == {"key": STATE["key"], "title": "New order", "dialog": "",
                              "size": [1000.0, 500.0]}
    assert answer["elements"] == [{"box": [0.8, 0.1, 0.1, 0.05], "label": "Save",
                                   "secret": False, "covered": False, "choices": []}]


def test_an_element_something_is_drawn_over_says_so():
    page = FakePage({"#run": {"label": "Run", "covered": True, "box": [10, 60, 70, 15]}})
    assert _capture(page, ["#run"])["elements"][0]["covered"] is True


def test_everything_is_measured_before_the_picture_and_the_hud_is_left_out():
    page = FakePage({"#save": {"label": "Save", "box": [0, 0, 10, 10]}})
    _capture(page, ["#save", "#customer"])
    kinds = [entry[0] for entry in page.log]
    assert kinds[-1] == "shot" and kinds.index("state") < kinds.index("shot")
    assert page.log[-1][2] == pa.DOC_HIDE


def test_the_step_s_own_element_is_waited_for_and_earlier_ones_only_looked_for():
    page = FakePage({"#save": {"box": [0, 0, 10, 10]},
                     "#customer": {"box": [0, 0, 10, 10]}})
    answer = _capture(page, ["#save", "#customer", "#gone"])
    assert ("wait_for", "#save", pa.DOC_TARGET_MS) in page.log
    assert ("scroll", "#save") in page.log
    assert ("query", "#customer") in page.log and ("query", "#gone") in page.log
    assert not [entry for entry in page.log
                if entry[0] in ("wait_for", "scroll") and entry[1] != "#save"]
    assert answer["elements"][2] is None


def test_an_element_out_of_sight_is_not_on_the_picture():
    page = FakePage({"#below": {"box": [10, 600, 50, 20]},
                     "#hidden": {"box": [10, 10, 0, 0]},
                     "#half": {"box": [-20, 490, 40, 20]}})
    answer = _capture(page, ["#below", "#hidden", "#half"])
    assert answer["elements"][:2] == [None, None]
    assert answer["elements"][2]["box"] == [0.0, 0.98, 0.02, 0.02]   # clipped to it


def test_a_press_points_at_what_has_focus():
    page = FakePage()
    page.focused = {"label": "Reason", "box": [100, 100, 100, 50]}
    answer = _capture(page, [None], focus=True)
    assert answer["elements"][0]["label"] == "Reason"
    assert _capture(page, [None], focus=False)["elements"] == [None]


def test_an_older_playwright_still_takes_the_picture():
    page = FakePage({"#a": {"box": [0, 0, 10, 10]}}, style_ok=False)
    answer = _capture(page, ["#a"])
    assert page.log[-1] == ("shot", "step_001.png", None)
    assert answer["elements"][0] is not None


def test_earlier_elements_are_looked_up_once_and_measured_together():
    # One call per earlier step on every picture cost a busy page seconds.
    page = FakePage({"#a": {"box": [0, 0, 10, 10]}, "#b": {"box": [0, 20, 10, 10]},
                     "#c": {"box": [0, 40, 10, 10]}})
    adapter = pa.PlaywrightAdapter(None, None, page)
    _capture(page, ["#c", "#a", "#b"], adapter=adapter)
    page.log.clear()
    answer = _capture(page, ["#c", "#a", "#b"], adapter=adapter)
    assert [entry for entry in page.log if entry[0] == "query"] == []
    assert ("measure_all", ["#a", "#b"]) in page.log
    assert [element["box"] for element in answer["elements"]] == [
        [0.0, 0.08, 0.01, 0.02], [0.0, 0.0, 0.01, 0.02], [0.0, 0.04, 0.01, 0.02]]


def test_an_element_the_page_drew_anew_is_found_again():
    page = FakePage({"#a": {"box": [0, 0, 10, 10]}, "#b": {"box": [0, 20, 10, 10]}})
    adapter = pa.PlaywrightAdapter(None, None, page)
    _capture(page, ["#b", "#a"], adapter=adapter)
    page.redrawn.add("#a")
    page.log.clear()
    answer = _capture(page, ["#b", "#a"], adapter=adapter)
    assert ("query", "#a") in page.log
    assert answer["elements"][1]["box"] == [0.0, 0.0, 0.01, 0.02]


def test_elements_of_a_page_left_behind_are_let_go():
    page = FakePage({"#a": {"box": [0, 0, 10, 10]}, "#b": {"box": [0, 20, 10, 10]},
                     "#c": {"box": [0, 40, 10, 10]}})
    adapter = pa.PlaywrightAdapter(None, None, page)
    _capture(page, ["#c", "#a"], adapter=adapter)
    kept = adapter._doc_handles["#a"]
    _capture(page, ["#b"], adapter=adapter)         # a new page: nothing earlier
    assert kept.disposed and "#a" not in adapter._doc_handles


@pytest.mark.parametrize("name", ["_DOC_PAGE_JS", "_DOC_ELEMENT_JS"])
def test_the_scripts_sent_to_the_page_parse(name, tmp_path):
    # A fake page runs no script, so a syntax error in one only shows in a real
    # browser - as a guide with no pictures, since every capture is best-effort.
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("no node to parse the script with")
    script = tmp_path / "check.js"
    script.write_text("const probe = (%s);\n" % getattr(pa, name), encoding="utf-8")
    checked = subprocess.run([node, "--check", str(script)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
