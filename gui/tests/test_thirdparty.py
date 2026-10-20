"""Notices are usable offline, even when no core can start."""

import json

from cms_gui import core as core_mod, thirdparty
from cms_gui.main_window import MainWindow
from cms_gui.pages.settings_dialog import SettingsDialog
from cms_gui.settings import Settings


def test_notice_authorship_and_complete_license_are_readable_without_core(qapp, monkeypatch, dispose):
    def unavailable(*args, **kwargs):
        raise AssertionError("License access must not depend on the core or network")
    monkeypatch.setattr(core_mod.Core, "describe", unavailable)
    view = thirdparty.ThirdPartyLicensesDialog()
    try:
        assert "Bubblewrap" in view.components.item(0).text()
        assert "LGPL-2.1-or-later" in view.summary.text()
        assert "Alexander Larsson" in view.text.toPlainText()
        view.documents.setCurrentIndex(2)
        view._document_changed(2)
        expected = (thirdparty.licenses_root() / "bubblewrap/COPYING").read_text(encoding="utf-8")
        assert view.text.toPlainText() == expected
        assert view.text.isReadOnly()
    finally:
        dispose(view)


def test_help_about_and_third_party_licenses_are_connected(qapp, monkeypatch, dispose):
    opened = []
    monkeypatch.setattr(thirdparty.AboutDialog, "exec", lambda self: opened.append(type(self).__name__))
    monkeypatch.setattr(thirdparty.ThirdPartyLicensesDialog, "exec", lambda self: opened.append(type(self).__name__))
    settings = SettingsDialog(Settings())
    window = MainWindow()
    about = thirdparty.AboutDialog()
    try:
        assert not hasattr(settings, "about_button")
        help_action = next(action for action in window.menuBar().actions()
                           if action.text().replace("&", "") == "Help")
        help_menu = help_action.menu()
        next(action for action in help_menu.actions() if action.text() == "About").trigger()
        about.licenses_button.click()
        assert opened == ["AboutDialog", "ThirdPartyLicensesDialog"]
    finally:
        dispose(about)
        dispose(window)
        dispose(settings)


def test_frozen_viewer_reads_its_own_materials_not_user_data(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(thirdparty.sys, "frozen", True, raising=False)
    monkeypatch.setattr(thirdparty.sys, "_MEIPASS", str(tmp_path / "resources"), raising=False)
    monkeypatch.setenv("CMS_HOME", str(tmp_path / "untrusted"))
    assert thirdparty.licenses_root() == tmp_path / "resources/licenses"


def test_packaged_libcap_version_and_full_notice_are_visible(qapp, monkeypatch, tmp_path, dispose):
    (tmp_path / "libcap").mkdir()
    (tmp_path / "libcap/COPYRIGHT").write_text("Copyright: library authors\nFull terms", encoding="utf-8")
    catalog = {"schema_version": 1, "components": [{"name": "libcap", "version": "2.66",
        "license": "BSD-3-Clause", "documents": ["libcap/COPYRIGHT"]}]}
    (tmp_path / "components.json").write_text(json.dumps(catalog), encoding="utf-8")
    monkeypatch.setattr(thirdparty, "licenses_root", lambda: tmp_path)
    view = thirdparty.ThirdPartyLicensesDialog()
    try:
        assert "2.66" in view.summary.text()
        assert "library authors" in view.text.toPlainText()
    finally:
        dispose(view)
