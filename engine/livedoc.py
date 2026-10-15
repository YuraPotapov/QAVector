"""A scenario run's step-by-step guide: live documentation, written as it runs.

The screenshots a run takes are evidence for whoever tests the app. The same
run, told for someone learning the feature, is a guide: what to do, on which
page, and where on that page - and since it is written by running the scenario,
it is as current as the last run. ``--report-doc`` turns it on.

What it writes, into the scenario's report folder::

    documentation.html        the guide, rewritten after every step it describes
    doc/documentation.json    what the page is rendered from
    doc/step_NNN.png          the window before step NNN acts (a check: after it passes)

What it describes is decided for each chapter - each top-level ``use:`` block,
and the scenario's own steps beside them. A chapter whose author wrote a
``doc:`` shows the steps that carry one: the author says what a step means, in
whatever language the guide is read in. A chapter with no ``doc:`` in it gets a
draft instead, made from its clicks, fills, selects and gotos and the names the
page itself gives their elements. Decided for the whole scenario, one ``doc:``
on the steps that restart a server left a guide of four lines out of 750 steps.

How it groups them: a top-level ``use:`` block is a chapter, and inside it a run
of steps on the same page - or in the same dialog - is one section with one
picture, every step numbered on it where its element is - numbered through the
chapter, so the numbers go on from one section to the next. A step whose
element is gone by then (an option in a dropdown that has closed) keeps a
picture of its own; so does one pictured with something open over the steps
before it (the dropdown it chooses in), and the section keeps the picture that
shows them.

What it deliberately does not do: run a script, reach the network or anything
outside the folder - the page is opened in the GUI's preview and printed to PDF
from there. And it never fails a run: every capture is best-effort, like every
other report artifact.
"""

import html
import json
import logging
import os
import re
import time
from urllib.parse import quote

import runtime_paths
from domain.plan import STEP
from domain.result import PASS
from engine import assertions, compiler

log = logging.getLogger("flowengine.livedoc")

HTML_NAME = "documentation.html"
DOC_DIR = "doc"
JSON_NAME = "documentation.json"

#: Steps that act on the page: pictured before they act, the element outlined.
BEFORE = ("click", "fill", "select", "press")
#: What a scenario with no ``doc:`` at all is described by.
DRAFTED = ("click", "fill", "select", "goto")
#: How much of a failure the guide quotes; the rest is in result.json.
MESSAGE_LINES = 6
MESSAGE_CHARS = 400
#: What a password field's value is shown as.
HIDDEN = "\u2022" * 6
#: A target named like this is taken for a password when the page could not be
#: asked - a guide is a document people pass around, and a password typed into
#: it once is a password in every copy.
SECRET_NAMES = re.compile(r"pass(word|wd)?|pwd|secret|token", re.IGNORECASE)
#: A picture is cut off a little below the lowest element outlined on it - the
#: rest of a window is mostly empty page - but never to less than half of it.
CROP_MARGIN = 0.12
CROP_LEAST = 0.5

#: The guide's own words, and the sentences it drafts. English here; a user puts
#: them in their own language in doc_labels.json (runtime_paths.doc_labels_path).
LABELS = {
    "lang": "en",
    "contents": "Contents",
    "result": "Expected result",
    "stopped": "The run stopped here",
    "incomplete": "Incomplete",
    "running": "Being written",
    "complete": "Complete",
    "draft": ("A draft: these sentences were made from the steps. Give a step "
              "a doc: to say it yourself."),
    "dialog": "Dialog",
    "click": "Click \u201c{label}\u201d",
    "fill": "Enter \u201c{value}\u201d in \u201c{label}\u201d",
    "select": "Choose \u201c{value}\u201d in \u201c{label}\u201d",
    # When neither the page nor the scenario has a name for the element: the
    # picture points at it.
    "click_marked": "Click the marked element",
    "fill_marked": "Enter \u201c{value}\u201d in the marked field",
    "select_marked": "Choose \u201c{value}\u201d in the marked field",
    "goto": "Open \u201c{label}\u201d",
    "press": "Press {value}",
}

_warned = set()


