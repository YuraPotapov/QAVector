"""``report.json`` and ``report.html`` - what a run looked like, as a file.

A report is a plugin rather than something the executor always does, which is
the design document's own arrangement and is right for a practical reason too:
a cycle that wants no report simply has no report node, and one that wants two
has two. The run's own ``metadata.json`` is written either way, so nothing is
lost by leaving them out.

Both read the same thing - the run record as it stands when the report step
runs - so a report node placed last describes everything before it. A report
node in the middle describes what has happened so far, which is occasionally
what somebody wants and is never a lie about the rest.

The HTML is written by hand, the way ``engine/flowfile.render`` writes YAML by
hand. A templating dependency for one page would be paid for by every install
and every frozen build, and the page is a table.
"""

import html
import json
import os

from cycle import registry
from cycle.registry import CyclePlugin, PluginMetadata, field, output
from domain.cycle import (Artifact, CANCELLED, FAILED, SKIPPED, SUCCESS,
                          TIMEOUT)

#: Status -> the colour it is painted and the word it is given. Deliberately
#: words as well as colours: colour alone is not something everyone can read,
#: which is the same rule the run page in the GUI follows.
LOOK = {
    SUCCESS: ("#1a7f37", "passed"),
    FAILED: ("#cf222e", "failed"),
    TIMEOUT: ("#bc4c00", "timed out"),
    CANCELLED: ("#57606a", "cancelled"),
    SKIPPED: ("#8250df", "skipped"),
    "pending": ("#8c959f", "never ran"),
    "running": ("#0969da", "still running"),
}


class _Report(CyclePlugin):
    """What both reports share: where to write, and what to write about."""

    def _target(self, context, step, default_name):
        name = str(self.setting(step.settings, "path") or default_name).strip()
        path = os.path.expanduser(name)
        if not os.path.isabs(path):
            path = os.path.join(context.workspace, "reports", path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        return path

    def _result(self, context, step, path, kind):
        size = os.path.getsize(path) if os.path.exists(path) else 0
        artifact = Artifact(kind, context.relative(path), step.id,
                            name=os.path.basename(path), bytes=size)
        return registry.PluginResult(
            SUCCESS, outputs={"path": context.relative(path)},
            artifacts=[artifact],
            message="wrote %s" % os.path.basename(path))


class ReportJson(_Report):
    """The whole run record as JSON, for something else to read."""

    metadata = PluginMetadata(
        id="report.json",
        name="JSON Report",
        category=registry.REPORT,
        summary="Write the run - every step, output and artifact - as JSON.",
        permissions=("filesystem.write",),
        inputs=(
            field("path", "File", "text", default="run.json",
                  hint="Where to write it. A relative path goes in the run's "
                       "reports directory."),
        ),
        outputs=(output("path", "string", "Where the report was written."),),
    )

    def execute(self, context, step):
        from cycle import run as run_mod

        path = self._target(context, step, "run.json")
        document = run_mod.to_document(context.run)
        # The step writing the report is itself still running, and saying so
        # would be true but useless in a finished report. It is named instead.
        document["generated_by"] = step.id
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, ensure_ascii=False,
                      default=str)
            handle.write("\n")
        return self._result(context, step, path, "json")


class ReportHtml(_Report):
    """One self-contained page: what ran, how it went, and what it produced."""

    metadata = PluginMetadata(
        id="report.html",
        name="HTML Report",
        category=registry.REPORT,
        summary="Write a readable page describing the run.",
        permissions=("filesystem.write",),
        inputs=(
            field("path", "File", "text", default="run.html",
                  hint="Where to write it. A relative path goes in the run's "
                       "reports directory."),
            field("title", "Title", "text",
                  hint="The heading. Blank uses the cycle's name."),
        ),
        outputs=(output("path", "string", "Where the report was written."),),
    )

    def execute(self, context, step):
        path = self._target(context, step, "run.html")
        title = (str(self.setting(step.settings, "title") or "").strip()
                 or context.run.cycle_name or context.run.cycle_id)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(_page(context.run, title, step.id))
        return self._result(context, step, path, "html")


# -- the page -----------------------------------------------------------------
def _page(run, title, generated_by):
    """The whole document. One string, no assets, nothing to fetch."""
    tally = run.tally()
    colour, word = LOOK.get(run.status, LOOK["pending"])
    return _TEMPLATE % {
        "title": html.escape(title),
        "status_colour": colour,
        "status_word": html.escape(word),
        "run_id": html.escape(run.id),
        "cycle": html.escape(run.cycle_id),
        "trigger": html.escape(run.trigger),
        "started": html.escape(_when(run.started_at)),
        "duration": html.escape(_duration(run.duration_ms)),
        "message": html.escape(run.message or ""),
        "passed": tally.get(SUCCESS, 0),
        "failed": tally.get(FAILED, 0) + tally.get(TIMEOUT, 0),
        "skipped": tally.get(SKIPPED, 0),
        "rows": _rows(run),
        "generated_by": html.escape(generated_by),
    }


def _rows(run):
    """One table row per step, in the order the steps were written."""
    out = []
    for step_id, step in run.steps.items():
        colour, word = LOOK.get(step.status, LOOK["pending"])
        out.append(
            '<tr><td class="id">%s</td><td class="plugin">%s</td>'
            '<td><span class="pill" style="background:%s">%s</span></td>'
            '<td class="num">%s</td><td class="num">%s</td><td>%s</td></tr>'
            % (html.escape(step_id), html.escape(step.plugin or ""), colour,
               html.escape(word), _duration(step.duration_ms),
               step.attempts or "", html.escape(step.message or "")))
        details = _details(step)
        if details:
            out.append('<tr class="details"><td></td><td colspan="5">%s</td></tr>'
                       % details)
    return "\n".join(out)


