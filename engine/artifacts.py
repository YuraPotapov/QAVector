"""Reporting / observability: per-run report tree and configurable artifacts.

Layout:  ``reports/<timestamp>/<session>/<scenario>/`` containing the artifacts
selected for the run. By default a scenario writes ``result.json`` on success and
the full diagnostic bundle (``console.log`` / ``dom.html`` / ``result.json`` /
``screenshot.png`` / ``url.txt``) on failure - the original behaviour, left
unchanged whenever no ``--report-*`` flag is given.

The ``--report-level`` / ``--report-always`` / ``--report-screen`` flags configure
*what* artifacts are produced, *when* a report is produced, and *when* screenshots
are captured. :class:`ReportConfig` holds the parsed/validated choices and answers
"which artifacts for this outcome?"; :class:`Reporter` applies them against one
scenario's output directory. Every capture is best-effort - a diagnostics failure
must never mask the real result.
"""

import json
import logging
import os
import time
from dataclasses import asdict, dataclass

from engine import events

log = logging.getLogger("flowengine.artifacts")

# Report artifacts selectable via --report-level, and the screenshot capture
# points selectable via --report-screen. Plain tuples so the launcher can mirror
# them for validation without importing anything heavy (see engine.overlay note).
ARTIFACTS = ("console", "dom", "result", "screen", "url")
SCREEN_MODES = ("start", "each", "finish")

# The non-screenshot artifacts captured from live page state on a full report.
_DIAGNOSTIC = ("console", "dom", "url")

# Backend logs are deliberately NOT one of ARTIFACTS. Everything in that tuple is
# captured from the browser, and --report-level decides which. A server log is not
# the browser's, and it has its own switch: --server-log turns the streaming on,
# and if it is on, the run keeps the file. Gating it a second time behind a report
# level only creates a way to stream lines all run and then throw them away.

# One file per log, because a stand usually has several (app, nginx, journal) and
# a single file would interleave them into something nobody can read.
_SERVER_FILE = "server_log-%s.log"