def load_labels(path=None):
    """LABELS, with whatever ``doc_labels.json`` says in their place.

    A missing file is the normal case. One that cannot be read, or a key whose
    value is not text, leaves the English word in place and says so once - a
    typo in a translation must not cost anybody their guide.
    """
    path = path or runtime_paths.doc_labels_path()
    labels = dict(LABELS)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return labels
    except (OSError, ValueError) as exc:
        _warn_once(path, "cannot read %s, the guide stays in English: %s" % (path, exc))
        return labels
    if not isinstance(data, dict):
        _warn_once(path, "%s is not a JSON object, the guide stays in English" % path)
        return labels
    for key, value in data.items():
        if key in LABELS and isinstance(value, str):
            labels[key] = value
    return labels


def _warn_once(key, message):
    if key not in _warned:
        _warned.add(key)
        log.warning(message)


class _Blank(dict):
    """A format map in which a field nobody filled in reads as nothing."""

    def __missing__(self, key):
        return ""


def _say(labels, key, **fields):
    """``labels[key]`` filled in; the English one when a translation is broken."""
    try:
        return labels.get(key, LABELS[key]).format_map(_Blank(fields))
    except (ValueError, IndexError, AttributeError):
        return LABELS[key].format_map(_Blank(fields))


def _short(message):
    lines = [one.strip() for one in str(message or "").splitlines() if one.strip()]
    return "\n".join(lines[:MESSAGE_LINES])[:MESSAGE_CHARS]


