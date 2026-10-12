"""Unit tests for configurable reporting (no browser needed).

Covers :class:`ReportConfig` parsing/validation and its per-outcome artifact
decisions, plus :class:`Reporter` writing the right files against a fake adapter
and a temp directory. The scenarios mirror the examples in the feature spec.
"""

import os

import pytest

from domain.result import FlowResult
from engine.artifacts import ARTIFACTS, ReportConfig, Reporter


class FakeAdapter:
    """Records screenshots and serves canned page state for the diagnostics."""

    def __init__(self):
        self.shots = []

    def screenshot(self, path):
        self.shots.append(os.path.basename(path))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("PNG")

    def content(self):
        return "<html></html>"

    def console_logs(self):
        return ["one", "two"]

    def url(self):
        return "http://localhost:8069/web"


def _files(tmp_path):
    return set(os.listdir(tmp_path))


def _flow():
    return FlowResult(scenario="s", session="sess")


# --- ReportConfig: parsing & validation -------------------------------------
def test_default_config_is_legacy():
    cfg = ReportConfig()
    assert cfg.configured is False
    assert cfg.effective_level == frozenset(ARTIFACTS)
    assert cfg.screen_modes == frozenset()          # legacy never uses the modes


def test_from_cli_marks_configured_for_each_flag():
    assert ReportConfig.from_cli(level="result").configured is True
    assert ReportConfig.from_cli(always=True).configured is True
    assert ReportConfig.from_cli(screen="start").configured is True


def test_unknown_artifact_raises():
    with pytest.raises(ValueError) as exc:
        ReportConfig.from_cli(level="result,bogus")
    assert "bogus" in str(exc.value)
    assert "report-level artifact" in str(exc.value)


def test_unknown_screen_mode_message_matches_spec():
    with pytest.raises(ValueError) as exc:
        ReportConfig.from_cli(level="screen", screen="start,middle")
    msg = str(exc.value)
    assert "Unknown report-screen mode 'middle'." in msg
    assert "Supported values: start, each, finish." in msg


def test_duplicate_modes_are_deduped():
    cfg = ReportConfig.from_cli(level="screen", screen="start,start,finish")
    assert cfg.screen == frozenset({"start", "finish"})


def test_screen_modes_default_to_finish_when_enabled():
    cfg = ReportConfig.from_cli(level="console,dom,result,screen,url")
    assert cfg.screen_modes == frozenset({"finish"})


def test_screen_ignored_when_not_in_level():
    cfg = ReportConfig.from_cli(level="result", screen="start,each,finish")
    assert cfg.screen_enabled is False
    assert cfg.screen_modes == frozenset()


# --- ReportConfig: which non-screenshot artifacts per outcome ---------------
def test_outcome_artifacts_default():
    cfg = ReportConfig()
    assert cfg.outcome_artifacts(failed=False) == {"result"}
    # server_log is absent on purpose: it follows the level it was asked for in,
    # not the outcome, because it records the whole scenario rather than snapshots
    # where the page ended up. See test_a_passing_scenario_still_keeps_it.
    assert cfg.outcome_artifacts(failed=True) == {"console", "dom", "result", "url"}


def test_outcome_artifacts_level_filters_both_outcomes():
    cfg = ReportConfig.from_cli(level="result")
    assert cfg.outcome_artifacts(failed=False) == {"result"}
    assert cfg.outcome_artifacts(failed=True) == {"result"}


def test_outcome_artifacts_always_promotes_success():
    cfg = ReportConfig.from_cli(level="console,result", always=True)
    assert cfg.outcome_artifacts(failed=False) == {"console", "result"}


# --- Reporter: legacy behaviour (backward compatibility) --------------------
def test_legacy_success_writes_only_result(tmp_path):
    r = Reporter(ReportConfig(), str(tmp_path), FakeAdapter())
    r.capture_start()
    r.capture_step()
    r.finalize(_flow(), failed=False)
    assert _files(tmp_path) == {"result.json"}


def test_legacy_failure_writes_full_bundle(tmp_path):
    r = Reporter(ReportConfig(), str(tmp_path), FakeAdapter())
    r.finalize(_flow(), failed=True)
    assert _files(tmp_path) == {"console.log", "dom.html", "result.json",
                                "screenshot.png", "url.txt"}


def test_legacy_compile_error_writes_result_only(tmp_path):
    r = Reporter(ReportConfig(), str(tmp_path), FakeAdapter())
    r.finalize_compile_error(_flow())
    assert _files(tmp_path) == {"result.json"}


# --- Reporter: configured behaviour (spec examples) -------------------------
def test_level_result_only_even_on_failure(tmp_path):
    r = Reporter(ReportConfig.from_cli(level="result"), str(tmp_path), FakeAdapter())
    r.capture_start()
    r.capture_step()
    r.finalize(_flow(), failed=True)
    assert _files(tmp_path) == {"result.json"}


