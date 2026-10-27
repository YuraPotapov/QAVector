"""One catalogue and one user preference for every configurable Qt action.

The catalogue describes logical actions; several reader instances may bind the
same action. Qt still owns delivery and availability, so a menu and its shortcut
always execute the very same QAction.
"""

from dataclasses import dataclass
import weakref

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid


@dataclass(frozen=True)
class ActionSpec:
    id: str
    name: str
    description: str = ""
    category: str = ""
    default_shortcut: str = ""
    scope: str = "application"


def portable(sequence):
    """Canonical, platform independent spelling of a single Qt chord."""
    if isinstance(sequence, str):
        value = QKeySequence.fromString(sequence, QKeySequence.PortableText)
    elif isinstance(sequence, QKeySequence):
        value = sequence
    else:
        value = QKeySequence(sequence)
    if value.count() == 1:
        combined = value[0]
        key = _latin_key(combined.key())
        if key != int(combined.key()):
            value = QKeySequence(combined.keyboardModifiers().value | key)
    return value.toString(QKeySequence.PortableText)


def native(sequence):
    return QKeySequence.fromString(portable(sequence), QKeySequence.PortableText).toString(
        QKeySequence.NativeText)


# Qt key events identify keys independently of event.text(). Cyrillic key names
# are mapped to the matching positions on the standard Ukrainian/English layout.
# Native Qt shortcut matching already considers Latin alternatives on XKB; this
# also gives a recorder the same stored chord when the active layout is Ukrainian.
# Escaped so the source stays English: the Ukrainian letters, key by key.
_UKRAINIAN_KEYS = dict(zip(
    "\u0419\u0426\u0423\u041a\u0415\u041d\u0413\u0428\u0429\u0417\u0425\u0407"
    "\u0424\u0406\u0412\u0410\u041f\u0420\u041e\u041b\u0414\u0416\u0404"
    "\u042f\u0427\u0421\u041c\u0418\u0422\u042c\u0411\u042e\u0490",
    "QWERTYUIOP[]ASDFGHJKL;'ZXCVBNM,.\\"))
MODIFIER_KEYS = {Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta,
                 Qt.Key_AltGr, Qt.Key_Super_L, Qt.Key_Super_R}

# The number row and the punctuation keys, by where they are rather than what
# they type: Shift+2 reads "@" in English and '"' in Ukrainian, so a chord
# recorded in one layout never fired in the other - while Qt itself matches
# Ctrl+Shift+2 for that key in both (checked live on X11, us and ua). Named
# from the scan code, as on a US keyboard: XKB key codes (X11 and Wayland),
# and the set-1 scan codes Windows reports.
_ROW = "1234567890-="
_XKB_KEYS = dict(zip(range(10, 22), _ROW))
_XKB_KEYS.update({34: "[", 35: "]", 47: ";", 48: "'", 49: "`", 51: "\\",
                  59: ",", 60: ".", 61: "/"})
_WINDOWS_KEYS = dict(zip(range(2, 14), _ROW))
_WINDOWS_KEYS.update({0x1A: "[", 0x1B: "]", 0x27: ";", 0x28: "'", 0x29: "`",
                      0x2B: "\\", 0x33: ",", 0x34: ".", 0x35: "/"})
_SCAN_CODES = {"xcb": _XKB_KEYS, "wayland": _XKB_KEYS, "windows": _WINDOWS_KEYS}
#: Keys only a component's own scope may leave unmodified: they move focus,
#: confirm, edit and scroll in every widget there is.
_WIDGET_KEYS = {int(key) for key in (
    Qt.Key_Tab, Qt.Key_Backtab, Qt.Key_Backspace, Qt.Key_Return, Qt.Key_Enter,
    Qt.Key_Insert, Qt.Key_Delete, Qt.Key_Home, Qt.Key_End, Qt.Key_Left, Qt.Key_Up,
    Qt.Key_Right, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown, Qt.Key_Escape)}


def _latin_key(key):
    try:
        letter = chr(int(key)).upper()
    except (ValueError, OverflowError):
        return int(key)
    return ord(_UKRAINIAN_KEYS[letter]) if letter in _UKRAINIAN_KEYS else int(key)


