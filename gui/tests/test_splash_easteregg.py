"""One conditional change to the JPG, with the SVG prototype kept dormant."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QPixmap

from cms_gui import projectsfile, splash_easteregg as egg


ARTWORK = Path(__file__).resolve().parents[1] / "cms_gui" / "assets" / "splash.jpg"


def _history(directory, entries):
    (directory / "history.json").write_text(json.dumps(entries), encoding="utf-8")


@pytest.mark.parametrize("entries,failed", [
    ([], False),
    ([{"last_scenario_status": "fail"}], True),
    ([{"last_scenario_status": "error"}], True),
    ([{"last_scenario_status": "pass"}, {"last_scenario_status": "fail"}], False),
    ([{"kind": "launch", "status": "failed", "total": 0, "passed": 0},
      {"last_scenario_status": "pass"}], False),
    ([{"kind": "command", "status": "ok", "total": 0, "passed": 0},
      {"last_scenario_status": "fail"}], True),
    ([{"kind": "launch", "status": "failed", "total": 2, "passed": 1}], True),
    ([{"kind": "command", "status": "ok", "total": 2, "passed": 2},
      {"last_scenario_status": "fail"}], False),
    ([{"kind": "launch", "status": "stopped", "total": 2, "passed": 1}], False),
    ([{"kind": "cycle", "status": "failed", "total": 2, "passed": 1}], False),
    ([None, "bad", {"kind": "launch", "status": "failed", "total": "bad"}], False),
])
def test_only_the_last_scenario_result_matters(tmp_path, entries, failed):
    _history(tmp_path, entries)
    assert egg.last_scenario_failed(str(tmp_path)) is failed


@pytest.mark.parametrize("contents", [None, "{broken", "{}"])
def test_missing_or_corrupt_history_keeps_the_normal_splash(tmp_path, contents):
    if contents is not None:
        (tmp_path / "history.json").write_text(contents, encoding="utf-8")
    assert not egg.last_scenario_failed(str(tmp_path))


def test_splash_uses_the_project_that_will_be_restored(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _history(tmp_path, [{"last_scenario_status": "pass"}])
    _history(project_dir, [{"last_scenario_status": "fail"}])
    project = projectsfile.Project("chosen", "Chosen", str(project_dir))
    monkeypatch.setattr(projectsfile, "load", lambda: [projectsfile.workspace(), project])
    settings = SimpleNamespace(current_project="chosen", data_dir=lambda: str(tmp_path))
    assert egg.last_scenario_failed(egg.history_directory(settings))
    settings.current_project = projectsfile.WORKSPACE
    assert not egg.last_scenario_failed(egg.history_directory(settings))


def test_normal_splash_is_unchanged_and_failure_changes_only_step_three(qapp, tmp_path, monkeypatch):
    settings = SimpleNamespace(current_project="workspace", data_dir=lambda: str(tmp_path))
    monkeypatch.setattr(projectsfile, "load", lambda: [projectsfile.workspace()])
    original = QPixmap(str(ARTWORK)).toImage()
    assert egg.pixmap(str(ARTWORK), settings).toImage() == original
    _history(tmp_path, [{"last_scenario_status": "fail"}])
    changed = egg.pixmap(str(ARTWORK), settings).toImage()
    assert changed != original
    # Everything outside the central tile's verification row stays identical.
    for x, y, w, h in ((0, 0, 1024, 500), (0, 660, 1024, 364),
                       (0, 500, 475, 160), (720, 500, 304, 160)):
        assert changed.copy(x, y, w, h) == original.copy(x, y, w, h)
    red_pixels = [changed.pixelColor(x, y) for x in range(520, 552) for y in range(599, 631)]
    assert any(color.red() > 150 and color.green() < 100 and color.blue() < 100
               for color in red_pixels)
    _history(tmp_path, [{"last_scenario_status": "pass"}, {"last_scenario_status": "fail"}])
    assert egg.pixmap(str(ARTWORK), settings).toImage() == original
