import json
from pathlib import Path
import time
import pytest

from cms_gui.pages.plugins import PluginsPage
from cms_gui.pages.cycledialogs import CycleDialog, NodeDialog
from cms_gui.core import Core


def test_catalog_from_frozen_core_is_visible_on_a_fresh_profile(qapp, dispose, request, tmp_path, monkeypatch):
    executable = request.config.getoption('--gui-runtime-frozen-core')
    if not executable:
        pytest.skip('Pass --gui-runtime-frozen-core for packaged catalog acceptance')
    monkeypatch.setenv('CMS_HOME', str(tmp_path / 'data'))
    monkeypatch.setenv('PATH', '/no-system-python-or-tools')
    page = PluginsPage()
    try:
        page.set_core(Core(script=str(Path(executable).absolute()), interpreter=''))
        page.refresh()
        deadline = time.monotonic() + 30
        while page.busy and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert not page.busy, 'Packaged catalog request timed out'
        assert page.rows, page.status.text()
        assert any(row.get('delivery') == 'core_node' for row in page.rows)
        assert not any(row.get('delivery') == 'installed' for row in page.rows)
        page.source.setCurrentIndex(page.source.findData('bundled'))
        assert page.list.count() == 1
        assert page.selected()['state'] == 'bundled_available'
        assert 'Claude' in page.selected()['manifest']['name']
        assert page.actions['Activate / roll back'].isEnabled()
        assert not page.actions['Remove'].isVisible()
    finally:
        dispose(page)


def package_row(state='installed_inactive', version='1.0.0'):
    return {'plugin_id': 'example.quality', 'version': version, 'delivery': 'installed',
        'state': state, 'effective_state': state, 'installation': {}, 'manifest': {
            'name': 'Quality Tools', 'publisher': 'Example Engineering', 'description': 'Check workspace conventions.',
            'contributions': [{'kind': 'node_provider', 'id': 'example.workspace.validate'}]}}


class FakeCore:
    def __init__(self):
        self.calls = []
    def plugins(self, *args):
        self.calls.append(args)
        if args[0] == 'catalog':
            return {'ok': True, 'result': [{'plugin_id': 'example.demo', 'version': '1.0.0',
                'effective_state': 'incompatible', 'installation': {}, 'manifest': {'contributions': [
                    {'kind': 'node_provider', 'id': 'example.demo.run'}]}}]}
        return {'ok': False, 'message': 'Removal blocked by run lock'}


def test_catalog_keeps_unavailable_packages_and_displays_lifecycle_errors(qapp, dispose):
    page = PluginsPage()
    try:
        core = FakeCore()
        page.set_core(core)
        page.refresh()
        assert page.selected()['effective_state'] == 'incompatible'
        page.mutate('remove')
        assert core.calls[-1] == ('remove', 'example.demo', '1.0.0')
        assert 'run lock' in page.status.text()
    finally:
        dispose(page)


def test_core_nodes_are_visible_without_package_actions(qapp, dispose):
    page = PluginsPage()
    try:
        page.populate([{'plugin_id': 'command.shell', 'version': '1.0.0', 'effective_state': 'available',
            'delivery': 'core_node', 'node': {'name': 'Shell Command', 'inputs': []}}])
        assert page.fields['Source'].text() == 'Built-in'
        assert not page.actions['Activate / roll back'].isEnabled()
        assert not page.actions['Remove'].isEnabled()
        assert page.actions['Details / schemas'].isEnabled()
    finally:
        dispose(page)


