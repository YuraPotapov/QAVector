"""What the GUI says it is, and what About says about both halves.

The version is written down once, in pyproject.toml, and everything else reads
it back - the GUI included, even though it runs from its own virtualenv where
the distribution is usually not installed. A second copy in the source is the
failure this guards: it does not break anything, it just quietly reports the
wrong number forever.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cms_gui

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def _pyproject_version():
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as handle:
        for table in re.split(r"(?m)^\[", handle.read()):
            if table.startswith("project]"):
                found = re.search(r"(?m)^\s*version\s*=\s*[\"']([^\"']+)[\"']",
                                  table)
                if found:
                    return found.group(1)
    raise AssertionError("pyproject.toml has no [project] version")


def test_the_gui_reports_the_projects_version():
    assert cms_gui.version() == _pyproject_version()


def test_the_version_is_not_hard_coded_in_the_source():
    """The number itself must not appear in the package - only the lookup."""
    package = os.path.join(ROOT, "gui", "cms_gui")
    wanted = _pyproject_version()
    offenders = []
    for folder, _dirs, files in os.walk(package):
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(folder, name)
            with open(path, encoding="utf-8") as handle:
                if wanted in handle.read():
                    offenders.append(os.path.relpath(path, ROOT))
    assert not offenders, ("pyproject.toml is the only place the version lives: %s"
                           % ", ".join(offenders))


def test_a_packaged_build_reads_the_version_file_beside_it(tmp_path, monkeypatch):
    """The path packaging depends on, and the one a checkout never exercises.

    A frozen GUI has no distribution metadata and no pyproject.toml to fall back
    on; all it has is the VERSION file build_deb.sh installs at the prefix above
    the two bundles (/opt/qavector/VERSION, with the executable in
    gui/ beside core/). If that lookup ever stops matching the layout, About
    silently reports "dev (not installed)" on every installed machine.
    """
    prefix = tmp_path / "opt" / "qavector"
    (prefix / "gui").mkdir(parents=True)
    (prefix / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable",
                        str(prefix / "gui" / "qavector-gui"))
    monkeypatch.setattr(cms_gui, "_version", None)
    # No installed distribution in a freeze - that is what makes VERSION the answer.
    monkeypatch.setattr(cms_gui, "_installed_version", lambda: "")
    try:
        assert cms_gui.version() == "9.9.9"
    finally:
        cms_gui._version = None


def test_about_names_the_application_and_its_version(qapp, dispose):
    """One application, one version - not a front-end with two halves."""
    from cms_gui import main_window as main_window_mod

    window = main_window_mod.MainWindow()
    try:
        text = window.about_text()
    finally:
        dispose(window)
    assert "Version: %s" % cms_gui.version() in text
    assert "Qt (PySide6): " in text
    for gone in ("GUI:", "core:", "from:", "Python:", "front-end"):
        assert gone not in text