def new_run_dir(reports_dir):
    path = os.path.join(reports_dir, time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(path, exist_ok=True)
    return path


def scenario_dir(run_dir, session, scenario):
    path = os.path.join(run_dir, session, scenario)
    os.makedirs(path, exist_ok=True)
    return path


def write_result(flow_result, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as fh:
        json.dump(asdict(flow_result), fh, indent=2, ensure_ascii=False)


def capture_failure(adapter, out_dir):
    """Best-effort screenshot + DOM + console + URL for a failed scenario.

    The legacy on-failure bundle: a ``screenshot.png`` plus the diagnostic files.
    Used only when no ``--report-*`` flag is given (see :class:`Reporter`).
    """
    os.makedirs(out_dir, exist_ok=True)
    _safe(lambda: adapter.screenshot(os.path.join(out_dir, "screenshot.png")), "screenshot")
    _safe(lambda: _write(os.path.join(out_dir, "dom.html"), adapter.content()), "dom")
    _safe(lambda: _write(os.path.join(out_dir, "console.log"),
                         "\n".join(adapter.console_logs())), "console")
    _safe(lambda: _write(os.path.join(out_dir, "url.txt"), adapter.url()), "url")


def write_server_logs(server, out_dir):
    """One ``server_log-<name>.log`` per backend log that had lines in this window.

    ``server`` is a zero-argument callable answering ``{log name: [lines]}`` - the
    same shape as ``adapter.console_logs()``, and for the same reason: the reporter
    asks for the text when it is ready to write it, and knows nothing about how it
    was collected. ``None`` (no --server-log) writes nothing.
    """
    if server is None:
        return
    def _collect():
        for name, lines in (server() or {}).items():
            if lines:
                _write(os.path.join(out_dir, _SERVER_FILE % name), "\n".join(lines))
    _safe(_collect, "server")


# --- configurable reporting --------------------------------------------------

@dataclass(frozen=True)
class ReportConfig:
    """Parsed ``--report-*`` choices; a default instance means legacy behaviour.

    ``level`` / ``screen`` are ``None`` when the corresponding flag was absent (so
    the defaults apply) or a :class:`frozenset` of the selected values otherwise.
    ``configured`` is ``True`` as soon as *any* report flag is given, which is what
    switches on the new artifact naming and success/always semantics.
    """

    level: frozenset = None       # None => default artifact set (all of ARTIFACTS)
    always: bool = False          # --report-always: full report on success too
    screen: frozenset = None      # None => default capture point (finish)
    configured: bool = False      # any --report-* flag was given

    @classmethod
    def from_cli(cls, level=None, always=False, screen=None):
        """Build from raw CLI strings, validating artifact names / screen modes.

        ``level`` / ``screen`` are the raw comma-separated strings (or ``None`` when
        the flag was absent). Duplicates collapse silently; an unknown artifact or
        mode raises :class:`ValueError` with a user-facing message.
        """
        configured = level is not None or bool(always) or screen is not None
        level_set = None if level is None else _parse_choices(
            level, ARTIFACTS, "report-level", "artifact")
        screen_set = None if screen is None else _parse_choices(
            screen, SCREEN_MODES, "report-screen", "mode")
        return cls(level=level_set, always=bool(always), screen=screen_set,
                   configured=configured)

    @property
    def effective_level(self):
        """Artifact set in force: the explicit ``--report-level`` or the full set."""
        return self.level if self.level is not None else frozenset(ARTIFACTS)

    @property
    def screen_enabled(self):
        """Whether screenshots are permitted at all (``screen`` in the level)."""
        return "screen" in self.effective_level

    @property
    def screen_modes(self):
        """Screenshot capture points in force; empty when screenshots are off.

        Legacy runs never use this machinery (they capture a single on-failure
        ``screenshot.png`` via :func:`capture_failure`), so it is empty unless a
        report flag was given. When enabled but ``--report-screen`` was omitted the
        default capture point is ``finish``.
        """
        if not self.configured or not self.screen_enabled:
            return frozenset()
        return self.screen if self.screen is not None else frozenset({"finish"})

    def wants_screen(self, mode):
        return mode in self.screen_modes

    def outcome_artifacts(self, failed):
        """Non-screenshot artifacts to write given the run outcome.

        A failure - or ``--report-always`` - yields the full diagnostic set; a plain
        success yields just ``result`` (the original behaviour). The chosen set is
        then intersected with ``--report-level`` so nothing outside it is created.
        """
        base = {"result", *_DIAGNOSTIC} if (failed or self.always) else {"result"}
        return {name for name in base if name in self.effective_level}


def _parse_choices(raw, allowed, kind, noun):
    """Split a comma list, validate against ``allowed``, return a de-duped frozenset.

    Raises ``ValueError`` naming the unknown value(s); the message mirrors the spec,
    e.g. ``Unknown report-screen mode 'middle'.\nSupported values: start, each, finish.``
    """
    items = [p.strip() for p in raw.split(",") if p.strip()]
    bad = [it for it in items if it not in allowed]
    if bad:
        label = noun if len(bad) == 1 else noun + "s"
        names = ", ".join("'%s'" % b for b in bad)
        raise ValueError("Unknown %s %s %s.\nSupported values: %s."
                         % (kind, label, names, ", ".join(allowed)))
    return frozenset(items)


class Reporter:
    """Applies a :class:`ReportConfig` to one scenario's output directory.

    The runner calls :meth:`capture_start` before the first step, :meth:`capture_step`
    after each step that passes, and :meth:`finalize` once the flow ends (which also
    takes the finish screenshot). In legacy mode (no ``--report-*`` flags) it
    reproduces the original behaviour exactly: ``result.json`` always, the full
    diagnostic bundle with a ``screenshot.png`` on failure, nothing extra on success.
    """

    def __init__(self, config, out_dir, adapter, server=None):
        self._cfg = config
        self._out = out_dir
        self._adapter = adapter
        # Zero-argument callable -> {log name: [lines]}, or None when --server-log
        # is off. Same contract as adapter.console_logs(): asked for at write time,
        # so the reporter never learns where the lines came from.
        self._server = server
        self._each_n = 0

    def capture_start(self):
        """Screenshot immediately before the first step (``--report-screen=start``)."""
        if self._cfg.wants_screen("start"):
            self._shot("screenshot_start.png")

    def capture_step(self, index=None):
        """Screenshot after a step passes (``--report-screen=each``).

        Named after the step - ``screenshot_004.png`` is step index 3 - so a
        viewer can put each picture beside the step it shows. Taken once the
        page has settled: right after a click it still showed the page the click
        left, or one half drawn.
        """
        if self._cfg.wants_screen("each"):
            self._each_n = self._each_n + 1 if index is None else index + 1
            self._settle()
            self._shot("screenshot_%03d.png" % self._each_n)

    def finalize(self, flow_result, failed):
        """Write the finish screenshot and the outcome's report artifacts."""
        try:
            self._finalize(flow_result, failed)
            # Unconditional, and outside the level: if the run was streaming a
            # backend log, the scenario keeps its slice of it - pass or fail, with
            # or without --report-* flags. Turning the streaming on is the whole
            # decision. No provider (no --server-log) writes nothing.
            write_server_logs(self._server, self._out)
        finally:
            # Announce what actually landed on disk, whichever branch ran, so a
            # consumer never has to guess the artifact names from the config.
            events.emit_artifacts(self._out, scenario=flow_result.scenario,
                                  session=flow_result.session)

    def _finalize(self, flow_result, failed):
        if not self._cfg.configured:
            self._finalize_legacy(flow_result, failed)
            return
        if self._cfg.wants_screen("finish"):
            self._settle()
            self._shot("screenshot_finish.png")
        wanted = self._cfg.outcome_artifacts(failed)
        if "result" in wanted:
            write_result(flow_result, self._out)
        if "console" in wanted:
            _safe(lambda: _write(os.path.join(self._out, "console.log"),
                                 "\n".join(self._adapter.console_logs())), "console")
        if "dom" in wanted:
            _safe(lambda: _write(os.path.join(self._out, "dom.html"),
                                 self._adapter.content()), "dom")
        if "url" in wanted:
            _safe(lambda: _write(os.path.join(self._out, "url.txt"),
                                 self._adapter.url()), "url")


    def finalize_compile_error(self, flow_result):
        """Compile-time failure: only ``result.json`` (dropped if not in the level).

        Kept minimal on purpose - no steps ran and there is no meaningful page state
        to capture - which also preserves the original compile-error behaviour.
        """
        if not self._cfg.configured or "result" in self._cfg.effective_level:
            write_result(flow_result, self._out)
        write_server_logs(self._server, self._out)
        events.emit_artifacts(self._out, scenario=flow_result.scenario,
                              session=flow_result.session)

    def _finalize_legacy(self, flow_result, failed):
        if failed:
            capture_failure(self._adapter, self._out)
        write_result(flow_result, self._out)

    def _settle(self):
        """Let the page finish what the last step set off - see adapter.settle.
        An adapter without it (a test double, an older one) does not wait."""
        settle = getattr(self._adapter, "settle", None)
        if settle is not None:
            _safe(settle, "settle")

    def _shot(self, name):
        _safe(lambda: self._adapter.screenshot(os.path.join(self._out, name)),
              "screenshot %s" % name)


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text or "")


def _safe(fn, what):
    try:
        fn()
    except Exception as exc:  # diagnostics must never break the run
        log.debug("could not capture %s: %s", what, exc)
