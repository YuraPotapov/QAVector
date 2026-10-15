"""The run's step-by-step guide (engine.livedoc).

What matters: the steps a reader needs are in it and nothing else, they are
grouped by the page they happen on, each picture points at the right elements,
and a run that fails still leaves a guide that says where it stopped.
"""

import json
import os
import textwrap

import pytest

from domain.result import ERROR, FAIL, PASS, FlowResult, StepResult
from engine import compiler, livedoc
from engine.context import RunContext

LIST = {"key": "https://shop.example.invalid/odoo/orders", "title": "Orders", "dialog": ""}
FORM = {"key": "https://shop.example.invalid/odoo/orders/*", "title": "New order",
        "dialog": ""}
CONFIRM = {"key": FORM["key"] + "|dialog:Confirm", "title": "New order",
           "dialog": "Confirm"}


class FakeAdapter:
    """Answers doc_capture the way the Playwright adapter does, from a script:
    the page the window is on, and where each selector's element is."""

    def __init__(self):
        self.page = dict(LIST)
        self.boxes = {}
        self.labels = {}
        self.secret = set()
        self.covered = set()
        self.calls = []

    def doc_capture(self, path, targets, focus=False):
        self.calls.append((os.path.basename(path), list(targets), focus))
        with open(path, "wb") as handle:
            handle.write(b"\x89PNG")
        return {"page": dict(self.page),
                "elements": [None if target not in self.boxes else
                             {"box": self.boxes[target],
                              "label": self.labels.get(target, ""),
                              "secret": target in self.secret,
                              "covered": target in self.covered}
                             for target in targets]}