class LiveDoc:
    """One scenario's guide, kept on disk as the run goes.

    The runner's reporter calls :meth:`start` once the scenario has compiled,
    :meth:`before` and :meth:`after` around every step, and :meth:`finish` at
    the end. ``settle`` lets the page finish what the last step set off before a
    picture; ``written`` is told each time the files change.
    """

    def __init__(self, out_dir, adapter, labels=None, settle=None, written=None,
                 clock=time.time):
        self._out = out_dir
        self._adapter = adapter
        self._labels = labels if labels is not None else load_labels()
        self._settle = settle or (lambda: None)
        self._written = written or (lambda: None)
        self._clock = clock
        self._head = {}
        self._mode = "auto"       # "doc", "auto", or "mixed" across chapters
        self._curated = set()     # chapter keys whose author wrote a doc:
        self._names = {}          # step index -> the target's name in the YAML
        self._blocks = {}         # step index -> top-level block node, or None
        self._chapters = []
        self._chapter_key = object()
        self._segment = None
        # Ids of the steps covered already on the open section's picture: only
        # what a new picture covers anew makes it keep the old one.
        self._covered = set()
        self._ids = 0
        self._latest = None
        self._started = None

    # -- the run's side -----------------------------------------------------
    def start(self, plan, steps, scenario, session=None, role=None):
        self._started = self._clock()
        keys = set()
        for child in plan.children:
            block = None if child.kind == STEP else child
            key = _chapter_key(block)
            keys.add(key)
            for leaf in child.leaves():
                self._blocks[leaf.step_index] = block
                self._names[leaf.step_index] = leaf.target
                index = leaf.step_index
                if 0 <= index < len(steps) and (steps[index].doc or "").strip():
                    self._curated.add(key)
        if keys and keys <= self._curated:
            self._mode = "doc"
        elif self._curated:
            self._mode = "mixed"
        self._head = {
            "scenario": scenario,
            "title": plan.label or scenario,
            "description": plan.description or "",
            "session": session or "",
            "role": role or "",
            "started": time.strftime("%Y-%m-%d %H:%M", time.localtime(self._started)),
            "status": "running",
            "duration_ms": 0.0,
            "error": "",
        }
        os.makedirs(os.path.join(self._out, DOC_DIR), exist_ok=True)
        self._write()

    def before(self, index, step):
        """A step that acts is pictured before it does, with its element outlined."""
        if step.action in BEFORE and self._described(index, step):
            self._add(index, step, "step")

    def after(self, index, step, result):
        """A check or a goto is pictured once it has passed; a failure, at once."""
        if result.status != PASS:
            self._stopped(index, step, result)
            return
        if step.action in BEFORE or not self._described(index, step):
            return
        check = assertions.is_assertion(step.action) or step.action == "wait_for"
        self._add(index, step, "result" if check else "step")

    def finish(self, flow_result):
        self._head["status"] = flow_result.status
        self._head["duration_ms"] = flow_result.duration_ms
        if flow_result.status != PASS:
            self._head["error"] = _short(flow_result.error).split("\n")[0]
        self._write()

    # -- what goes in -------------------------------------------------------
    def _described(self, index, step):
        if _chapter_key(self._blocks.get(index)) in self._curated:
            return bool((step.doc or "").strip())
        return step.action in DRAFTED

    def _add(self, index, step, kind):
        chapter = self._chapter_for(index)
        own = step.target if self._outlined(step) else None
        # The open section is always the newest chapter's: _chapter_for closes
        # it when a chapter begins.
        earlier = [item for item in (self._segment or {}).get("items", [])
                   if item.get("selector") and item.get("box")]
        shot = "%s/step_%03d.png" % (DOC_DIR, index + 1)
        capture = None
        if step.action not in compiler.NO_BROWSER:
            self._settle()
            capture = self._capture(shot, [own] + [item["selector"] for item in earlier],
                                    focus=step.action == "press")
        page = capture["page"] if capture else None
        segment, same = self._segment_for(chapter, page)
        found = (capture or {}).get("elements") or [None]
        element = found[0] or {}

        ratio = _ratio(page)
        self._ids += 1
        item = {"id": self._ids, "index": index, "kind": kind, "action": step.action,
                "status": PASS, "selector": own, "box": element.get("box"),
                "shot": shot if capture else "", "ratio": ratio}
        label = element.get("label") or _readable(self._names.get(index))
        if step.action == "goto":
            label = (page or {}).get("title") or step.target or ""
        value = step.value
        if step.action in ("fill", "select") and value is not None:
            # A <select> is chosen by an option's value; a person sees its text.
            shown = dict(tuple(pair) for pair in element.get("choices") or []
                         if isinstance(pair, (list, tuple)) and len(pair) == 2)
            secret = element.get("secret") or (not element and SECRET_NAMES.search(
                "%s %s" % (step.target or "", self._names.get(index) or "")))
            value = HIDDEN if secret else shown.get(value, str(value))
        if (step.doc or "").strip():
            item["text"] = step.doc.strip()
            if step.action in ("fill", "select") and value is not None:
                # What to type, beside the author's sentence; a draft says it.
                item["value"] = value
        else:
            key = step.action
            if not label and key in ("click", "fill", "select"):
                key += "_marked"
            item["text"] = _say(self._labels, key, label=label,
                                value="" if value is None else value)
        if kind == "step":
            # Numbered through the chapter, not the section: a run of pages with
            # one step each read "1, 1, 1".
            item["n"] = 1 + sum(1 for one in chapter["segments"]
                                for each in one["items"] if each["kind"] == "step")
        segment["items"].append(item)

        if capture:
            again = list(zip(earlier, found[1:])) if same else []
            covered = {str(before["id"]) for before, one in again
                       if one and one.get("covered")}
            if element.get("covered"):
                covered.add(str(item["id"]))
            if covered & (set(segment["boxes"]) - self._covered):
                # Something opened over what the section's picture shows - the
                # dropdown this step chooses in. The picture stays, and this
                # step keeps the one it took (_item shows it beside its text).
                pass
            else:
                boxes = {}
                if item["box"]:
                    boxes[str(item["id"])] = item["box"]
                for before, one in again:
                    if one and one.get("box"):
                        boxes[str(before["id"])] = one["box"]
                segment["shot"], segment["boxes"], segment["ratio"] = shot, boxes, ratio
                self._covered = covered
        self._latest = item["id"]
        self._write()

    def _stopped(self, index, step, result):
        for chapter in self._chapters:
            for segment in chapter["segments"]:
                for item in segment["items"]:
                    if item["index"] == index:
                        item["status"] = result.status
        chapter = self._chapter_for(index)
        shot = "%s/stopped.png" % DOC_DIR
        capture = None
        if step.action not in compiler.NO_BROWSER:
            capture = self._capture(shot, [])
        segment, _same = self._segment_for(chapter, capture["page"] if capture else None)
        self._ids += 1
        segment["items"].append({
            "id": self._ids, "index": index, "kind": "stopped", "action": step.action,
            "status": result.status, "text": _say(self._labels, "stopped"),
            "message": _short(result.message), "selector": None, "box": None,
            "shot": shot if capture else "",
            "ratio": _ratio(capture["page"] if capture else None)})
        self._latest = self._ids
        self._write()

    @staticmethod
    def _outlined(step):
        """Whether the step has an element to outline: a check that something
        is NOT there has nothing to point at."""
        return (step.action in compiler.SELECTOR_TARGET
                and step.action != "assert_not_visible")

    def _capture(self, shot, targets, focus=False):
        try:
            return self._adapter.doc_capture(os.path.join(self._out, shot), targets,
                                             focus=focus)
        except Exception as exc:  # a guide without a picture beats a failed run
            log.debug("guide: no picture for %s: %s", shot, exc)
            return None

    def _chapter_for(self, index):
        block = self._blocks.get(index)
        key = _chapter_key(block)
        if key != self._chapter_key or not self._chapters:
            self._chapter_key = key
            self._chapters.append({
                "title": block.label if block is not None else "",
                "description": (block.description or "") if block is not None else "",
                "drafted": key not in self._curated,
                "segments": []})
            self._segment = None
        return self._chapters[-1]

    def _segment_for(self, chapter, page):
        """The section a step on ``page`` belongs to, and whether it was already open.

        A step with no picture (a service step) stays in the section it is in.
        """
        current = self._segment
        if current is not None and (page is None or page["key"] == current["key"]):
            return current, True
        dialog = (page or {}).get("dialog") or ""
        segment = {"key": (page or {}).get("key"),
                   "title": dialog or (page or {}).get("title") or "",
                   "dialog": bool(dialog), "shot": "", "boxes": {}, "ratio": None,
                   "items": []}
        chapter["segments"].append(segment)
        self._segment = segment
        return segment, False

    # -- what comes out -----------------------------------------------------
    def data(self):
        head = dict(self._head)
        if head.get("status") == "running" and self._started is not None:
            head["duration_ms"] = (self._clock() - self._started) * 1000
        return dict(head, version=1, mode=self._mode, latest=self._latest,
                    labels=self._labels, chapters=self._chapters)

    def _write(self):
        data = self.data()
        _atomic(os.path.join(self._out, DOC_DIR, JSON_NAME),
                json.dumps(data, indent=2, ensure_ascii=False))
        _atomic(os.path.join(self._out, HTML_NAME), render_html(data))
        self._written()


