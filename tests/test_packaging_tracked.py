"""The package ships the scenarios and cycles git tracks, and nothing else.

A checkout's flows/ and cycles/ also hold what its owner recorded or wrote for
their own projects, untracked; the build used to bundle the directories as they
stood, so all of that went into the .deb for everyone who installed it.
"""

import importlib.util
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(ROOT, "packaging", "pyinstaller", "tracked_files.py")

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _helper():
    spec = importlib.util.spec_from_file_location("tracked_files", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def checkout(tmp_path):
    """flows/ with a shipped scenario, a shipped block, and somebody's own work."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "you@yourcompany.example")
    _git(tmp_path, "config", "user.name", "Builder")
    (tmp_path / "flows" / "scenarios").mkdir(parents=True)
    (tmp_path / "flows" / "auth").mkdir()
    (tmp_path / "flows" / "scenarios" / "shipped.yaml").write_text("steps: []\n")
    (tmp_path / "flows" / "auth" / "login.yaml").write_text("steps: []\n")
    _git(tmp_path, "add", "flows")
    _git(tmp_path, "commit", "-q", "-m", "flows")
    (tmp_path / "flows" / "scenarios" / "recorded_20260819_105309.yaml").write_text("x\n")
    (tmp_path / "flows" / "mine").mkdir()
    (tmp_path / "flows" / "mine" / "project.yaml").write_text("x\n")
    return tmp_path


def test_only_what_git_tracks_is_bundled(checkout):
    datas = _helper().tracked_datas(str(checkout), "flows")
    shipped = sorted(os.path.relpath(source, str(checkout)) for source, _dest in datas)
    assert shipped == [os.path.join("flows", "auth", "login.yaml"),
                       os.path.join("flows", "scenarios", "shipped.yaml")]


def test_each_file_keeps_its_place_in_the_tree(checkout):
    datas = dict(_helper().tracked_datas(str(checkout), "flows"))
    source = str(checkout / "flows" / "scenarios" / "shipped.yaml")
    assert datas[source] == os.path.join("flows", "scenarios")


def test_a_tracked_file_deleted_from_the_working_copy_is_left_out(checkout):
    (checkout / "flows" / "auth" / "login.yaml").unlink()
    datas = _helper().tracked_datas(str(checkout), "flows")
    assert [os.path.basename(source) for source, _dest in datas] == ["shipped.yaml"]


def test_a_directory_git_knows_nothing_of_stops_the_build(checkout):
    (checkout / "cycles").mkdir()
    (checkout / "cycles" / "mine.yaml").write_text("x\n")
    with pytest.raises(SystemExit, match="tracks nothing"):
        _helper().tracked_datas(str(checkout), "cycles")


def test_no_checkout_at_all_stops_the_build(tmp_path):
    (tmp_path / "flows").mkdir()
    (tmp_path / "flows" / "a.yaml").write_text("x\n")
    with pytest.raises(SystemExit):
        _helper().tracked_datas(str(tmp_path), "flows")


def test_the_core_spec_bundles_flows_and_cycles_through_it():
    with open(os.path.join(ROOT, "packaging", "pyinstaller", "core.spec"),
              encoding="utf-8") as handle:
        spec = handle.read()
    assert 'tracked_datas(ROOT, "flows")' in spec
    assert 'tracked_datas(ROOT, "cycles")' in spec
    assert '(os.path.join(ROOT, "flows"), "flows")' not in spec


def test_this_checkout_leaves_its_owner_s_scenarios_out():
    """The real tree: whatever is untracked under flows/ is not in the list."""
    datas = _helper().tracked_datas(ROOT, "flows")
    untracked = subprocess.run(
        ["git", "-C", ROOT, "ls-files", "--others", "--exclude-standard", "flows"],
        check=True, capture_output=True, text=True).stdout.split()
    bundled = {os.path.relpath(source, ROOT) for source, _dest in datas}
    assert not bundled & {os.path.join(*one.split("/")) for one in untracked}