def test_catalog_search_filters_and_selection_survive_refresh(qapp, dispose):
    page = PluginsPage()
    rows = [{'plugin_id': name, 'version': '1.0.0', 'effective_state': 'available',
             'delivery': 'core_node', 'node': {'name': title, 'summary': summary, 'category': category}}
            for name, title, summary, category in [('agent', 'Agent', 'Run a task', 'agent'),
                ('command.shell', 'Shell Command', 'Execute a command', 'action')]]
    try:
        page.populate(rows)
        page.search.setText('command')
        assert page.list.count() == 1 and page.selected()['plugin_id'] == 'command.shell'
        page.populate(rows)
        assert page.selected()['plugin_id'] == 'command.shell'
        page.source.setCurrentIndex(page.source.findData('installed'))
        assert page.list.count() == 0 and page.selected() is None
        assert not page.actions['Activate / roll back'].isEnabled()
        page.source.setCurrentIndex(0)
        page.search.clear()
        page.state_filter.setCurrentIndex(page.state_filter.findData('attention'))
        assert page.list.count() == 0
    finally:
        dispose(page)


def test_json_fields_preserve_nested_types_and_reject_invalid_edits(qapp, dispose, monkeypatch):
    document = {'id': 'demo', 'plugin': 'example.demo.run', 'with': {'config': {'list': [1, True, None]}}}
    plugin = {'id': 'example.demo.run', 'inputs': [{'key': 'config', 'kind': 'json', 'label': 'Config'}]}
    dialog = NodeDialog(document, plugin)
    try:
        dialog._save()
        assert dialog.saved == document
        dialog.saved = None
        dialog._widgets['with.config'].setPlainText('{broken')
        errors = []
        monkeypatch.setattr(dialog, '_complain', errors.extend)
        dialog._save()
        assert dialog.saved is None and 'invalid JSON' in errors[0]
        dialog._widgets['with.config'].setPlainText('{"list": [false, {"a": 2}]}')
        dialog._save()
        assert dialog.saved['with']['config'] == {'list': [False, {'a': 2}]}
    finally:
        dispose(dialog)


def test_roles_roundtrip_in_cycle_properties(qapp, dispose):
    role = {'agent.business.analyst': {'backend_ref': 'example.agent', 'instructions': 'Analyze'}}
    dialog = CycleDialog({'id': 'demo', 'agent_profiles': role}, agent_backends=['example.agent'])
    try:
        dialog._save()
        assert dialog.saved['agent_profiles'] == role
        dialog.agent_profiles.setPlainText('{}')
        dialog._save()
        assert dialog.saved['agent_profiles'] == {}
    finally:
        dispose(dialog)


def test_plugin_cli_omits_project_flags_and_import_does_not_activate(tmp_path, monkeypatch):
    from types import SimpleNamespace
    calls, approval_data = [], []
    def run(argv, **kwargs):
        calls.append(argv)
        if '--approval' in argv:
            with open(argv[argv.index('--approval') + 1]) as stream:
                approval_data.append(json.load(stream))
        return SimpleNamespace(stdout='{"ok":true,"result":{}}')
    monkeypatch.setattr('cms_gui.core.subprocess.run', run)
    core = Core('/fake/session_launcher.py', '/fake/python', config='/private/users.json')
    core.plugin_import('/tmp/demo.qavplug', {'digest': 'reviewed', 'grants': {}})
    assert calls[0][2:5] == ['--plugins', 'import', '/tmp/demo.qavplug']
    assert not any(arg.startswith('--config') for arg in calls[0])
    assert '--activate' not in calls[0]
    assert approval_data == [{'digest': 'reviewed', 'grants': {}}]


@pytest.mark.parametrize('state,activate,disable,remove', [
    ('active', False, True, False), ('installed_inactive', True, True, True),
    ('disabled', True, False, True), ('incompatible', False, False, True),
    ('quarantined', False, False, True),
])
def test_lifecycle_actions_match_package_state(qapp, dispose, state, activate, disable, remove):
    page = PluginsPage()
    try:
        page.populate([package_row(state)])
        assert page.actions['Activate / roll back'].isEnabled() == activate
        assert page.actions['Disable'].isEnabled() == disable
        assert page.actions['Remove'].isEnabled() == remove
        if state in ('incompatible', 'quarantined'):
            assert page.problem.text()
    finally:
        dispose(page)