def _physical_key(event):
    """The US name of the number-row or punctuation key pressed, or None."""
    table = _SCAN_CODES.get(QGuiApplication.platformName(), {})
    name = table.get(event.nativeScanCode())
    return ord(name) if name else None


def sequence_from_event(event):
    """Read a chord from its key code and modifiers, never the typed text."""
    key = event.key()
    if key in MODIFIER_KEYS or key in (0, Qt.Key_unknown):
        return ""
    key = _physical_key(event) or _latin_key(key)
    modifiers = event.modifiers() & (Qt.ControlModifier | Qt.AltModifier |
                                    Qt.ShiftModifier | Qt.MetaModifier)
    return portable(QKeySequence(modifiers.value | int(key)))


def alternate_name(name, index):
    """The row for a platform's ``index``-th key for one action: "Next match,
    second key" - read in a list, "(alternate 1)" said nothing about which."""
    if not index:
        return name
    ordinals = ("second", "third", "fourth", "fifth")
    ordinal = ordinals[index - 1] if index <= len(ordinals) else "%dth" % (index + 1)
    return "%s, %s key" % (name, ordinal)


def scope_label(scope):
    """Where an action's key works, in words: never "page/cycles"."""
    kind, _slash, name = scope.partition("/")
    words = name.replace("_", " ")
    if kind == "application":
        return "Works anywhere in QAVector"
    if kind == "page" and words:
        return "Works on the %s page" % words.capitalize()
    if words == "main window":
        return "Works in the main window"
    return "Works in the focused %s" % words if words else "Works where it is shown"


def scopes_overlap(left, right):
    """Only separate pages are known to be mutually exclusive.

    Component scopes intentionally overlap unless they explicitly name their
    page: a reader can be nested in another component or in a floating window.
    """
    if left == "application" or right == "application":
        return True
    left_parts, right_parts = left.split("/"), right.split("/")
    if (len(left_parts) >= 2 and len(right_parts) >= 2
            and left_parts[0] == right_parts[0] == "page"):
        return left_parts[1] == right_parts[1]
    return True


def _sequence_error(sequence, scope):
    if not isinstance(sequence, str):
        return "Shortcut must be a key combination."
    if not sequence:
        return ""
    parsed = QKeySequence.fromString(sequence, QKeySequence.PortableText)
    if parsed.count() != 1 or not parsed.toString(QKeySequence.PortableText):
        return "Use one key combination."
    key = parsed[0].key()
    if key in (0, Qt.Key_unknown) or key in MODIFIER_KEYS:
        return "Press a key together with any modifiers."
    modifiers = parsed[0].keyboardModifiers()
    specialized = scope.startswith("component/") and scope != "component/main_window"
    if not specialized and not (modifiers & (
            Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)):
        # A printable key would stop text being typed; Tab, Return, the
        # arrows, Backspace and the like would stop focus moving, forms
        # confirming and lists being walked - an item view does not claim its
        # arrows before a shortcut does. Function keys and the keyboard's own
        # Undo, Save and media keys go bare as they always have.
        if 0x20 <= int(key) < 0x1000000 or int(key) in _WIDGET_KEYS:
            return ("Use Ctrl, Alt or Meta with a text, navigation or editing key "
                    "outside a component.")
    return ""