def _details(step):
    """Outputs and artifacts, when a step had any worth showing."""
    parts = []
    if step.outputs:
        parts.append("<dl>%s</dl>" % "".join(
            "<dt>%s</dt><dd>%s</dd>" % (html.escape(str(key)),
                                        html.escape(_short(value)))
            for key, value in sorted(step.outputs.items())))
    if step.artifacts:
        parts.append("<ul>%s</ul>" % "".join(
            '<li><a href="../%s">%s</a> <span class="dim">%s</span></li>'
            % (html.escape(artifact.path),
               html.escape(artifact.name or os.path.basename(artifact.path)),
               _bytes(artifact.bytes))
            for artifact in step.artifacts))
    return "".join(parts)


def _short(value):
    """A value as one line. A long output does not get to be the whole page."""
    text = str(value)
    if len(text) > 300:
        return text[:300] + "..."
    return text.replace("\n", " ")


def _bytes(count):
    if not count:
        return ""
    for unit in ("B", "KiB", "MiB", "GiB"):
        if count < 1024 or unit == "GiB":
            return "%.0f %s" % (count, unit) if unit == "B" else "%.1f %s" % (
                count, unit)
        count /= 1024.0
    return ""


def _duration(milliseconds):
    """A duration in the unit that fits, the way the run page already shows one."""
    if not milliseconds:
        return ""
    seconds = milliseconds / 1000.0
    if seconds < 1:
        return "%dms" % milliseconds
    if seconds < 60:
        return "%.1fs" % seconds
    minutes, seconds = divmod(int(seconds), 60)
    return "%dm %02ds" % (minutes, seconds)


def _when(epoch):
    if not epoch:
        return ""
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


#: Percent-formatted, so every brace in the CSS would have to be doubled under
#: str.format. Written as one string for the reason the module docstring gives.
_TEMPLATE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<title>%(title)s - run %(run_id)s</title>
<style>
  :root { color-scheme: light dark; }
  body { margin: 0; padding: 32px;
         font: 14px/1.5 -apple-system, "Segoe UI", system-ui, sans-serif;
         background: #ffffff; color: #1f2328; }
  h1 { margin: 0 0 4px; font-size: 22px; }
  .sub { color: #656d76; margin-bottom: 24px; font-size: 13px; }
  .pill { color: #fff; border-radius: 999px; padding: 2px 10px;
          font-size: 12px; white-space: nowrap; }
  .facts { display: flex; flex-wrap: wrap; gap: 24px; margin-bottom: 24px;
           padding: 16px; border: 1px solid #d1d9e0; border-radius: 8px; }
  .fact b { display: block; font-size: 11px; text-transform: uppercase;
            letter-spacing: .04em; color: #656d76; font-weight: 600; }
  table { border-collapse: collapse; width: 100%%; }
  th { text-align: left; font-size: 11px; text-transform: uppercase;
       letter-spacing: .04em; color: #656d76; padding: 8px 12px;
       border-bottom: 1px solid #d1d9e0; }
  td { padding: 8px 12px; border-bottom: 1px solid #eaeef2;
       vertical-align: top; }
  td.id { font-weight: 600; }
  td.plugin, td.num { font-family: ui-monospace, "SF Mono", Menlo, monospace;
                      font-size: 12px; color: #656d76; }
  td.num { text-align: right; }
  tr.details td { padding-top: 0; border-bottom: 1px solid #eaeef2; }
  dl { margin: 0 0 8px; display: grid; grid-template-columns: max-content 1fr;
       gap: 2px 12px; font-size: 12px; }
  dt { font-family: ui-monospace, Menlo, monospace; color: #656d76; }
  dd { margin: 0; font-family: ui-monospace, Menlo, monospace;
       overflow-wrap: anywhere; }
  ul { margin: 0; padding-left: 18px; font-size: 12px; }
  .dim { color: #656d76; }
  footer { margin-top: 32px; color: #656d76; font-size: 12px; }
  @media (prefers-color-scheme: dark) {
    body { background: #0d1117; color: #e6edf3; }
    .facts, th { border-color: #30363d; }
    td, tr.details td { border-color: #21262d; }
    .fact b, td.plugin, td.num, dt, .dim, .sub, footer, th { color: #9198a1; }
  }
</style>
<h1>%(title)s</h1>
<div class="sub">run %(run_id)s</div>

<div class="facts">
  <div class="fact"><b>Result</b>
    <span class="pill" style="background:%(status_colour)s">%(status_word)s</span>
  </div>
  <div class="fact"><b>Cycle</b>%(cycle)s</div>
  <div class="fact"><b>Trigger</b>%(trigger)s</div>
  <div class="fact"><b>Started</b>%(started)s</div>
  <div class="fact"><b>Took</b>%(duration)s</div>
  <div class="fact"><b>Steps</b>%(passed)s passed, %(failed)s failed,
    %(skipped)s skipped</div>
</div>

<table>
  <tr><th>Step</th><th>Plugin</th><th>Result</th><th>Took</th>
      <th>Tries</th><th>Notes</th></tr>
%(rows)s
</table>

<footer>%(message)s<br>Written by the %(generated_by)s step.</footer>
</html>
"""