def _readable(name):
    """A target's name from the scenario, if it reads as one.

    A named selector (``save_button``) does, as a last resort; a raw CSS
    selector put into a sentence for a reader does not.
    """
    name = (name or "").strip()
    return name if re.fullmatch(r"[\w-]+", name) else ""


def _chapter_key(block):
    """What a step's chapter is known by: its top-level block, or None for the
    scenario's own steps."""
    return block.id if block is not None else None


def _ratio(page):
    """The picture's height over its width, or None when the adapter did not say."""
    try:
        width, height = (float(part) for part in (page or {}).get("size") or ())
    except (TypeError, ValueError):
        return None
    return round(height / width, 4) if width > 0 and height > 0 else None


def _atomic(path, text):
    """Never a half-written file: the GUI may reload it at any moment."""
    partial = path + ".part"
    with open(partial, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(partial, path)


# -- the page -----------------------------------------------------------------

def render_html(data):
    """The guide as one HTML page: no script, pictures beside it in doc/."""
    labels = dict(LABELS, **(data.get("labels") or {}))
    esc = html.escape
    out = ['<!doctype html>', '<html lang="%s">' % esc(labels.get("lang") or "en"),
           '<head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           '<title>%s</title>' % esc(data.get("title") or data.get("scenario") or ""),
           '<style>%s</style></head><body>' % _CSS]

    out.append('<header>')
    out.append('<p class="kicker">%s</p>' % esc(data.get("scenario") or ""))
    out.append('<h1>%s</h1>' % esc(data.get("title") or data.get("scenario") or ""))
    if data.get("description"):
        out.append('<p class="lede">%s</p>' % esc(data["description"]))
    out.append('<p class="meta">%s</p>' % esc(" \u00b7 ".join(_meta(data, labels))))
    status = data.get("status")
    if status not in ("running", PASS):
        out.append('<p class="banner bad"><b>%s</b>%s</p>' % (
            esc(labels["incomplete"]),
            (" \u2014 " + esc(data["error"])) if data.get("error") else ""))
    if data.get("mode") == "auto":
        out.append('<p class="banner">%s</p>' % esc(labels["draft"]))
    out.append('</header>')

    chapters = data.get("chapters") or []
    sections = [(chapter, segment) for chapter in chapters
                for segment in chapter["segments"]]
    numbers = {id(segment): place for place, (_c, segment) in enumerate(sections, 1)}
    if sum(1 for _c, segment in sections if segment["title"]) >= 3:
        out.append(_contents(chapters, numbers, labels))

    latest = data.get("latest")
    for chapter in chapters:
        out.append('<section class="chapter">')
        if chapter.get("title"):
            out.append('<h2>%s</h2>' % esc(chapter["title"]))
            if chapter.get("description"):
                out.append('<p class="lede">%s</p>' % esc(chapter["description"]))
        if data.get("mode") == "mixed" and chapter.get("drafted"):
            # Said where it is true: the chapters beside it were written.
            out.append('<p class="banner">%s</p>' % esc(labels["draft"]))
        for segment in chapter["segments"]:
            out.append(_segment(segment, numbers[id(segment)], latest, labels))
        out.append('</section>')
    out.append('</body></html>')
    return "\n".join(out) + "\n"


def _meta(data, labels):
    parts = [data.get("started") or "", data.get("session") or "", data.get("role") or ""]
    status = data.get("status")
    parts.append(labels["running"] if status == "running" else
                 labels["complete"] if status == PASS else labels["incomplete"])
    try:
        parts.append("%.1f s" % (float(data.get("duration_ms") or 0) / 1000.0))
    except (TypeError, ValueError):
        pass
    return [part for part in parts if part]


def _contents(chapters, numbers, labels):
    esc = html.escape
    out = ['<nav class="contents"><p class="kicker">%s</p><ol>' % esc(labels["contents"])]
    for chapter in chapters:
        titled = [segment for segment in chapter["segments"] if segment["title"]]
        if not titled:
            continue
        if chapter.get("title"):
            out.append('<li class="chapter">%s<ol>' % esc(chapter["title"]))
        for segment in titled:
            out.append('<li><a href="#page-%d">%s</a></li>'
                       % (numbers[id(segment)], esc(segment["title"])))
        if chapter.get("title"):
            out.append('</ol></li>')
    out.append('</ol></nav>')
    return "".join(out)


def _segment(segment, number, latest, labels):
    esc = html.escape
    out = ['<section class="page" id="page-%d">' % number]
    if segment["title"]:
        kicker = ('<span class="kicker">%s</span>' % esc(labels["dialog"])
                  if segment.get("dialog") else "")
        out.append('<h3>%s%s</h3>' % (kicker, esc(segment["title"])))
    items = segment["items"]
    boxes = segment.get("boxes") or {}
    if segment.get("shot"):
        callouts = [(item, boxes[str(item["id"])]) for item in items
                    if str(item["id"]) in boxes]
        out.append(_figure(segment["shot"], callouts, "overview", segment.get("ratio")))
    rendered = [_item(item, segment, latest, labels) for item in items]
    if rendered:
        out.append(rendered.pop(0))
    # The heading, the picture and the first step stay on one page: a heading
    # at the foot of a page, or a picture parted from what it shows, is how a
    # printed guide stops reading as one.
    out = [out[0], '<div class="lead">'] + out[1:] + ['</div>'] + rendered
    out.append('</section>')
    return "\n".join(out)


def _item(item, segment, latest, labels):
    esc = html.escape
    anchor = ' id="latest"' if item["id"] == latest else ""
    own = ""
    if (item.get("shot") and item["shot"] != segment.get("shot")
            and str(item["id"]) not in (segment.get("boxes") or {})
            and (item.get("box") or item["kind"] == "stopped")):
        own = _figure(item["shot"], [(item, item["box"])] if item.get("box") else [],
                      "own", item.get("ratio"))
    value = ('<code class="value">%s</code>' % esc(item["value"])
             if item.get("value") else "")
    if item["kind"] == "step":
        failed = " failed" if item.get("status") != PASS else ""
        return ('<div class="item step%s"%s><span class="n">%d</span>'
                '<div class="body"><p>%s%s</p>%s</div></div>'
                % (failed, anchor, item.get("n") or 0, esc(item["text"]),
                   (" " + value) if value else "", own))
    if item["kind"] == "result":
        return ('<div class="item result"%s><div class="body"><p class="kicker">%s</p>'
                '<p>%s</p>%s</div></div>'
                % (anchor, esc(labels["result"]), esc(item["text"]), own))
    message = ('<pre>%s</pre>' % esc(item["message"])) if item.get("message") else ""
    return ('<div class="item stopped"%s><div class="body"><p class="kicker">%s</p>'
            '%s%s</div></div>' % (anchor, esc(item["text"]), message, own))


def _figure(shot, callouts, size, ratio=None):
    """A picture with its callouts: a numbered box for each step that acts, in
    the order they act, joined by a path - and a dashed box, with no number and
    off the path, for what a check looked at. Finding an element is not a step
    the reader takes.

    With the picture's proportions known it is cut off below its lowest
    callout (CROP_MARGIN, at least CROP_LEAST of it), and the callouts are
    placed in what is left.
    """
    boxes = []
    for item, box in callouts:
        try:
            boxes.append((item, [float(part) for part in box][:4]))
        except (TypeError, ValueError):
            continue
    boxes = [(item, box) for item, box in boxes if len(box) == 4]
    crop = 1.0
    if ratio and boxes:
        lowest = max(box[1] + box[3] for _item, box in boxes)
        crop = min(1.0, max(CROP_LEAST, lowest + CROP_MARGIN))
    marks = [_sequence(boxes, crop)]
    # Steps on the same element - a list chosen in three times - share one box,
    # numbered "3, 4, 5", instead of three boxes drawn on top of each other.
    places = {}
    for item, box in boxes:
        place = (tuple(round(part, 4) for part in box), item["kind"] == "result")
        places.setdefault(place, []).append(item)
    for (box, check), items in places.items():
        x, y, w, h = box
        numbers = [str(item["n"]) for item in items if item.get("n")]
        badge = '<b>%s</b>' % ", ".join(numbers) if numbers else ""
        marks.append('<span class="box %s" style="left:%.2f%%;top:%.2f%%;'
                     'width:%.2f%%;height:%.2f%%">%s</span>'
                     % ("check" if check else "act", x * 100, y / crop * 100, w * 100,
                        h / crop * 100, badge))
    frame = (' cropped" style="aspect-ratio: 1000 / %d' % round(1000 * ratio * crop)
             if ratio else "")
    return ('<figure class="%s"><div class="shot%s"><div class="clip">'
            '<img src="%s" alt=""></div>%s</div></figure>'
            % (size, frame, html.escape(quote(shot)), "".join(marks)))


def _sequence(boxes, crop=1.0):
    """The path from one numbered callout to the next, drawn under them.

    Beside each badge - the middle of its box's left edge, which the badge
    stands just outside of, so as not to hide what is typed - in the picture's
    own coordinates (percent of it, both ways), so it scales with the picture;
    the stroke does not, so it stays a hairline.
    """
    points = ["%.2f,%.2f" % (box[0] * 100, (box[1] + box[3] / 2) / crop * 100)
              for item, box in sorted(boxes, key=lambda pair: pair[0].get("n") or 0)
              if item["kind"] == "step" and item.get("n")]
    if len(points) < 2:
        return ""
    return ('<svg class="sequence" viewBox="0 0 100 100" preserveAspectRatio="none">'
            '<polyline points="%s"/></svg>' % " ".join(points))


_CSS = """
@page { size: A4; margin: 14mm 14mm 16mm; }
:root {
  --ink: #1d1f20; --muted: #5d5d60; --line: #d4d4d7; --paper: #ffffff;
  --desk: #e7e7ea; --accent: #597ea3; --accent-ink: #2c455d;
  --accent-tint: #eef6ff; --bad: #a33a2e; --bad-tint: #f7e7e5;
  --body: 'Barlow', 'Inter', 'Noto Sans', 'DejaVu Sans', sans-serif;
  --heading: 'Barlow Condensed', 'Barlow', 'Noto Sans', 'DejaVu Sans Condensed',
             'DejaVu Sans', sans-serif;
  --mono: 'JetBrains Mono', 'Noto Sans Mono', 'DejaVu Sans Mono', monospace;
}
* { box-sizing: border-box; }
html { background: var(--desk); }
body { margin: 24px auto; max-width: 860px; padding: 40px 48px 56px;
       background: var(--paper); color: var(--ink);
       font: 14px/1.5 var(--body); border: 1px solid var(--line); }
header { margin-bottom: 20px; }
h1 { font: 600 30px/1.15 var(--heading); margin: 2px 0 10px; }
h2 { font: 600 21px/1.2 var(--heading); margin: 34px 0 8px; padding-bottom: 4px;
     border-bottom: 2px solid var(--ink); break-after: avoid; }
h3 { font: 600 17px/1.25 var(--heading); margin: 26px 0 8px; break-after: avoid; }
h3 .kicker { display: block; margin-bottom: 2px; }
.kicker { font: 600 11px/1.3 var(--heading); letter-spacing: .08em;
          text-transform: uppercase; color: var(--muted); margin: 0 0 3px; }
.lede { margin: 0 0 8px; }
.meta { margin: 0 0 10px; color: var(--muted); font-size: 12px; }
.banner { margin: 8px 0 0; padding: 6px 10px; border-left: 3px solid var(--accent);
          background: var(--accent-tint); font-size: 13px; }
.banner.bad { border-left-color: var(--bad); background: var(--bad-tint); }
nav.contents { margin: 0 0 8px; padding: 10px 14px; border: 1px solid var(--line); }
nav.contents ol { margin: 2px 0; padding-left: 20px; }
nav.contents li.chapter { font-weight: 600; }
nav.contents li li { font-weight: 400; }
nav.contents a { color: var(--accent-ink); text-decoration: none; }
figure { margin: 8px 0 12px; break-inside: avoid; }
.lead { break-inside: avoid; }
.shot { position: relative; display: inline-block; max-width: 100%;
        border: 1px solid var(--line); line-height: 0; }
.shot img { display: block; max-width: 100%; height: auto; }
.shot.cropped { display: block; width: 100%; }
.shot.cropped .clip { position: absolute; left: 0; top: 0; right: 0; bottom: 0;
                      overflow: hidden; }
.shot.cropped img { width: 100%; max-width: none; }
figure.own .shot { max-width: 72%; }
figure.own .shot.cropped { width: 72%; }
svg.sequence { position: absolute; left: 0; top: 0; width: 100%; height: 100%;
               overflow: visible; }
svg.sequence polyline { fill: none; stroke: var(--accent-ink); stroke-width: 1.5;
                        stroke-dasharray: 5 4; vector-effect: non-scaling-stroke;
                        opacity: .75; }
.box { position: absolute; border: 2px solid var(--accent);
       background: rgba(89, 126, 163, .12); }
.box.check { border-style: dashed; background: none; }
.box b { position: absolute; right: 100%; margin-right: 3px; top: 50%;
         transform: translateY(-50%);
         min-width: 20px; height: 20px; padding: 0 5px; background: var(--accent-ink);
         color: #fff; font: 600 12px/20px var(--body); text-align: center; }
.item { display: flex; gap: 10px; margin: 6px 0; break-inside: avoid; }
.item .n { flex: none; width: 22px; height: 22px; background: var(--accent-ink);
           color: #fff; font: 600 12px/22px var(--body); text-align: center; }
.item.failed .n { background: var(--bad); }
.item .body { flex: 1; min-width: 0; }
.item p { margin: 1px 0; }
.item.result, .item.stopped { padding: 6px 10px; border-left: 3px solid var(--accent);
                              background: var(--accent-tint); }
.item.stopped { border-left-color: var(--bad); background: var(--bad-tint); }
.item.stopped pre { margin: 4px 0; white-space: pre-wrap; font: 12px/1.4 var(--mono); }
code.value { padding: 0 5px; border: 1px solid var(--line); background: #f5f5f8;
             font: 12px var(--mono); white-space: pre-wrap; }
@media print {
  html { background: none; }
  body { margin: 0; max-width: none; padding: 0; border: none; font-size: 10.5pt; }
  a { color: inherit; }
}
"""