def test_screen_each_numbers_screenshots(tmp_path):
    # Spec example 1: --report-level=screen --report-screen=each
    r = Reporter(ReportConfig.from_cli(level="screen", screen="each"),
                 str(tmp_path), FakeAdapter())
    r.capture_start()                       # start not requested -> nothing
    r.capture_step()
    r.capture_step()
    r.capture_step()
    r.finalize(_flow(), failed=False)       # finish not requested, result not in level
    assert _files(tmp_path) == {"screenshot_001.png", "screenshot_002.png",
                                "screenshot_003.png"}


def test_complete_report_on_failure_uses_finish_name(tmp_path):
    # Spec "Complete report": all artifacts, default screen point = finish
    r = Reporter(ReportConfig.from_cli(level="console,dom,result,screen,url"),
                 str(tmp_path), FakeAdapter())
    r.capture_start()
    r.finalize(_flow(), failed=True)
    assert _files(tmp_path) == {"console.log", "dom.html", "result.json",
                                "screenshot_finish.png", "url.txt"}


def test_always_writes_console_and_result_on_success(tmp_path):
    # Spec --report-always example: --report-always --report-level=console,result
    r = Reporter(ReportConfig.from_cli(level="console,result", always=True),
                 str(tmp_path), FakeAdapter())
    r.finalize(_flow(), failed=False)
    assert _files(tmp_path) == {"console.log", "result.json"}


def test_start_and_finish_with_result(tmp_path):
    # Spec example 2 (success side): result,screen + always + start,finish
    cfg = ReportConfig.from_cli(level="result,screen", always=True, screen="start,finish")
    r = Reporter(cfg, str(tmp_path), FakeAdapter())
    r.capture_start()
    r.capture_step()                        # each not requested -> nothing
    r.finalize(_flow(), failed=False)
    assert _files(tmp_path) == {"result.json", "screenshot_start.png",
                                "screenshot_finish.png"}


def test_screen_requested_but_not_in_level_captures_nothing(tmp_path):
    # Spec example 3: --report-level=result --report-screen=start,each,finish
    cfg = ReportConfig.from_cli(level="result", screen="start,each,finish")
    r = Reporter(cfg, str(tmp_path), FakeAdapter())
    r.capture_start()
    r.capture_step()
    r.finalize(_flow(), failed=False)
    assert _files(tmp_path) == {"result.json"}


def test_configured_compile_error_respects_level(tmp_path):
    r = Reporter(ReportConfig.from_cli(level="console"), str(tmp_path), FakeAdapter())
    r.finalize_compile_error(_flow())       # result not in level -> nothing written
    assert _files(tmp_path) == set()


# --- the server_log artifact -------------------------------------------------
# Not a --report-level choice. Everything in ARTIFACTS is captured from the
# browser; a backend log is not, and --server-log is its switch. If the run was
# streaming one, the scenario keeps its slice - pass or fail, flags or no flags.
def _server(**logs):
    """A provider in the shape the runner hands over: {log name: [lines]}."""
    return lambda: dict(logs)


def test_server_log_writes_one_file_per_log(tmp_path):
    # A stand usually has several (app, nginx, journal); one merged file would
    # interleave them into something nobody can read.
    r = Reporter(ReportConfig.from_cli(level="result"), str(tmp_path),
                 FakeAdapter(), server=_server(app=["boom"], nginx=["upstream"]))
    r.finalize(_flow(), failed=True)
    assert _files(tmp_path) == {"result.json", "server_log-app.log",
                                "server_log-nginx.log"}
    assert (tmp_path / "server_log-app.log").read_text() == "boom"


def test_a_log_with_nothing_in_the_window_writes_no_file(tmp_path):
    # An empty file reads as "the server said nothing", which is a claim; no file
    # reads as "nothing to show here", which is the truth.
    r = Reporter(ReportConfig.from_cli(level="result"), str(tmp_path),
                 FakeAdapter(), server=_server(app=["kept"], nginx=[]))
    r.finalize(_flow(), failed=True)
    assert _files(tmp_path) == {"result.json", "server_log-app.log"}


def test_nothing_is_written_without_a_provider(tmp_path):
    # No --server-log: there is no slice to keep, and no empty file pretending.
    r = Reporter(ReportConfig.from_cli(level="result"), str(tmp_path), FakeAdapter())
    r.finalize(_flow(), failed=True)
    assert _files(tmp_path) == {"result.json"}


def test_a_narrow_report_level_does_not_suppress_it(tmp_path):
    """--report-level is about the browser's artifacts, and never gates this one.

    Gating it a second time only creates a way to stream a backend's log all run
    and then throw the evidence away.
    """
    r = Reporter(ReportConfig.from_cli(level="screen", screen="each"), str(tmp_path),
                 FakeAdapter(), server=_server(app=["boom"]))
    r.capture_step()
    r.finalize(_flow(), failed=True)
    assert _files(tmp_path) == {"screenshot_001.png", "server_log-app.log"}


