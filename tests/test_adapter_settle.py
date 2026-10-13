"""PlaywrightAdapter.settle: wait for the page's own requests, not for ever."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

pytest.importorskip("playwright")

from adapters import playwright_adapter as pa  # noqa: E402


class FakeRequest:
    def __init__(self, url, kind="fetch"):
        self.url = url
        self.resource_type = kind


class FakePage:
    """Time advances only through wait_for_timeout; a script of events may land
    during a wait, the way Playwright's sync API delivers them."""

    def __init__(self):
        self.handlers = {}
        self.now = 0
        self.scheduled = []          # (at ms, callable)
        self.evaluated = []

    def on(self, name, handler):
        self.handlers[name] = handler

    def emit(self, name, request):
        self.handlers[name](request)

    def wait_for_timeout(self, ms):
        self.now += ms
        due = [one for one in self.scheduled if one[0] <= self.now]
        self.scheduled = [one for one in self.scheduled if one[0] > self.now]
        for _at, action in due:
            action()

    def evaluate(self, script):
        self.evaluated.append(script)


def _adapter():
    page = FakePage()
    return pa.PlaywrightAdapter(None, None, page), page


def test_settle_waits_for_a_request_the_step_set_off():
    adapter, page = _adapter()
    rpc = FakeRequest("http://localhost:8069/web/dataset/call_kw")
    page.scheduled = [(100, lambda: page.emit("request", rpc)),
                      (1200, lambda: page.emit("requestfinished", rpc))]
    adapter.settle()
    # Not before the request finished, plus a quiet spell after it.
    # (the quiet spell counts from the poll that saw it end)
    assert page.now >= 1200 + pa.SETTLE_QUIET_MS - pa.SETTLE_POLL_MS
    assert page.evaluated, "two frames are awaited at the end"


def test_settle_does_not_wait_on_the_bus():
    adapter, page = _adapter()
    page.emit("request", FakeRequest("ws://localhost:8069/websocket", "websocket"))
    page.emit("request", FakeRequest("http://localhost:8069/longpolling/poll"))
    adapter.settle()
    assert page.now < 1000


def test_settle_gives_up_at_its_timeout():
    adapter, page = _adapter()
    page.emit("request", FakeRequest("http://localhost:8069/web/stuck"))
    adapter.settle(timeout_ms=1000)
    assert 1000 <= page.now < 1200


def test_a_quiet_page_is_not_waited_out():
    # Nothing started or ended through the first pause: the page is not busy,
    # and the quiet spell would only be sat through - on every picture.
    adapter, page = _adapter()
    adapter.settle()
    assert page.now == pa.SETTLE_FIRST_MS
    assert page.evaluated, "two frames are still awaited"


def test_a_request_that_ended_during_the_pause_still_counts_as_busy():
    adapter, page = _adapter()
    rpc = FakeRequest("http://localhost:8069/web/dataset/call_kw")
    page.emit("request", rpc)                       # the step's own request
    page.scheduled = [(100, lambda: page.emit("requestfinished", rpc))]
    adapter.settle()
    assert page.now >= pa.SETTLE_FIRST_MS + pa.SETTLE_QUIET_MS