class ActionRegistry(QObject):
    """Action metadata, live Qt bindings, and transactional user overrides."""

    changed = Signal()
    registered = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._specs = {}
        self._actions = {}
        self._overrides = dict(settings.hotkey_overrides())
        self._values = {}
        self._suspensions = 0

    @property
    def suspended(self):
        return self._suspensions > 0

    def specs(self):
        return sorted(self._specs.values(), key=lambda spec: (
            spec.category.casefold(), spec.name.casefold(), spec.id))

    def spec(self, action_id):
        return self._specs[action_id]

    def bindings(self):
        return dict(self._values)

    def defaults(self):
        return {spec.id: portable(spec.default_shortcut) for spec in self.specs()}

    def shortcut(self, action_id):
        return QKeySequence.fromString(self._values.get(action_id, ""),
                                       QKeySequence.PortableText)

    def register(self, spec, action=None):
        if not spec.id or not spec.name:
            raise ValueError("Actions require a stable ID and a name.")
        previous = self._specs.get(spec.id)
        if previous is not None and previous != spec:
            raise ValueError("Action %s is already registered with different metadata." % spec.id)
        if action is not None:
            for action_id, references in self._actions.items():
                if action_id != spec.id and any(reference() is action for reference in references):
                    raise ValueError("This QAction is already registered as %s." % action_id)
        previous_values = self._values
        self._specs[spec.id] = spec
        if action is not None:
            bindings = self._actions.setdefault(spec.id, [])
            if not any(reference() is action for reference in bindings):
                bindings.append(weakref.ref(action))
                action.setProperty("hotkeyActionId", spec.id)
                action.setAutoRepeat(False)
                if action.property("hotkeyBaseTooltip") is None:
                    action.setProperty("hotkeyBaseTooltip", action.toolTip())
        self._load_values()
        self._update_actions()
        if previous is None:
            self.registered.emit()
        if self._values != previous_values:
            self.changed.emit()
        return action

    def bind(self, spec, host, callback, enabled=None):
        existing = _existing_action(host, spec)
        if existing is not None:
            self.register(spec, existing)
            return existing
        action = _make_action(spec, host, callback, enabled)
        self.register(spec, action)
        return action

    def conflicts(self, action_id, sequence, draft=None):
        values = self.bindings() if draft is None else draft
        if not sequence:
            return []
        sequence = portable(sequence)
        scope = self._specs[action_id].scope
        found = []
        for spec in self.specs():
            other = values.get(spec.id, self._values.get(spec.id, ""))
            if (spec.id != action_id and isinstance(other, str) and other
                    and portable(other) == sequence and scopes_overlap(scope, spec.scope)):
                found.append(spec)
        return found

    def validate(self, draft):
        errors, seen = [], set()
        for spec in self.specs():
            sequence = draft.get(spec.id, self._values.get(spec.id, ""))
            error = _sequence_error(sequence, spec.scope)
            if error:
                errors.append("%s: %s" % (spec.name, error))
                continue
            for other in self.conflicts(spec.id, sequence, draft):
                pair = tuple(sorted((spec.id, other.id)))
                if pair not in seen:
                    seen.add(pair)
                    errors.append("%s / %s: %s" % (spec.name, other.name, native(sequence)))
        return errors

    def apply(self, draft):
        values = self.bindings()
        values.update({key: value for key, value in draft.items() if key in self._specs})
        errors = self.validate(values)
        if errors:
            raise ValueError("\n".join(errors))
        values = {key: portable(value) for key, value in values.items()}
        overrides = dict(self._overrides)  # Keep unavailable plugins' preferences.
        for spec in self.specs():
            value = values[spec.id]
            if value == portable(spec.default_shortcut):
                overrides.pop(spec.id, None)
            else:
                overrides[spec.id] = value  # Empty string is an explicit removal.
        self.settings.save_hotkey_overrides(overrides)
        self._overrides, self._values = overrides, values
        self._update_actions()
        self.changed.emit()

    def suspend(self):
        self._suspensions += 1
        if self._suspensions == 1:
            self._update_actions()

    def resume(self):
        if self._suspensions:
            self._suspensions -= 1
            if not self._suspensions:
                self._update_actions()

    def _load_values(self):
        # A damaged or hand-edited file must not create ambiguous shortcuts.
        # User assignments take precedence over defaults; conflicting entries
        # remain stored so an absent plugin's preference is never erased.
        values = {}
        ordered = sorted(self.specs(), key=lambda spec: (
            spec.id not in self._overrides, spec.id))
        for spec in ordered:
            raw = self._overrides.get(spec.id, portable(spec.default_shortcut))
            value = (portable(raw) if isinstance(raw, str)
                     and not _sequence_error(raw, spec.scope) else "")
            if any(value and value == other_value
                   and scopes_overlap(spec.scope, self._specs[other_id].scope)
                   for other_id, other_value in values.items()):
                value = ""
            values[spec.id] = value
        self._values = values

    def _update_actions(self):
        for action_id, references in self._actions.items():
            living = []
            sequence = self.shortcut(action_id)
            for reference in references:
                action = reference()
                if action is None or not isValid(action):
                    continue
                living.append(reference)
                action.setShortcut(QKeySequence() if self.suspended else sequence)
                base = action.property("hotkeyBaseTooltip") or self._specs[action_id].name
                text = sequence.toString(QKeySequence.NativeText)
                action.setToolTip("%s (%s)" % (base, text) if text else base)
            self._actions[action_id] = living