def test_a_passing_scenario_keeps_it_too(tmp_path):
    r = Reporter(ReportConfig.from_cli(level="result"), str(tmp_path),
                 FakeAdapter(), server=_server(app=["chatter"]))
    r.finalize(_flow(), failed=False)
    assert _files(tmp_path) == {"result.json", "server_log-app.log"}


def test_the_legacy_path_keeps_it_on_success_as_well(tmp_path):
    # No --report-* flag at all is the commonest way to run, and the one where
    # "logs are configured, so save them" has to hold just the same.
    r = Reporter(ReportConfig(), str(tmp_path), FakeAdapter(),
                 server=_server(app=["chatter"]))
    r.finalize(_flow(), failed=False)
    assert _files(tmp_path) == {"result.json", "server_log-app.log"}


def test_the_legacy_failure_bundle_includes_it(tmp_path):
    r = Reporter(ReportConfig(), str(tmp_path), FakeAdapter(),
                 server=_server(app=["Traceback (most recent call last):"]))
    r.finalize(_flow(), failed=True)
    assert "server_log-app.log" in _files(tmp_path)


def test_a_compile_error_still_keeps_what_the_backend_said(tmp_path):
    # A scenario that never compiled is exactly when the backend's own complaint
    # is worth reading.
    r = Reporter(ReportConfig(), str(tmp_path), FakeAdapter(),
                 server=_server(app=["ValueError: bad view"]))
    r.finalize_compile_error(_flow())
    assert "server_log-app.log" in _files(tmp_path)


def test_a_provider_that_raises_does_not_lose_the_rest_of_the_report(tmp_path):
    # The hub reads over ssh; a connection can die between the run and the write.
    def broken():
        raise OSError("ssh: connection closed")

    r = Reporter(ReportConfig.from_cli(level="result"), str(tmp_path),
                 FakeAdapter(), server=broken)
    r.finalize(_flow(), failed=True)
    assert _files(tmp_path) == {"result.json"}


def test_server_log_is_not_a_report_level_value():
    # It was, briefly. Keeping it would mean two switches for one decision.
    assert "server_log" not in ARTIFACTS
    with pytest.raises(ValueError):
        ReportConfig.from_cli(level="server_log")


def test_written_server_logs_are_announced_like_every_other_artifact(tmp_path,
                                                                     monkeypatch):
    # The GUI's Artifacts page builds its tree from this event, not from a guess
    # at the file names.
    from engine import artifacts as artifacts_mod

    seen = {}
    monkeypatch.setattr(artifacts_mod.events, "emit_artifacts",
                        lambda out_dir, **kw: seen.update(dir=out_dir))
    r = Reporter(ReportConfig.from_cli(level="result"), str(tmp_path),
                 FakeAdapter(), server=_server(app=["boom"]))
    r.finalize(_flow(), failed=True)
    assert seen["dir"] == str(tmp_path)


# --- the page settles before a step's screenshot -------------------------------
class SettlingAdapter(FakeAdapter):
    """Says when it was asked to settle, in the order of everything else."""

    def __init__(self):
        FakeAdapter.__init__(self)
        self.calls = []

    def settle(self, timeout_ms=3000):
        self.calls.append("settle")

    def screenshot(self, path):
        self.calls.append(os.path.basename(path))
        FakeAdapter.screenshot(self, path)


def test_a_step_screenshot_waits_for_the_page_and_is_named_after_the_step(tmp_path):
    """Right after a click Odoo still showed the page it left, or half the new
    one; and the file's number is the step's, so a viewer can pair them."""
    adapter = SettlingAdapter()
    r = Reporter(ReportConfig.from_cli(level="screen", screen="start,each,finish"),
                 str(tmp_path), adapter)
    r.capture_start()
    r.capture_step(0)
    r.capture_step(4)
    r.finalize(_flow(), failed=False)
    assert adapter.calls == ["screenshot_start.png", "settle", "screenshot_001.png",
                             "settle", "screenshot_005.png", "settle",
                             "screenshot_finish.png"]


def test_nothing_waits_when_no_screenshot_is_taken(tmp_path):
    adapter = SettlingAdapter()
    r = Reporter(ReportConfig.from_cli(level="result"), str(tmp_path), adapter)
    r.capture_step(0)
    r.finalize(_flow(), failed=False)
    assert "settle" not in adapter.calls


def test_an_adapter_without_settle_still_gets_its_screenshots(tmp_path):
    r = Reporter(ReportConfig.from_cli(level="screen", screen="each"),
                 str(tmp_path), FakeAdapter())
    r.capture_step(2)
    assert _files(tmp_path) == {"screenshot_003.png"}
