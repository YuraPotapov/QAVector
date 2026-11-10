"""Plugin catalog and lifecycle. Package parsing and execution stay in core."""

import json
import html

from PySide6.QtCore import Qt, Signal, QEvent
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QAbstractItemView, QPlainTextEdit, QFileDialog,
    QDialog, QDialogButtonBox, QCheckBox, QInputDialog, QLineEdit, QComboBox,
    QFrame, QSplitter, QScrollArea, QFormLayout, QMenu, QToolButton, QSizePolicy)

from .. import background, icons, theme, widgets
from .plugin_catalog_widgets import PluginDelegate, presentation


def label(text='', role=None):
    result = QLabel(str(text))
    result.setTextFormat(Qt.PlainText)
    result.setWordWrap(True)
    result.setMinimumWidth(0)
    result.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
    if role:
        result.setProperty('role', role)
    return result


def document_dialog(parent, title, document):
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.resize(760, 600)
    layout = QVBoxLayout(dialog)
    text = QPlainTextEdit(json.dumps(document, indent=2, ensure_ascii=False))
    text.setReadOnly(True)
    layout.addWidget(text)
    buttons = QDialogButtonBox(QDialogButtonBox.Close)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    dialog.exec()
    dialog.deleteLater()


class PluginsPage(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.core, self.rows, self.busy = None, [], False
        self._detail_key = None
        self.buttons, self.actions = [], {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 18)
        outer.setSpacing(14)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(5)
        titles.addWidget(label('Plugins', 'h1'))
        titles.addWidget(label('Manage the capabilities that power your workflows.', 'lede'))
        header.addLayout(titles, 1)
        refresh = self.button('Refresh', self.refresh, 'cell', 'refresh')
        header.addWidget(refresh)
        more = QToolButton()
        more.setText('Manage')
        more.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(more)
        menu.addAction('Trust a publisher key…', self.trust_key)
        more.setMenu(menu)
        self.buttons.append(more)
        header.addWidget(more)
        header.addWidget(self.button('Import plugin…', self.import_package, 'primary', 'plus'))
        outer.addLayout(header)
        self.summary = label('Loading plugin inventory…', 'muted')
        outer.addWidget(self.summary)
        filters = QHBoxLayout()
        filters.setSpacing(10)
        self.search = QLineEdit()
        self.search.setPlaceholderText('Search plugins, publishers or capabilities…')
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(220)
        filters.addWidget(self.search, 1)
        self.source = widgets.capped(QComboBox())
        self.source.setAccessibleName('Plugin source')
        for text, value in [('All sources', ''), ('Built-in', 'core_node'), ('Bundled', 'bundled'), ('Imported', 'installed')]:
            self.source.addItem(text, value)
        filters.addWidget(self.source)
        self.state_filter = widgets.capped(QComboBox())
        self.state_filter.setAccessibleName('Plugin status')
        for text, value in [('All statuses', ''), ('Available', 'available'), ('Inactive', 'inactive'), ('Needs attention', 'attention')]:
            self.state_filter.addItem(text, value)
        filters.addWidget(self.state_filter)
        outer.addLayout(filters)
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setObjectName('pluginSplitter')
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(16)
        catalog = QWidget()
        column = QVBoxLayout(catalog)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        self.result_count = label('', 'kicker')
        column.addWidget(self.result_count)
        self.list = QListWidget()
        self.list.setObjectName('pluginCatalog')
        self.list.setAccessibleName('Plugins')
        self.list.setMinimumWidth(300)
        self.list.setItemDelegate(PluginDelegate(self.list))
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setMouseTracking(True)
        self.list.setFrameShape(QFrame.NoFrame)
        column.addWidget(self.list, 1)
        self.empty = widgets.empty_zone('No matching plugins', 'Try another search or import a .qavplug package.')
        self.empty.layout().setAlignment(Qt.AlignCenter)
        column.addWidget(self.empty, 1)
        self.empty.hide()
        self.splitter.addWidget(catalog)
        self.detail = QFrame()
        self.detail.setObjectName('pluginDetail')
        self.detail.setProperty('role', 'panel-fixed')
        self.detail.setMinimumWidth(300)
        frame_layout = QVBoxLayout(self.detail)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        frame_layout.setSpacing(0)
        self.panel_scroll = QScrollArea()
        self.panel_scroll.setWidgetResizable(True)
        self.panel_scroll.setFrameShape(QFrame.NoFrame)
        self.panel_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.panel = QFrame()
        self.panel.setObjectName('pluginDetailBody')
        self.panel.setProperty('role', 'panel')
        detail_layout = QVBoxLayout(self.panel)
        detail_layout.setContentsMargins(22, 20, 22, 20)
        detail_layout.setSpacing(15)
        detail_layout.addWidget(label('PLUGIN DETAILS', 'kicker'))
        self.detail_name = label('Select a plugin', 'h2')
        detail_layout.addWidget(self.detail_name)
        self.detail_id = label('', 'mono')
        self.detail_id.setTextInteractionFlags(Qt.TextSelectableByMouse)
        detail_layout.addWidget(self.detail_id)
        self.detail_status = widgets.Tag('', 'accent')
        self.detail_status.setTextFormat(Qt.PlainText)
        detail_layout.addWidget(self.detail_status, 0, Qt.AlignLeft)
        self.description = label('Select an item to see its capabilities, origin and available actions.', 'lede')
        detail_layout.addWidget(self.description)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignLeft)
        form.setVerticalSpacing(12)
        self.fields = {}
        for name in ('Version', 'Source', 'Publisher', 'Type'):
            self.fields[name] = label('—')
            form.addRow(label(name, 'muted'), self.fields[name])
        detail_layout.addLayout(form)
        line = QFrame()
        line.setProperty('role', 'hline')
        detail_layout.addWidget(line)
        detail_layout.addWidget(label('CAPABILITIES & ACCESS', 'kicker'))
        self.capabilities = label('')
        detail_layout.addWidget(self.capabilities)
        self.explanation = label('', 'muted')
        detail_layout.addWidget(self.explanation)
        self.problem = label('')
        self.problem.setObjectName('pluginProblem')
        detail_layout.insertWidget(4, self.problem)
        detail_layout.addStretch(1)
        self.panel_scroll.setWidget(self.panel)
        frame_layout.addWidget(self.panel_scroll, 1)
        self.action_panel = QWidget()
        self.action_panel.setObjectName('pluginDetailActions')
        action_layout = QVBoxLayout(self.action_panel)
        action_layout.setContentsMargins(22, 14, 22, 18)
        action_layout.setSpacing(10)
        action_layout.addWidget(self.button('Activate / roll back', lambda: self.mutate('activate'), 'primary'))
        action_row = QHBoxLayout()
        action_row.addWidget(self.button('Disable', lambda: self.mutate('disable')))
        action_row.addWidget(self.button('Remove', lambda: self.mutate('remove')))
        action_layout.addLayout(action_row)
        action_layout.addWidget(self.button('Details / schemas', self.schemas, 'cell'))
        frame_layout.addWidget(self.action_panel)
        for field in [self.detail_name, self.detail_id, self.description, self.capabilities,
                      self.explanation, self.problem, *self.fields.values()]:
            field.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.splitter.addWidget(self.detail)
        self.splitter.setSizes([700, 350])
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        outer.addWidget(self.splitter, 1)
        self.status = label('', 'muted')
        outer.addWidget(self.status)
        self.search.textChanged.connect(self.filter_rows)
        self.source.currentIndexChanged.connect(self.filter_rows)
        self.state_filter.currentIndexChanged.connect(self.filter_rows)
        self.list.currentItemChanged.connect(self.details)
        self.sync_actions()

    def button(self, text, callback, variant=None, icon=None):
        button = QPushButton(text)
        if variant:
            button.setProperty('variant', variant)
        if icon and icon in icons.DRAWINGS:
            icons.button(button, icon, text)
        button.clicked.connect(callback)
        self.buttons.append(button)
        self.actions[text] = button
        return button

    def set_core(self, core):
        self.core = core

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        orientation = Qt.Vertical if self.width() < 760 else Qt.Horizontal
        if self.splitter.orientation() != orientation:
            self.splitter.setOrientation(orientation)
            self.splitter.setSizes([250, 350] if orientation == Qt.Vertical else [700, 350])

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.StyleChange, QEvent.PaletteChange) and 'Details / schemas' in getattr(self, 'actions', {}):
            self.details()
            self.list.viewport().update()

    def call(self, work, done):
        if self.busy or self.core is None:
            return
        self.busy = True
        for button in self.buttons:
            button.setEnabled(False)
        self.status.setText('Working…')
        def answer(payload, error):
            self.busy = False
            for button in self.buttons:
                button.setEnabled(True)
            self.sync_actions()
            if error or not payload.get('ok'):
                self.status.setText(str(error) if error else payload.get('message', 'Operation rejected'))
            else:
                self.status.setText('Inventory is up to date')
                done(payload.get('result'))
        if background.offloads(self.core):
            background.run(work, answer, owner=self)
        else:
            answer(*background.call(work))

    def refresh(self):
        self.call(lambda: self.core.plugins('catalog'), self.populate)

    def populate(self, rows):
        self.rows = rows or []
        builtins = sum(row.get('delivery') == 'core_node' for row in self.rows)
        bundled = sum(presentation(row)['delivery'] == 'bundled' for row in self.rows)
        imported = sum(presentation(row)['delivery'] == 'installed' for row in self.rows)
        attention = sum(presentation(row)['attention'] for row in self.rows)
        self.summary.setText('%s entries  ·  %s built-in  ·  %s bundled  ·  %s imported%s' % (
            len(self.rows), builtins, bundled, imported, '  ·  %s need attention' % attention if attention else ''))
        self.filter_rows()

    def filter_rows(self, *_):
        selected = self.selected()
        key = (selected['plugin_id'], selected['version']) if selected else None
        query = self.search.text().strip().casefold()
        source, state = self.source.currentData(), self.state_filter.currentData()
        self.list.blockSignals(True)
        self.list.clear()
        selection = 0
        for row in self.rows:
            info = presentation(row)
            contributions = ' '.join(one['id'] for one in row.get('manifest', {}).get('contributions', []))
            haystack = ' '.join(str(value) for value in (info['name'], info['description'], info['publisher'], row['plugin_id'], info['category'], contributions)).casefold()
            if query and query not in haystack or source and source != info['delivery']:
                continue
            if state == 'attention' and not info['attention']:
                continue
            if state == 'available' and info['state'] not in ('available', 'bundled_available', 'active'):
                continue
            if state == 'inactive' and info['state'] not in ('installed_inactive', 'disabled'):
                continue
            item = QListWidgetItem('%s — %s — %s' % (info['name'], info['status'], info['source']))
            item.setData(Qt.UserRole, row)
            item.setToolTip('<qt>' + html.escape('%s · %s\n%s' % (info['name'], row['version'], info['description'])).replace('\n', '<br>') + '</qt>')
            self.list.addItem(item)
            if (row['plugin_id'], row['version']) == key:
                selection = self.list.count() - 1
        self.list.setCurrentRow(selection if self.list.count() else -1)
        self.list.blockSignals(False)
        self.result_count.setText('%s PLUGINS' % self.list.count())
        self.empty.setVisible(not self.list.count())
        self.list.setVisible(bool(self.list.count()))
        self.details()

    def selected(self):
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    def details(self, *_):
        row = self.selected()
        self.sync_actions()
        if not row:
            self.detail.hide()
            self._detail_key = None
            return
        key = (row['plugin_id'], row['version'])
        if key != self._detail_key:
            self.panel_scroll.verticalScrollBar().setValue(0)
            self._detail_key = key
        self.detail.show()
        info = presentation(row)
        self.detail_name.setText(info['name'])
        self.detail_id.setText(row['plugin_id'])
        self.detail_status.setText(info['status'])
        self.detail_status.set_variant('bad' if info['attention'] else 'accent' if info['status'] in ('Active', 'Available') else 'neutral')
        if info['attention']:
            self.detail_status.setStyleSheet(self.detail_status.styleSheet() + 'color: %s;' % theme.BAD_INK)
        self.detail_status.setFixedHeight(24)
        self.detail_status.setMinimumWidth(self.detail_status.fontMetrics().horizontalAdvance(info['status']) + 20)
        self.description.setText(info['description'])
        for name, value in [('Version', row['version']), ('Source', info['source']), ('Publisher', info['publisher']), ('Type', info['category'])]:
            self.fields[name].setText(value)
        if info['delivery'] == 'core_node':
            node = row['node']
            self.capabilities.setText('%s settings  ·  %s outputs\n%s' % (len(node.get('inputs', [])), len(node.get('outputs', [])),
                'Declared access: ' + ', '.join(node.get('permissions') or ['No additional permissions'])))
            self.explanation.setText('Included with QAVector and ready to add to a Cycle. Updated with the application.')
        else:
            manifest = row.get('manifest', {})
            grants = manifest.get('permissions_requested', {})
            parts = ['Workspace: ' + {'none': 'No access', 'read': 'Read only', 'write': 'Read & write'}.get(grants.get('workspace'), 'Not requested'),
                'Network: ' + ('Approved routes only' if grants.get('network') == 'policy_controlled' else 'No access'),
                'Tools: ' + (', '.join(grants.get('process', [])) or 'None'),
                'Credentials: ' + (', '.join(grants.get('credentials', [])) or 'None')]
            self.capabilities.setText('\n'.join(parts))
            self.explanation.setText('Ships with QAVector. Activate the version supplied by this application.' if info['delivery'] == 'bundled' else
                'Version and permissions are pinned for each run. Activation applies to new runs; existing runs keep their exact version.')
        issues = row.get('compatibility', {}).get('issues', [])
        reasons = ['%s: requires %s; running %s. %s' % (issue.get('component', 'Compatibility'),
            issue.get('required') or 'a declared version range', issue.get('actual') or 'unknown', issue.get('reason', '')) for issue in issues]
        if row.get('state', info['state']) == 'quarantined':
            reasons.insert(0, 'This version is quarantined. Activation is blocked until it passes reassessment.')
        elif info['state'] == 'incompatible' and not reasons:
            reasons.append('This version is incompatible with this QAVector installation.')
        self.problem.setText('\n'.join(reasons))
        self.problem.setVisible(bool(reasons))

    def sync_actions(self):
        row = self.selected()
        selected = bool(row) and not self.busy
        self.actions['Details / schemas'].setEnabled(selected)
        row = row or {}
        core = row.get('delivery') == 'core_node'
        state = row.get('state', row.get('effective_state', ''))
        blocked = state in ('quarantined', 'incompatible') or row.get('effective_state') == 'incompatible'
        installed = row.get('installation') is not None
        activate = self.actions['Activate / roll back']
        another_active = any(one['plugin_id'] == row.get('plugin_id') and one.get('state', one.get('effective_state')) == 'active' for one in self.rows)
        activate.setText('Active version' if state == 'active' else 'Switch to this version' if another_active else 'Activate version')
        activate.setVisible(not core)
        activate.setEnabled(selected and not core and not blocked and state != 'active')
        activate.setToolTip('Resolve the version issue before activation.' if blocked else 'Use this version for new runs. Existing runs retain their pinned version.')
        self.actions['Disable'].setVisible(installed)
        self.actions['Remove'].setVisible(installed)
        self.actions['Disable'].setEnabled(selected and installed and state in ('active', 'installed_inactive'))
        self.actions['Remove'].setEnabled(selected and installed and state != 'active')
        self.actions['Remove'].setToolTip('Disable this version before removal.' if state == 'active' else 'Removal is blocked while saved runs reference this version.')

    def updated(self, _result):
        self.changed.emit()
        self.refresh()

    def mutate(self, action):
        row = self.selected()
        if row is None:
            return
        if row.get('delivery') == 'core_node' or row.get('installation') is None and action != 'activate':
            return
        self.call(lambda: self.core.plugins(action, row['plugin_id'], row['version']), self.updated)

    def schemas(self):
        row = self.selected()
        if row:
            if row.get('installation') is None:
                document_dialog(self, 'Plugin details', row)
            else:
                self.call(lambda: self.core.plugins('show', row['plugin_id'], row['version']),
                    lambda result: document_dialog(self, 'Plugin schemas', result))

    def import_package(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Import plugin', '', 'QAVector plugins (*.qavplug)')
        if path:
            self.call(lambda: self.core.plugins('inspect', path), lambda result: self.review(path, result))

    def review(self, path, inspection):
        dialog = QDialog(self)
        dialog.setWindowTitle('Review plugin import')
        dialog.resize(650, 520)
        outer = QVBoxLayout(dialog)
        outer.setContentsMargins(20, 20, 20, 20)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(4, 4, 12, 12)
        layout.setSpacing(16)
        manifest = inspection['manifest']
        layout.addWidget(label(manifest.get('name', manifest['plugin_id']), 'h1'))
        layout.addWidget(label(manifest.get('description', ''), 'lede'))
        form = QFormLayout()
        for title, value in [('Version', manifest['version']), ('Publisher', manifest.get('publisher', '')),
                             ('Signature', inspection.get('trust', {}).get('status', '').replace('_', ' ').title()),
                             ('Package ID', manifest['plugin_id']), ('SHA-256', inspection['digest'])]:
            form.addRow(label(title, 'muted'), label(value))
        layout.addLayout(form)
        grants = manifest['permissions_requested']
        layout.addWidget(label('REQUESTED ACCESS', 'kicker'))
        layout.addWidget(label('Workspace: %s\nNetwork: %s\nTools: %s\nCredentials: %s' % (
            grants['workspace'], grants['network'].replace('_', ' '),
            ', '.join(grants['process']) or 'None', ', '.join(grants['credentials']) or 'None')))
        technical = QPushButton('View manifest and compatibility details')
        technical.setProperty('variant', 'ghost')
        technical.clicked.connect(lambda: document_dialog(dialog, 'Package details', inspection))
        layout.addWidget(technical)
        if not inspection.get('runtime_ready'):
            layout.addWidget(label('The managed plugin runtime must be provisioned before installation.'))
        for issue in inspection.get('compatibility', {}).get('issues', []):
            layout.addWidget(label('%s: %s (requires %s, running %s)' % (issue.get('component', 'Compatibility'),
                issue.get('reason', ''), issue.get('required') or 'a declared range', issue.get('actual') or 'unknown')))
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        consent = QCheckBox('I trust this exact package and approve the displayed permissions.')
        outer.addWidget(consent)
        outer.addWidget(label('Import keeps the package inactive. Activation is a separate action.', 'muted'))
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText('Import inactive')
        buttons.button(QDialogButtonBox.Ok).setEnabled(False)
        consent.toggled.connect(lambda checked: buttons.button(QDialogButtonBox.Ok).setEnabled(
            checked and bool(inspection.get('approval_required')) and inspection.get('runtime_ready', False)))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        outer.addWidget(buttons)
        if dialog.exec() == QDialog.Accepted:
            self.call(lambda: self.core.plugin_import(path, inspection['approval_required']), self.updated)
        dialog.deleteLater()

    def trust_key(self):
        identifier, ok = QInputDialog.getText(self, 'Enroll trusted publisher', 'Key ID')
        if not ok or not identifier.strip():
            return
        key, ok = QInputDialog.getText(self, 'Enroll trusted publisher', 'Ed25519 public key (base64)')
        if ok and key.strip():
            self.call(lambda: self.core.plugins('trust-key', identifier.strip(), key.strip()), self.updated)