def bind_tooltip(action, widget, base=None):
    """Keep a button's hint in step with its QAction, including an empty key."""
    base = widget.toolTip() if base is None else base
    base = base or action.text().replace("&", "")
    target = weakref.ref(widget)

    def update():
        button = target()
        if button is None or not isValid(button):
            return
        sequence = action.shortcut().toString(QKeySequence.NativeText)
        button.setToolTip("%s (%s)" % (base, sequence) if sequence else base)

    action.changed.connect(update)
    update()


def registry_for(host):
    current = host
    while current is not None:
        registry = getattr(current, "action_registry", None)
        if isinstance(registry, ActionRegistry) and isValid(registry):
            return registry
        current = current.parent()
    return None


def _make_action(spec, host, callback, enabled):
    action = QAction(spec.name, host)
    action.setProperty("hotkeyActionId", spec.id)
    action._hotkey_spec = spec
    action.setToolTip(spec.description or spec.name)
    action.setShortcut(QKeySequence.fromString(portable(spec.default_shortcut),
                                              QKeySequence.PortableText))
    action.setShortcutContext(Qt.ApplicationShortcut if spec.scope == "application"
                              else Qt.WidgetWithChildrenShortcut)
    action.setAutoRepeat(False)
    if enabled is not None and not callable(enabled):
        action.setEnabled(bool(enabled))

    def trigger(_checked=False):
        if enabled is None or not callable(enabled) or enabled():
            callback()

    action.triggered.connect(trigger)
    host.addAction(action)
    return action


def _existing_action(host, spec):
    for action in host.findChildren(QAction, options=Qt.FindDirectChildrenOnly):
        if action.property("hotkeyActionId") == spec.id:
            previous = getattr(action, "_hotkey_spec", spec)
            if previous != spec:
                raise ValueError("Action %s is already bound with different metadata." % spec.id)
            return action
    return None


class _Adopter(QObject):
    """Adopt component actions when their initially parentless page is attached."""

    def __init__(self, app):
        super().__init__(app)
        self.pending = []
        self._watched = weakref.WeakSet()

    def add(self, host, action, spec):
        self.pending.append((weakref.ref(host), weakref.ref(action), spec))
        self._watch_ancestors()

    def _watch_ancestors(self):
        # Do not filter QApplication: WebEngine sends events for private native
        # receivers whose Python wrappers are unsafe on some PySide versions.
        # A host's existing ancestor chain includes its initially parentless
        # page, so watching only these known objects is enough for adoption.
        needed = set()
        for host_ref, _action_ref, _spec in self.pending:
            host = host_ref()
            while host is not None and isValid(host):
                needed.add(host)
                host = host.parent()
        for host in list(self._watched):
            if isValid(host) and host not in needed:
                host.removeEventFilter(self)
        for host in needed:
            if host not in self._watched:
                host.installEventFilter(self)
        self._watched = weakref.WeakSet(needed)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.ParentChange, QEvent.Show, QEvent.Polish):
            remaining = []
            pending, self.pending = self.pending, []
            for host_ref, action_ref, spec in pending:
                host, action = host_ref(), action_ref()
                if host is None or action is None or not isValid(host) or not isValid(action):
                    continue
                registry = registry_for(host)
                if registry is not None:
                    registry.register(spec, action)
                else:
                    remaining.append((host_ref, action_ref, spec))
            self.pending.extend(remaining)
            self._watch_ancestors()
        return False


def bind_action(host, spec, callback, enabled=None):
    """Bind a component action now, joining the owning window's registry later."""
    registry = registry_for(host)
    if registry is not None:
        return registry.bind(spec, host, callback, enabled)
    existing = _existing_action(host, spec)
    if existing is not None:
        return existing
    action = _make_action(spec, host, callback, enabled)
    app = QApplication.instance()
    if app is not None:
        adopter = getattr(app, "_hotkey_adopter", None)
        if adopter is None:
            adopter = app._hotkey_adopter = _Adopter(app)
        adopter.add(host, action, spec)
    return action
