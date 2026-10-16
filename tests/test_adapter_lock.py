"""PlaywrightAdapter.lock_input: the person is locked out, the adapter is not.

Chrome's Input.setIgnoreInputEvents drops the mouse and key events Playwright
dispatches over CDP as well as the person's - silently - so the adapter lifts it
for each click and key press. These pin that order down against fakes.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

pytest.importorskip("playwright")

from adapters import playwright_adapter as pa  # noqa: E402

LOCK = ("Input.setIgnoreInputEvents", {"ignore": True})
UNLOCK = ("Input.setIgnoreInputEvents", {"ignore": False})


class FakeCDP:
    def __init__(self, log, fail=False):
        self.log = log
        self.fail = fail

    def send(self, method, params=None):
        if self.fail:
            raise RuntimeError("Target closed")
        self.log.append((method, params))
        return {}


class FakeKeyboard:
    def __init__(self, log):
        self.log = log

    def press(self, key):
        self.log.append(("press", key))


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.sessions = 0

    def new_cdp_session(self, page):
        assert page is self.page
        self.sessions += 1
        return FakeCDP(self.page.log, fail=self.page.cdp_fails)


class FakePage:
    def __init__(self, cdp_fails=False):
        self.log = []
        self.cdp_fails = cdp_fails
        self.context = FakeContext(self)
        self.keyboard = FakeKeyboard(self.log)

    def on(self, _name, _handler):
        pass

    def click(self, selector, timeout=None, trial=False, force=False,
              no_wait_after=False):
        if trial:
            self.log.append(("check", selector))
        elif force:
            self.log.append(("press", selector))
        else:
            self.log.append(("click", selector))

    def fill(self, selector, value, timeout=None):
        self.log.append(("fill", selector, value))


def _adapter(**kwargs):
    page = FakePage(**kwargs)
    return pa.PlaywrightAdapter(None, None, page), page


def test_an_unlocked_page_is_driven_exactly_as_before():
    adapter, page = _adapter()
    adapter.click("#save")
    adapter.press_key("Enter")
    assert page.log == [("click", "#save"), ("press", "Enter")]
    assert page.context.sessions == 0          # no CDP session for nothing


def test_a_click_on_a_locked_page_opens_the_lock_for_the_click_alone():
    adapter, page = _adapter()
    assert adapter.lock_input(True) is True
    adapter.click("#save")
    # Every check a click makes is made while still locked (trial), so the gap
    # the person could click through is the press and release alone.
    assert page.log == [LOCK, ("check", "#save"), UNLOCK, ("press", "#save"), LOCK]


def test_a_key_press_on_a_locked_page_opens_the_lock_for_the_press_alone():
    adapter, page = _adapter()
    adapter.lock_input(True)
    adapter.press_key("Enter")
    assert page.log == [LOCK, UNLOCK, ("press", "Enter"), LOCK]


def test_fill_goes_through_the_lock_as_it_is():
    # Typed text arrives as Input.insertText, which the lock does not stop.
    adapter, page = _adapter()
    adapter.lock_input(True)
    adapter.fill("#name", "x")
    assert page.log == [LOCK, ("fill", "#name", "x")]


def test_the_page_is_locked_again_when_the_click_fails():
    adapter, page = _adapter()
    adapter.lock_input(True)

    def refuse(selector, timeout=None, **_kwargs):
        if _kwargs.get("force"):
            raise RuntimeError("detached")

    page.click = refuse
    with pytest.raises(RuntimeError):
        adapter.click("#save")
    assert page.log[-1] == LOCK


def test_a_click_that_fails_its_checks_never_opens_the_lock():
    adapter, page = _adapter()
    adapter.lock_input(True)

    def covered(selector, timeout=None, **_kwargs):
        raise RuntimeError("covered")

    page.click = covered
    with pytest.raises(RuntimeError):
        adapter.click("#save")
    assert page.log == [LOCK]


def test_one_cdp_session_serves_the_whole_lock():
    adapter, page = _adapter()
    adapter.lock_input(True)
    adapter.click("#a")
    adapter.lock_input(False)
    assert page.context.sessions == 1
    adapter.click("#b")                        # unlocked now: straight through
    assert page.log[-1] == ("click", "#b")


def test_a_lock_that_cannot_be_set_says_so_and_leaves_the_page_alone():
    adapter, page = _adapter(cdp_fails=True)
    assert adapter.lock_input(True) is False
    adapter.click("#save")
    assert page.log == [("click", "#save")]