def _write(base, relpath, content):
    path = os.path.join(str(base), relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(textwrap.dedent(content))


def _guide(tmp_path, scenario, labels=None):
    steps, plan = compiler.compile_plan(scenario, str(tmp_path / "flows"),
                                        ctx=RunContext(user={"login": "ann"}, env={}))
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    adapter, written = FakeAdapter(), []
    doc = livedoc.LiveDoc(str(out), adapter, labels=labels or dict(livedoc.LABELS),
                          written=lambda: written.append(1), clock=lambda: 1000.0)
    doc.start(plan, steps, scenario, session="ann", role="Agent")
    return doc, steps, adapter, out, written


def _run(doc, steps, adapter, script=None, fail_at=None):
    """Drive the guide as the runner does; ``script[i]`` changes the page first."""
    for index, step in enumerate(steps):
        if script and index in script:
            script[index](adapter)
        doc.before(index, step)
        status = PASS if index != fail_at else FAIL
        doc.after(index, step, StepResult(index, step.action, status, step.target,
                                          step.value, "boom\nat line 2" if status != PASS
                                          else "ok"))
        if status != PASS:
            return FAIL
    return PASS


def _items(doc):
    return [item for chapter in doc.data()["chapters"]
            for segment in chapter["segments"] for item in segment["items"]]


ORDER = """
    id: order
    name: Place an order
    description: How an order is made
    tags: []
    steps:
      - goto: "https://shop.example.invalid/odoo/orders"
      - click: "#new"
        doc: "Press New"
      - fill: {target: "#customer", value: "Ann"}
        doc: "Pick the customer"
      - press: ArrowDown
      - click: "#save"
        doc: "Save the order"
      - assert_visible: "#number"
        doc: "The order gets its number"
"""


def test_only_the_steps_with_a_doc_are_described(tmp_path):
    _write(tmp_path, "flows/scenarios/order.yaml", ORDER)
    doc, steps, adapter, _out, _w = _guide(tmp_path, "order")
    _run(doc, steps, adapter)
    texts = [(item["kind"], item["text"]) for item in _items(doc)]
    assert texts == [("step", "Press New"), ("step", "Pick the customer"),
                     ("step", "Save the order"), ("result", "The order gets its number")]
    assert doc.data()["mode"] == "doc"


def test_an_action_is_pictured_before_it_acts_and_a_check_after(tmp_path):
    _write(tmp_path, "flows/scenarios/order.yaml", ORDER)
    doc, steps, adapter, _out, _w = _guide(tmp_path, "order")
    doc.before(1, steps[1])
    assert adapter.calls[-1][:2] == ("step_002.png", ["#new"])
    doc.after(1, steps[1], StepResult(1, "click", PASS))
    assert len(adapter.calls) == 1                 # not again once it acted
    doc.before(5, steps[5])
    assert len(adapter.calls) == 1                 # a check waits for its result
    doc.after(5, steps[5], StepResult(5, "assert_visible", PASS))
    assert adapter.calls[-1][:2] == ("step_006.png", ["#number"])


def test_steps_on_one_page_share_a_picture_that_numbers_them_all(tmp_path):
    _write(tmp_path, "flows/scenarios/order.yaml", ORDER)
    doc, steps, adapter, out, _w = _guide(tmp_path, "order")
    adapter.boxes = {"#customer": [0.1, 0.2, 0.3, 0.05], "#save": [0.8, 0.05, 0.1, 0.04],
                     "#number": [0.4, 0.05, 0.2, 0.04]}

    def to_form(fake):
        fake.page = dict(FORM)
    _run(doc, steps, adapter, script={2: to_form})

    segments = doc.data()["chapters"][0]["segments"]
    assert [segment["title"] for segment in segments] == ["Orders", "New order"]
    form = segments[1]
    # The last picture on the form, with the customer field found again on it.
    assert form["shot"] == "doc/step_006.png"
    assert adapter.calls[-1][1] == ["#number", "#customer", "#save"]
    assert sorted(form["boxes"]) == sorted(str(item["id"]) for item in form["items"])
    # Numbered on from "Press New" on the list before it.
    assert [item.get("n") for item in form["items"]] == [2, 3, None]

    page = (out / "documentation.html").read_text(encoding="utf-8")
    assert page.count('<figure class="overview">') == 2
    assert '<figure class="own">' not in page
    assert "left:10.00%;top:20.00%" in page


def test_a_step_whose_element_has_gone_keeps_its_own_picture(tmp_path):
    _write(tmp_path, "flows/scenarios/order.yaml", ORDER)
    doc, steps, adapter, out, _w = _guide(tmp_path, "order")
    adapter.page = dict(FORM)
    adapter.boxes = {"#customer": [0.1, 0.2, 0.3, 0.05]}

    def dropdown_closes(fake):
        fake.boxes = {"#save": [0.8, 0.05, 0.1, 0.04]}
    _run(doc, steps, adapter, script={4: dropdown_closes})
    page = (out / "documentation.html").read_text(encoding="utf-8")
    assert page.count('<figure class="own">') == 1
    assert "doc/step_003.png" in page


def test_a_dialog_is_a_section_of_its_own(tmp_path):
    _write(tmp_path, "flows/scenarios/order.yaml", ORDER)
    doc, steps, adapter, out, _w = _guide(tmp_path, "order")

    def confirm(fake):
        fake.page = dict(CONFIRM)
    _run(doc, steps, adapter, script={4: confirm})
    segments = doc.data()["chapters"][0]["segments"]
    assert [(segment["title"], segment["dialog"]) for segment in segments] == [
        ("Orders", False), ("Confirm", True)]
    assert '<span class="kicker">Dialog</span>Confirm' in (
        out / "documentation.html").read_text(encoding="utf-8")


def test_a_top_level_block_is_a_chapter(tmp_path):
    _write(tmp_path, "flows/shop/sign_in.yaml", """
        id: shop.sign_in
        name: Sign in
        description: Every guide starts here
        steps:
          - fill: {target: "#login", value: "{{user.login}}"}
            doc: "Enter your login"
    """)
    _write(tmp_path, "flows/scenarios/signed.yaml", """
        id: signed
        name: Signed in
        tags: []
        steps:
          - use: shop.sign_in
          - click: "#new"
            doc: "Press New"
    """)
    doc, steps, adapter, out, _w = _guide(tmp_path, "signed")
    _run(doc, steps, adapter)
    chapters = doc.data()["chapters"]
    assert [(chapter["title"], chapter["description"]) for chapter in chapters] == [
        ("Sign in", "Every guide starts here"), ("", "")]
    page = (out / "documentation.html").read_text(encoding="utf-8")
    assert "<h2>Sign in</h2>" in page and page.count("<h2>") == 1


def test_with_no_doc_anywhere_a_draft_is_made_from_the_page(tmp_path):
    _write(tmp_path, "flows/scenarios/plain.yaml", """
        id: plain
        name: Plain
        tags: []
        steps:
          - goto: "https://shop.example.invalid/odoo/orders"
          - fill: {target: "#login", value: "ann"}
          - fill: {target: "#password", value: "s3cret"}
          - press: Enter
          - wait_for: "#menu"
          - click: "#new"
    """)
    doc, steps, adapter, out, _w = _guide(tmp_path, "plain")
    adapter.boxes = {"#login": [0, 0, 0.1, 0.1], "#password": [0, 0.2, 0.1, 0.1]}
    adapter.labels = {"#login": "Login", "#password": "Password"}
    adapter.secret = {"#password"}
    _run(doc, steps, adapter)
    assert [item["text"] for item in _items(doc)] == [
        "Open \u201cOrders\u201d",
        "Enter \u201cann\u201d in \u201cLogin\u201d",
        "Enter \u201c%s\u201d in \u201cPassword\u201d" % livedoc.HIDDEN,
        "Click the marked element",          # no name on the page, CSS in the scenario
    ]
    page = (out / "documentation.html").read_text(encoding="utf-8")
    assert "s3cret" not in page and "s3cret" not in (
        out / "doc" / "documentation.json").read_text(encoding="utf-8")
    assert livedoc.LABELS["draft"].split(":")[0] in page


def test_a_failed_run_says_where_it_stopped(tmp_path):
    _write(tmp_path, "flows/scenarios/order.yaml", ORDER)
    doc, steps, adapter, out, _w = _guide(tmp_path, "order")
    status = _run(doc, steps, adapter, fail_at=4)
    result = FlowResult(scenario="order", session="ann", status=status,
                        error="click(#save): boom", duration_ms=4200)
    doc.finish(result)
    items = _items(doc)
    assert items[-2]["text"] == "Save the order" and items[-2]["status"] == FAIL
    assert items[-1]["kind"] == "stopped" and items[-1]["message"] == "boom\nat line 2"
    assert adapter.calls[-1][:2] == ("stopped.png", [])
    page = (out / "documentation.html").read_text(encoding="utf-8")
    assert "<b>Incomplete</b>" in page and "click(#save): boom" in page
    assert 'class="item step failed"' in page
    assert ('<figure class="own"><div class="shot"><div class="clip">'
            '<img src="doc/stopped.png"') in page


def test_the_page_follows_the_run_and_never_is_half_written(tmp_path):
    _write(tmp_path, "flows/scenarios/order.yaml", ORDER)
    doc, steps, adapter, out, written = _guide(tmp_path, "order")
    assert (out / "documentation.html").exists() and written == [1]   # at once
    _run(doc, steps, adapter)
    page = (out / "documentation.html").read_text(encoding="utf-8")
    assert page.count('id="latest"') == 1
    assert 'id="latest"><div class="body"><p class="kicker">Expected result' in page
    assert len(written) == 5
    assert not [name for name in os.listdir(out) + os.listdir(out / "doc")
                if name.endswith(".part")]
    data = json.loads((out / "doc" / "documentation.json").read_text(encoding="utf-8"))
    assert data["status"] == "running" and data["title"] == "Place an order"
    doc.finish(FlowResult(scenario="order", session="ann", status=PASS, duration_ms=1500))
    page = (out / "documentation.html").read_text(encoding="utf-8")
    assert "Complete" in page and "1.5 s" in page


def test_what_the_author_wrote_is_shown_not_run(tmp_path):
    _write(tmp_path, "flows/scenarios/odd.yaml", """
        id: odd
        name: "<b>Odd</b>"
        tags: []
        steps:
          - click: "#a"
            doc: "<script>alert(1)</script> & more"
    """)
    doc, steps, adapter, out, _w = _guide(tmp_path, "odd")
    _run(doc, steps, adapter)
    page = (out / "documentation.html").read_text(encoding="utf-8")
    assert "<script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt; &amp; more" in page
    assert "<h1>&lt;b&gt;Odd&lt;/b&gt;</h1>" in page


def test_a_step_with_no_page_is_text_only(tmp_path):
    _write(tmp_path, "flows/scenarios/svc.yaml", """
        id: svc
        name: Services
        tags: []
        steps:
          - service_restart: "Shop/Web"
            doc: "Restart the web service"
    """)
    doc, steps, adapter, _out, _w = _guide(tmp_path, "svc")
    _run(doc, steps, adapter)
    assert adapter.calls == []
    assert [(item["text"], item["shot"]) for item in _items(doc)] == [
        ("Restart the web service", "")]


def test_a_picture_that_cannot_be_taken_leaves_the_step_in(tmp_path):
    _write(tmp_path, "flows/scenarios/order.yaml", ORDER)
    doc, steps, adapter, _out, _w = _guide(tmp_path, "order")

    def broken(*_args, **_kwargs):
        raise RuntimeError("Target page, context or browser has been closed")
    adapter.doc_capture = broken
    _run(doc, steps, adapter)
    assert len(_items(doc)) == 4 and all(not item["shot"] for item in _items(doc))


# -- its own words ----------------------------------------------------------------

def test_the_guide_speaks_the_language_of_doc_labels(tmp_path):
    path = tmp_path / "doc_labels.json"
    path.write_text(json.dumps({"lang": "xx", "click": "Tap {label}!",
                                "result": "Then", "unknown": "ignored", "draft": 3}),
                    encoding="utf-8")
    labels = livedoc.load_labels(str(path))
    assert labels["click"] == "Tap {label}!" and labels["result"] == "Then"
    assert labels["draft"] == livedoc.LABELS["draft"] and "unknown" not in labels
    assert livedoc._say(labels, "click", label="New") == "Tap New!"


def test_a_broken_translation_falls_back_to_english(tmp_path, caplog):
    path = tmp_path / "doc_labels.json"
    path.write_text("{not json", encoding="utf-8")
    assert livedoc.load_labels(str(path)) == livedoc.LABELS
    assert livedoc.load_labels(str(tmp_path / "missing.json")) == livedoc.LABELS
    bad = dict(livedoc.LABELS, click="Tap {label")
    assert livedoc._say(bad, "click", label="New") == "Click \u201cNew\u201d"


@pytest.mark.parametrize("status", [FAIL, ERROR])
def test_an_unfinished_guide_says_so_in_its_header(status):
    page = livedoc.render_html({"scenario": "s", "title": "S", "status": status,
                                "error": "stopped at step 3 of 9", "mode": "doc",
                                "chapters": []})
    assert "<b>Incomplete</b> \u2014 stopped at step 3 of 9" in page


def test_only_the_steps_that_act_are_in_the_picture_s_sequence(tmp_path):
    # A check is outlined for what it found, but it is not a step the reader
    # takes: no number, and the path from one action to the next skips it.
    _write(tmp_path, "flows/scenarios/order.yaml", ORDER)
    doc, steps, adapter, out, _w = _guide(tmp_path, "order")
    adapter.page = dict(FORM)
    adapter.boxes = {"#new": [0.05, 0.1, 0.1, 0.05], "#customer": [0.1, 0.2, 0.3, 0.05],
                     "#save": [0.8, 0.05, 0.1, 0.04], "#number": [0.4, 0.5, 0.2, 0.04]}
    _run(doc, steps, adapter)
    page = (out / "documentation.html").read_text(encoding="utf-8")
    # Through each badge: the middle of its box's left edge.
    assert '<polyline points="5.00,12.50 10.00,22.50 80.00,7.00"/>' in page
    assert page.count("<b>") - page.count("<b>Incomplete") == 3
    assert '<span class="box check" style="left:40.00%;top:50.00%;width:20.00%;' \
           'height:4.00%"></span>' in page


def test_a_picture_is_cut_below_what_it_points_at(tmp_path):
    # Most of a window is empty page below the form: the picture stops a little
    # under its lowest callout, and the callouts are placed in what is left.
    _write(tmp_path, "flows/scenarios/order.yaml", ORDER)
    doc, steps, adapter, out, _w = _guide(tmp_path, "order")
    adapter.page = dict(FORM, size=[1000, 600])
    adapter.boxes = {"#customer": [0.1, 0.2, 0.3, 0.05], "#save": [0.1, 0.3, 0.1, 0.05]}
    _run(doc, steps, adapter)
    page = (out / "documentation.html").read_text(encoding="utf-8")
    # Lowest callout ends at 0.35; plus the margin, 0.47 - but never under half.
    assert 'class="shot cropped" style="aspect-ratio: 1000 / 300"' in page
    assert "left:10.00%;top:60.00%;width:10.00%;height:10.00%" in page


def test_an_option_is_named_by_its_text(tmp_path):
    _write(tmp_path, "flows/scenarios/prio.yaml", """
        id: prio
        name: Priority
        tags: []
        steps:
          - select: {target: "#priority", value: "high"}
            doc: "Choose the priority"
    """)
    doc, steps, adapter, _out, _w = _guide(tmp_path, "prio")
    adapter.boxes = {"#priority": [0.1, 0.1, 0.2, 0.05]}
    real = adapter.doc_capture

    def with_choices(path, targets, focus=False):
        answer = real(path, targets, focus)
        answer["elements"][0]["choices"] = [["low", "Low"], ["high", "High"]]
        return answer
    adapter.doc_capture = with_choices
    _run(doc, steps, adapter)
    assert _items(doc)[0]["value"] == "High"


def test_a_password_is_hidden_even_when_the_page_could_not_be_asked(tmp_path):
    _write(tmp_path, "flows/scenarios/sign.yaml", """
        id: sign
        name: Sign in
        tags: []
        steps:
          - fill: {target: "input[name='password']", value: "s3cret"}
            doc: "Enter your password"
          - fill: {target: "#login", value: "ann"}
            doc: "Enter your login"
    """)
    doc, steps, adapter, out, _w = _guide(tmp_path, "sign")

    def unreachable(*_args, **_kwargs):
        raise RuntimeError("Target page, context or browser has been closed")
    adapter.doc_capture = unreachable
    _run(doc, steps, adapter)
    assert [item["value"] for item in _items(doc)] == [livedoc.HIDDEN, "ann"]
    assert "s3cret" not in (out / "documentation.html").read_text(encoding="utf-8")



def test_each_chapter_is_documented_or_drafted_on_its_own(tmp_path):
    # A scenario made of other scenarios: one doc: on the steps that restart a
    # server switched the drafts off for every chapter, and left a guide of four
    # lines out of 750 steps.
    _write(tmp_path, "flows/shop/restart.yaml", """
        id: shop.restart
        name: Restart
        steps:
          - goto: "https://shop.example.invalid/odoo"
            doc: "Open the app again"
          - wait_for: "#menu"
    """)
    _write(tmp_path, "flows/shop/order.yaml", """
        id: shop.order
        name: Order
        steps:
          - click: "#new"
          - fill: {target: "#amount", value: "100"}
          - assert_visible: "#number"
    """)
    _write(tmp_path, "flows/scenarios/both.yaml", """
        id: both
        name: Both
        tags: []
        steps:
          - use: shop.restart
          - use: shop.order
    """)
    doc, steps, adapter, out, _w = _guide(tmp_path, "both")
    adapter.labels = {"#new": "New", "#amount": "Amount"}
    adapter.boxes = {"#new": [0.1, 0.1, 0.1, 0.05], "#amount": [0.1, 0.2, 0.2, 0.05]}
    _run(doc, steps, adapter)
    data = doc.data()
    assert data["mode"] == "mixed"
    chapters = [(chapter["title"], chapter["drafted"],
                 [item["text"] for segment in chapter["segments"] for item in segment["items"]])
                for chapter in data["chapters"]]
    assert chapters == [
        ("Restart", False, ["Open the app again"]),
        ("Order", True, ["Click \u201cNew\u201d",
                         "Enter \u201c100\u201d in \u201cAmount\u201d"]),
    ]
    page = (out / "documentation.html").read_text(encoding="utf-8")
    # The draft is said where it is true, under its own chapter, not in the header.
    header, body = page.split("</header>", 1)
    assert livedoc.LABELS["draft"] not in header
    assert body.count(livedoc.LABELS["draft"]) == 1
    assert body.index("<h2>Order</h2>") < body.index(livedoc.LABELS["draft"])



def test_a_named_selector_reads_as_a_name_and_raw_css_does_not():
    assert livedoc._readable("save_button") == "save_button"
    assert livedoc._readable(".o_mrd_card:has(> .o_mrd_card_head) .o_mrd_exp") == ""
    assert livedoc._readable("#new") == ""
    assert livedoc._readable(None) == ""


def test_steps_on_the_same_element_share_one_numbered_box(tmp_path):
    _write(tmp_path, "flows/scenarios/measure.yaml", """
        id: measure
        name: Measure
        tags: []
        steps:
          - click: "#reset"
            doc: "Reset the filters"
          - select: {target: "#measure", value: "a"}
            doc: "Show the first measure"
          - select: {target: "#measure", value: "b"}
            doc: "Show the second"
    """)
    doc, steps, adapter, out, _w = _guide(tmp_path, "measure")
    adapter.boxes = {"#reset": [0.1, 0.1, 0.1, 0.05], "#measure": [0.5, 0.1, 0.2, 0.05]}
    _run(doc, steps, adapter)
    page = (out / "documentation.html").read_text(encoding="utf-8")
    assert page.count('<span class="box act"') == 2
    assert "<b>2, 3</b>" in page and "<b>1</b>" in page



def test_steps_are_numbered_through_the_chapter_and_start_again_in_the_next(tmp_path):
    # One step on each of three pages read "1, 1, 1".
    _write(tmp_path, "flows/shop/sign_in.yaml", """
        id: shop.sign_in
        name: Sign in
        steps:
          - fill: {target: "#login", value: "{{user.login}}"}
            doc: "Enter your login"
          - click: "#enter"
            doc: "Sign in"
    """)
    _write(tmp_path, "flows/scenarios/hops.yaml", """
        id: hops
        name: Hops
        tags: []
        steps:
          - use: shop.sign_in
          - click: "#orders"
            doc: "Open the orders"
          - click: "#new"
            doc: "Press New"
          - click: "#save"
            doc: "Save the order"
    """)
    doc, steps, adapter, out, _w = _guide(tmp_path, "hops")

    def to(page):
        return lambda fake: setattr(fake, "page", dict(page))
    _run(doc, steps, adapter, script={1: to(FORM), 3: to(LIST), 4: to(FORM)})
    chapters = doc.data()["chapters"]
    numbers = [[[item["n"] for item in segment["items"]] for segment in chapter["segments"]]
               for chapter in chapters]
    assert numbers == [[[1], [2]], [[1], [2], [3]]]


CRON = """
    id: cron
    name: Run a scheduled action
    tags: []
    steps:
      - click: "#run"
        doc: "Press Run manually"
      - type: wait_for
        target: "#run:not([disabled])"
        doc: "The action has run"
      - click: "#apps"
        doc: "Open the app menu"
      - click: "#claims"
        doc: "Go to Claims"
"""


def test_a_step_pictured_with_a_dropdown_over_earlier_steps_keeps_its_own_picture(tmp_path):
    # The app menu, open for "Go to Claims", lay over "Run manually": its box
    # was drawn on the menu, round a button the picture did not show.
    _write(tmp_path, "flows/scenarios/cron.yaml", CRON)
    doc, steps, adapter, out, _w = _guide(tmp_path, "cron")
    adapter.page = dict(FORM)
    adapter.boxes = {"#run": [0.01, 0.12, 0.07, 0.03],
                     "#run:not([disabled])": [0.01, 0.12, 0.07, 0.03],
                     "#apps": [0.0, 0.0, 0.03, 0.05], "#claims": [0.0, 0.06, 0.1, 0.03]}

    def menu_opens(fake):
        fake.covered = {"#run", "#run:not([disabled])"}
    _run(doc, steps, adapter, script={3: menu_opens})

    segment, = doc.data()["chapters"][0]["segments"]
    run, ran, apps, claims = segment["items"]
    assert segment["shot"] == "doc/step_003.png"         # before the menu opened
    assert sorted(segment["boxes"]) == sorted(str(item["id"]) for item in (run, ran, apps))
    assert claims["shot"] == "doc/step_004.png" and claims["box"]
    page = (out / "documentation.html").read_text(encoding="utf-8")
    assert page.count('<figure class="own">') == 1
    assert 'src="doc/step_004.png"' in page


def test_what_was_covered_already_does_not_hold_the_picture_back(tmp_path):
    # Covered on the section's picture too (a label floating over its field, say):
    # the next picture is no worse for it, and takes the section's place.
    _write(tmp_path, "flows/scenarios/cron.yaml", CRON)
    doc, steps, adapter, out, _w = _guide(tmp_path, "cron")
    adapter.page = dict(FORM)
    adapter.boxes = {"#run": [0.01, 0.12, 0.07, 0.03], "#apps": [0.0, 0.0, 0.03, 0.05],
                     "#claims": [0.0, 0.06, 0.1, 0.03]}
    adapter.covered = {"#run"}
    _run(doc, steps, adapter)
    segment, = doc.data()["chapters"][0]["segments"]
    assert segment["shot"] == "doc/step_004.png"
    assert '<figure class="own">' not in (out / "documentation.html").read_text(
        encoding="utf-8")
