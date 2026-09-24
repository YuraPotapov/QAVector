"""report.json and report.html: what a finished run looks like as a file."""

import json
import os

import pytest

from cycle import registry, workspace
from cycle.context import RunContext
from cycle.plugins.report import ReportHtml, ReportJson, _duration
from domain.cycle import (Artifact, CycleRun, CycleStep, StepRun, FAILED,
                          SKIPPED, SUCCESS, TIMEOUT)


@pytest.fixture
def context(tmp_path):
    path = workspace.create("20260916-120000-demo", str(tmp_path))
    run = CycleRun(id="20260916-120000-demo", cycle_id="demo",
                   cycle_name="Nightly validation", status=FAILED,
                   started_at=1789000000.0, duration_ms=95000.0,
                   workspace=path, message="tests failed")
    run.steps = {
        "build": StepRun("build", plugin="command.shell", status=SUCCESS,
                         attempts=1, duration_ms=1200.0, message="exited 0",
                         outputs={"exit_code": 0, "stdout_tail": "done"},
                         artifacts=[Artifact("log", "steps/build/stdout.log",
                                             "build", name="stdout",
                                             bytes=2048)]),
        "tests": StepRun("tests", plugin="command.shell", status=FAILED,
                         attempts=2, duration_ms=90000.0, message="exited 1"),
        "deploy": StepRun("deploy", plugin="command.shell", status=SKIPPED,
                          message="tests failed"),
    }
    return RunContext(run, None, path)


def step(plugin_id, **settings):
    return CycleStep(id="report", plugin=plugin_id, settings=settings)


# --------------------------------------------------------------------- json
def test_the_json_report_is_written_into_the_reports_directory(context):
    result = ReportJson().execute(context, step("report.json"))

    assert result.ok
    assert result.outputs["path"] == os.path.join("reports", "run.json")
    assert os.path.exists(os.path.join(context.workspace, "reports", "run.json"))


def test_the_json_report_is_the_run_record(context):
    ReportJson().execute(context, step("report.json"))
    with open(os.path.join(context.workspace, "reports", "run.json"),
              encoding="utf-8") as handle:
        document = json.load(handle)

    assert document["id"] == "20260916-120000-demo"
    assert document["status"] == FAILED
    assert set(document["steps"]) == {"build", "tests", "deploy"}
    assert document["tally"][SUCCESS] == 1


def test_the_json_report_names_the_step_that_wrote_it(context):
    """Saying the report step is "still running" would be true but useless."""
    ReportJson().execute(context, step("report.json"))
    with open(os.path.join(context.workspace, "reports", "run.json"),
              encoding="utf-8") as handle:
        assert json.load(handle)["generated_by"] == "report"


def test_the_report_becomes_an_artifact_of_the_step_that_wrote_it(context):
    result = ReportJson().execute(context, step("report.json"))
    artifact = result.artifacts[0]

    assert artifact.type == "json"
    assert artifact.producer == "report"
    assert artifact.name == "run.json"
    assert artifact.bytes > 0
    assert not os.path.isabs(artifact.path)


def test_the_file_can_be_given_another_name(context):
    result = ReportJson().execute(context, step("report.json", path="summary.json"))
    assert result.outputs["path"].endswith("summary.json")


def test_an_absolute_path_is_written_where_it_says(context, tmp_path):
    target = tmp_path / "elsewhere" / "run.json"
    result = ReportJson().execute(context, step("report.json", path=str(target)))
    assert result.ok
    assert target.exists()


# ---------------------------------------------------------------------- html
def test_the_html_report_is_written_and_is_a_whole_page(context):
    result = ReportHtml().execute(context, step("report.html"))
    text = _read(context, "run.html")

    assert result.ok
    assert text.startswith("<!doctype html>")
    assert "</html>" in text


def test_the_page_carries_nothing_it_would_have_to_fetch(context):
    """It is read from a run directory that may have been moved or zipped."""
    ReportHtml().execute(context, step("report.html"))
    text = _read(context, "run.html")

    assert "http://" not in text and "https://" not in text
    assert "<script" not in text


def test_the_page_is_headed_with_the_cycle_s_name(context):
    ReportHtml().execute(context, step("report.html"))
    assert "Nightly validation" in _read(context, "run.html")


def test_the_heading_can_be_overridden(context):
    ReportHtml().execute(context, step("report.html", title="Release 2.1"))
    assert "Release 2.1" in _read(context, "run.html")


def test_the_page_names_every_step_and_how_it_went(context):
    ReportHtml().execute(context, step("report.html"))
    text = _read(context, "run.html")

    for step_id in ("build", "tests", "deploy"):
        assert step_id in text
    assert "passed" in text and "failed" in text and "skipped" in text


def test_a_status_is_said_in_words_as_well_as_colour(context):
    """Colour alone is not something everyone can read."""
    ReportHtml().execute(context, step("report.html"))
    text = _read(context, "run.html")
    assert ">passed<" in text and ">failed<" in text


def test_the_page_links_to_the_artifacts_a_step_produced(context):
    ReportHtml().execute(context, step("report.html"))
    text = _read(context, "run.html")
    assert "steps/build/stdout.log" in text
    assert "2.0 KiB" in text


def test_the_page_shows_what_a_step_produced(context):
    ReportHtml().execute(context, step("report.html"))
    assert "exit_code" in _read(context, "run.html")


def test_a_long_output_does_not_become_the_whole_page(context):
    context.run.steps["build"].outputs["stdout_tail"] = "x" * 5000
    ReportHtml().execute(context, step("report.html"))
    assert len(_read(context, "run.html")) < 20000


def test_anything_that_came_from_a_command_is_escaped(context):
    """Output is arbitrary text from a process; it is data, never markup."""
    context.run.steps["tests"].message = "<script>alert('x')</script>"
    ReportHtml().execute(context, step("report.html"))
    text = _read(context, "run.html")

    assert "<script>alert" not in text
    assert "&lt;script&gt;" in text


def test_a_cycle_name_with_markup_in_it_is_escaped_too(context):
    context.run.cycle_name = "<b>bold</b>"
    ReportHtml().execute(context, step("report.html"))
    assert "<b>bold</b>" not in _read(context, "run.html")


def test_the_page_works_for_a_run_where_nothing_happened(context):
    context.run.steps = {}
    result = ReportHtml().execute(context, step("report.html"))
    assert result.ok


def test_every_status_a_step_can_wear_has_something_to_say(context):
    for status in (SUCCESS, FAILED, TIMEOUT, SKIPPED, "cancelled", "pending",
                   "running"):
        context.run.steps["tests"].status = status
        assert ReportHtml().execute(context, step("report.html")).ok


# --------------------------------------------------------------------- shared
def test_a_duration_is_shown_in_the_unit_that_fits():
    assert _duration(0) == ""
    assert _duration(250) == "250ms"
    assert _duration(1500) == "1.5s"
    assert _duration(95000) == "1m 35s"
    assert _duration(3725000) == "62m 05s"


def test_both_reports_are_in_the_table_and_describe_themselves():
    for plugin_id in ("report.json", "report.html"):
        plugin = registry.get(plugin_id)
        assert plugin is not None
        assert plugin.metadata.category == registry.REPORT
        assert plugin.metadata.permissions == ("filesystem.write",)


def test_neither_report_needs_any_setting_at_all():
    assert ReportJson().problems({}) == []
    assert ReportHtml().problems({}) == []


def _read(context, name):
    with open(os.path.join(context.workspace, "reports", name),
              encoding="utf-8") as handle:
        return handle.read()
