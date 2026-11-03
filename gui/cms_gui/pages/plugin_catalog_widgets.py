"""Presentation of plugin inventory, using the application's theme tokens."""

from PySide6.QtCore import QSize, QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPen
from PySide6.QtWidgets import QStyledItemDelegate, QStyle

from .. import icons, theme


SOURCES = {'core_node': 'Built-in', 'bundled': 'Bundled', 'installed': 'Imported'}
STATES = {'available': 'Available', 'bundled_available': 'Available', 'active': 'Active',
          'installed_inactive': 'Inactive', 'disabled': 'Disabled', 'incompatible': 'Incompatible',
          'quarantined': 'Quarantined'}


def presentation(row):
    node, manifest = row.get('node', {}), row.get('manifest', {})
    delivery = row.get('delivery', 'installed' if row.get('installation') is not None else 'bundled')
    kinds = {item['kind'] for item in manifest.get('contributions', [])}
    category = node.get('category') or ('agent' if 'agent_backend' in kinds else 'action')
    state = row.get('effective_state', 'available')
    icon = {'agent': 'subjects', 'scenario': 'scenarios', 'service': 'services',
            'report': 'log', 'utility': 'settings'}.get(category, 'plugins')
    if row['plugin_id'].startswith('command.'):
        icon = 'command'
    description = node.get('summary') or manifest.get('description') or 'Extend your QAVector workflows.'
    if delivery == 'bundled' and row['plugin_id'] == 'qavector.agent.claude_cli':
        description = 'Review code and carry out development tasks with Claude Code.'
    return dict(name=node.get('name') or manifest.get('name') or row['plugin_id'],
        description=description,
        category=category.title(), source=SOURCES.get(delivery, delivery), delivery=delivery,
        publisher='QAVector' if delivery != 'installed' else manifest.get('publisher', 'Local publisher'),
        status=STATES.get(state, state.replace('_', ' ').title()), state=state,
        attention=state in ('incompatible', 'quarantined'),
        icon=icon)


class PluginDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(380, 94)

    def paint(self, painter, option, index):
        row = index.data(Qt.UserRole)
        if not isinstance(row, dict):
            return
        info = presentation(row)
        rect = option.rect
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        painter.save()
        painter.fillRect(rect, QColor(theme.SELECT_BG if selected else theme.SECTION_BG if hovered else theme.BG))
        if selected:
            painter.fillRect(QRect(rect.left(), rect.top(), 3, rect.height()), QColor(theme.ACCENT))
        painter.setPen(QColor(theme.DIVIDER))
        painter.drawLine(rect.left() + 16, rect.bottom(), rect.right() - 16, rect.bottom())
        icon_box = QRect(rect.left() + 16, rect.top() + 19, 38, 38)
        painter.fillRect(icon_box, QColor(theme.TAG_TINT))
        icons.paint(painter, info['icon'], icon_box.adjusted(9, 9, -9, -9), theme.ACCENT)
        left, right = rect.left() + 68, rect.right() - 18
        font = QFont(option.font)
        font.setPixelSize(10)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        badge_width = min(126, metrics.horizontalAdvance(info['status']) + 20)
        badge = QRect(right - badge_width, rect.top() + 18, badge_width, 22)
        inactive = info['state'] in ('installed_inactive', 'disabled')
        painter.fillRect(badge, QColor(theme.BAD_TINT if info['attention'] else theme.NEUTRAL[200] if inactive else theme.TAG_TINT))
        painter.setPen(QColor(theme.BAD_INK if info['attention'] else theme.NEUTRAL[700] if inactive else theme.TAG_INK))
        painter.drawText(badge, Qt.AlignCenter, info['status'])
        font.setPixelSize(14)
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(theme.SELECT_INK if selected else theme.TEXT))
        name = QFontMetrics(font).elidedText(info['name'], Qt.ElideRight, max(50, badge.left() - left - 16))
        painter.drawText(QRect(left, rect.top() + 16, max(50, badge.left() - left - 16), 24), Qt.AlignVCenter, name)
        font.setPixelSize(12)
        font.setWeight(QFont.Normal)
        painter.setFont(font)
        painter.setPen(QColor(theme.SELECT_MUTED if selected else theme.NEUTRAL[600]))
        description = QFontMetrics(font).elidedText(info['description'].replace('\n', ' '), Qt.ElideRight, right - left)
        painter.drawText(QRect(left, rect.top() + 42, right - left, 20), Qt.AlignVCenter, description)
        font.setPixelSize(10)
        painter.setFont(font)
        caption = '%s  ·  %s  ·  %s' % (info['category'], info['source'], info['publisher'])
        painter.drawText(QRect(left, rect.top() + 67, right - left - 66, 17), Qt.AlignVCenter,
                         QFontMetrics(font).elidedText(caption, Qt.ElideRight, max(0, right - left - 66)))
        version = QFontMetrics(font).elidedText('v' + row['version'], Qt.ElideRight, 64)
        painter.drawText(QRect(right - 64, rect.top() + 67, 64, 17), Qt.AlignRight | Qt.AlignVCenter, version)
        if selected and option.state & QStyle.State_HasFocus:
            painter.setPen(QPen(QColor(theme.SELECT_INK), 1, Qt.DotLine))
            painter.drawRect(rect.adjusted(5, 3, -5, -3))
        painter.restore()