def test_search_finds_contributions_and_switching_versions_keeps_identity(qapp, dispose):
    page = PluginsPage()
    try:
        page.populate([package_row('active', '2.0.0'), package_row()])
        page.search.setText('workspace.validate')
        assert page.list.count() == 2
        page.list.setCurrentRow(1)
        assert page.actions['Activate / roll back'].text() == 'Switch to this version'
        page.populate([package_row('active', '2.0.0'), package_row()])
        assert page.selected()['version'] == '1.0.0'
        page.busy = True
        page.details()
        assert all(not page.actions[name].isEnabled() for name in ('Activate / roll back', 'Disable', 'Remove', 'Details / schemas'))
    finally:
        dispose(page)


@pytest.mark.parametrize('width,height', [(1250, 850), (850, 650), (680, 750)])
def test_details_actions_stay_visible_with_long_metadata(qapp, dispose, width, height):
    from PySide6.QtCore import Qt, QPoint
    page = PluginsPage()
    row = package_row()
    row['plugin_id'] = 'example.' + 'longidentifier' * 12
    row['manifest'].update(name='Long plugin title ' * 12, description='Long description. ' * 100)
    try:
        page.resize(width, height)
        page.populate([row])
        page.show()
        qapp.processEvents()
        assert page.width() == width
        assert page.splitter.orientation() == (Qt.Vertical if width < 760 else Qt.Horizontal)
        assert page.panel_scroll.horizontalScrollBar().maximum() == 0
        action = page.actions['Activate / roll back']
        point = action.mapTo(page, QPoint(0, action.height()))
        assert action.isVisible() and point.y() < page.height()
        assert page.panel_scroll.verticalScrollBar().maximum() > 0
    finally:
        dispose(page)


def test_live_theme_change_updates_status_and_empty_state(qapp, dispose):
    from cms_gui import theme
    page = PluginsPage()
    try:
        page.populate([package_row('quarantined')])
        page.show()
        theme.set_dark_mode(True)
        qapp.setStyleSheet(theme.stylesheet())
        qapp.processEvents()
        assert theme.BAD_TINT in page.detail_status.styleSheet()
        assert 'color: %s;' % theme.BAD_INK in page.detail_status.styleSheet()
        page.search.setText('no matches')
        qapp.processEvents()
        assert page.empty.isVisible() and not page.detail.isVisible()
        assert page.result_count.y() < page.empty.y()
    finally:
        dispose(page)
        theme.set_dark_mode(False)
        qapp.setStyleSheet(theme.stylesheet())


@pytest.mark.parametrize('runtime_ready,compatible', [(True, True), (False, True), (True, False)])
def test_import_review_requires_approval_and_keeps_actions_visible(qapp, dispose, monkeypatch, runtime_ready, compatible):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QDialog, QDialogButtonBox, QCheckBox, QScrollArea
    page = PluginsPage()
    inspection = {'manifest': dict(package_row()['manifest'], plugin_id='example.quality', version='1.0.0',
        description='Long description ' * 100, permissions_requested={'workspace': 'read', 'network': 'none', 'process': [], 'credentials': []}),
        'digest': 'a' * 64, 'runtime_ready': runtime_ready, 'approval_required': {'digest': 'reviewed'} if compatible else None}
    calls = []
    page.core = type('Core', (), {'plugin_import': lambda self, *args: calls.append(args) or {'ok': True, 'result': {}}})()
    page.updated = lambda _: None
    def review(dialog):
        dialog.show(); qapp.processEvents()
        button = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)
        assert not button.isEnabled()
        dialog.findChild(QCheckBox).setChecked(True)
        assert button.isEnabled() == (runtime_ready and compatible)
        assert button.mapTo(dialog, QPoint(0, button.height())).y() <= dialog.height()
        assert dialog.findChild(QScrollArea).verticalScrollBar().maximum() > 0
        dialog.hide()
        return QDialog.Accepted if button.isEnabled() else QDialog.Rejected
    monkeypatch.setattr(QDialog, 'exec', review)
    try:
        page.review('/tmp/example.qavplug', inspection)
        assert calls == ([('/tmp/example.qavplug', {'digest': 'reviewed'})] if runtime_ready and compatible else [])
    finally:
        dispose(page)
