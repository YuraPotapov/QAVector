"""Restrictions in the GUI: the button beside a folder, the quick toggles, the window.

A folder - a services project's, a cycle's - may hold the programs that run in
it to less than the user's whole account: no network, the project folder only
(read-write, read-only or not at all), and limits on CPU, memory and processes.
A service or a step can only narrow what its folder allows. Nothing has to be
created first and nothing changes for a folder that sets none.

Three surfaces, one stored mapping:

- **The button beside a folder field** says what the folder holds its programs
  to and opens the window.
- **Quick toggles** on a service row or a cycle node show Network / Files /
  Limits as they are now. A click on Network or Files switches that access off
  - or back on to exactly what was configured, because switching off writes
  ``switched_off: [...]`` beside the configuration instead of overwriting it. Limits opens
  a small popover with the numbers.
- **The window** edits one level in full.

What is shown here is worked out locally, so a toggle answers at once. It is
presentation only: the core resolves the same mapping again
(``--restrictions-preview``), refuses what cannot be enforced, and builds the
sandbox itself (``--restricted-exec``). The GUI never builds a sandbox command.
"""

import copy
import json
import os
import subprocess

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMenu, QMessageBox, QPushButton, QSizePolicy, QTableWidgetItem, QToolButton,
                               QVBoxLayout, QWidget, QWidgetAction)

from . import background, icons, widgets

NETWORK = ("on", "isolated", "off")
NETWORK_SHORT = {"on": "On", "isolated": "Isolated", "off": "Off"}
NETWORK_ICON = {"on": "network", "isolated": "network-isolated", "off": "network-off"}
LOOPBACK = ("127.0.0.1", "::1")
#: Widest first: narrowing moves to the right.
FILES = ("all", "read_write", "read_only", "none")
LIMITS = ("cpu_cores", "memory_mb", "processes")
SWITCHES = ("network", "files")

#: "and", not "&": a button label reads an ampersand as a keyboard shortcut.
FILE_WORDS = {"all": "No restrictions", "read_write": "Read and write", "read_only": "Read-only", "none": "No access"}
FILE_SHORT = {"all": "All", "read_write": "RW", "read_only": "RO", "none": "Off"}
LIMIT_WORDS = {"cpu_cores": "CPU cores", "memory_mb": "Memory", "processes": "Processes"}

#: Runner types whose processes can be held to a folder. A container is started
#: by its daemon, which nothing here can restrict.
SERVICE_TYPES = ("python", "shell")

#: Cycle plugins whose processes run inside the restrictions; every other step
#: is a system step. The core's own list (cycle/restrict.py) decides at run time.
CYCLE_PLUGINS = frozenset((
    "command.shell", "git.checkout", "git.commit", "git.prepare_branch",
    "agent.review", "agent.edit", "agent.implement", "agent.merge", "task.brief",
    "jira.issues", "jira.transition", "teams.message",
))
#: Runs without restrictions for now - it drives a real browser - so its node
#: says SYSTEM, like a step marked so on purpose.
SYSTEM_PLUGINS = frozenset(("scenario.run",))
#: Cannot work with the network off: restricted that way, the run refuses them.
NEEDS_NETWORK = frozenset(("agent.review", "agent.edit", "agent.implement", "agent.merge",
                           "jira.issues", "jira.transition", "teams.message"))


def step_kind(step):
    """``"restricted"``; ``"system"`` - runs without restrictions, and its node
    says so; or ``""`` - nothing to restrict: an approval, a check, a report."""
    plugin = step.get("plugin", "")
    if step.get("system") or plugin in SYSTEM_PLUGINS:
        return "system"
    return "restricted" if plugin in CYCLE_PLUGINS else ""


# -- the stored mapping, read the way the core reads it -------------------------------
def network_value(value):
    """``on``/``off`` - also when a hand-written YAML file said them unquoted,
    which YAML 1.1 reads as true and false."""
    if isinstance(value, bool):
        return "on" if value else "off"
    return value


def clean(raw):
    """A copy without empty keys - what a level writes when it says nothing."""
    result = {}
    for key, value in (raw or {}).items():
        if value in (None, "", [], {}):
            continue
        result[key] = copy.deepcopy(value)
    return result


def resolve(layers):
    """What a program gets from ``[(level, raw), ...]``, outermost first.

    The outer level sets the defaults and an inner one takes them unless it
    says otherwise, in either direction. Mirrors
    ``execution_runtime.restrictions.resolve`` closely enough to draw the
    controls; the core repeats it with the real paths before anything runs.
    """
    network, files, limits, folders, switched = "on", "all", {}, [], []
    outbound, host_loopback, publish = False, False, []
    configured = {"network": "on", "files": "all"}
    decided = {"network": "", "files": ""}
    for level, raw in layers:
        raw = raw or {}
        if "network" in raw and network_value(raw["network"]) in NETWORK:
            network = configured["network"] = network_value(raw["network"])
            decided["network"] = level
        if isinstance(raw.get("outbound"), bool):
            outbound, decided["outbound"] = raw["outbound"], level
        if isinstance(raw.get("host_loopback"), bool):
            host_loopback, decided["host_loopback"] = raw["host_loopback"], level
        if isinstance(raw.get("publish"), list):
            publish, decided["publish"] = [dict(one) for one in raw["publish"]], level
        if raw.get("files") in FILES:
            files = configured["files"] = raw["files"]
            decided["files"] = level
        if "folders" in raw:
            folders = list(raw.get("folders") or [])
        for key, value in (raw.get("limits") or {}).items():
            if key in LIMITS:
                limits[key] = value
                decided["limits." + key] = level
        for kind in raw.get("switched_off") or []:
            if kind not in SWITCHES:
                continue
            if kind == "network":
                network = "off"
            else:
                files = "none"
            decided[kind] = decided["switched_off." + kind] = level
            if kind not in switched:
                switched.append(kind)
    if files in ("all", "none"):
        folders = []
    isolated = network == "isolated"
    for one in publish:
        one.setdefault("host_port", "auto")
        one.setdefault("host_ip", "127.0.0.1")
    return {"restricted": network != "on" or files != "all" or bool(limits),
            "network": network, "files": files, "folders": folders, "limits": limits,
            "outbound": isolated and outbound, "host_loopback": isolated and host_loopback,
            "publish": publish if isolated else [],
            "switched_off": [one for one in SWITCHES if one in switched], "configured": configured,
            "decided": decided}


def network_icon(shown):
    """The globe for what a Network toggle shows: on, isolated (boxed) or off (struck)."""
    return {"Off": "network-off", "Isolated": "network-isolated"}.get(shown, "network")


def publication_words(one, bound=None):
    """``8069 -> localhost:18069``; ``auto`` until it is bound."""
    host = (bound or one).get("host_port", "auto")
    address = (bound or one).get("host_ip", "127.0.0.1")
    where = "localhost" if address in LOOPBACK else address
    return "%s -> %s:%s" % (one["internal_port"], where, host)


def memory_words(megabytes):
    if megabytes >= 1024 and megabytes % 1024 == 0:
        return "%d GB" % (megabytes // 1024)
    if megabytes >= 1024:
        return "%.1f GB" % (megabytes / 1024.0)
    return "%d MB" % megabytes


def limits_words(limits, short=False):
    parts = []
    if "memory_mb" in limits:
        parts.append(memory_words(limits["memory_mb"]))
    if "cpu_cores" in limits:
        cores = limits["cpu_cores"]
        parts.append("%s CPU" % (int(cores) if cores == int(cores) else cores))
    if "processes" in limits:
        parts.append(("%d proc" if short else "%d processes") % limits["processes"])
    return " · ".join(parts)


def summary(effective):
    """One line: what the program is held to. The core's own words, kept in step.

    The network always says which way it is: it is the one thing somebody
    looking at a service most often needs to know.
    """
    effective = effective or resolve([])
    parts = [{"off": "Network off", "isolated": "Network isolated"}.get(effective["network"], "Network on")]
    if not effective.get("restricted"):
        return parts[0] + " · No restrictions"
    if effective["network"] == "isolated":
        if effective.get("publish"):
            parts.append("publishes " + ", ".join(str(one["internal_port"]) for one in effective["publish"]))
        if effective.get("outbound"):
            parts.append("internet")
        if effective.get("host_loopback"):
            parts.append("this computer")
    if effective["files"] != "all":
        parts.append({"read_write": "Project read and write", "read_only": "Project read-only",
                      "none": "No file access"}[effective["files"]])
        if effective["folders"]:
            parts.append("+%d folder%s" % (len(effective["folders"]), "" if len(effective["folders"]) == 1 else "s"))
    if effective["limits"]:
        parts.append(limits_words(effective["limits"]))
    return " · ".join(parts)


def short_summary(effective):
    """Fewer words, for a button: ``Net off · RW · 4 GB``, ``Net on · No restrictions``."""
    effective = effective or resolve([])
    parts = [{"off": "Net off", "isolated": "Isolated"}.get(effective["network"], "Net on")]
    if not effective.get("restricted"):
        return parts[0] + " · No restrictions"
    if effective["files"] != "all":
        parts.append({"read_write": "RW", "read_only": "RO", "none": "No files"}[effective["files"]])
    if effective["limits"]:
        parts.append(limits_words(effective["limits"], short=True))
    return " · ".join(parts)


def toggled(raw, kind):
    """``raw`` with ``kind`` switched off, or back on to what was configured."""
    result = clean(raw)
    off = list(result.get("switched_off") or [])
    if kind in off:
        off.remove(kind)
    else:
        off.append(kind)
    if off:
        result["switched_off"] = [one for one in SWITCHES if one in off]
    else:
        result.pop("switched_off", None)
    return result


def _outer_word(outer):
    return outer[-1][0] if outer else ""


def switch_state(outer, own, level, kind):
    """``(shown, state, why)`` for one quick toggle at ``level``.

    ``state`` says what a click does: ``on`` - access is there, a click turns
    it off; ``off`` - this level switched it off, a click turns it back on;
    ``default`` - off by the outer level's default, a click turns it on for
    this level alone; ``locked`` - off with nothing to go back to, a click
    opens the window instead.
    """
    effective = resolve(list(outer) + [(level, own)])
    own = own or {}
    mine = kind in (own.get("switched_off") or [])
    parent = resolve(list(outer))
    where = _outer_word(outer)
    if kind == "network":
        shown = NETWORK_SHORT[effective["network"]]
        if mine:
            return shown, "off", "Network is switched off here. Click to turn it back on."
        if effective["network"] == "on":
            return shown, "on", "Network is on: the host's own network. Click to turn it off."
        if effective["network"] == "isolated":
            return shown, "on", ("Network is isolated: ports of its own, only the published ones reach this "
                                 "computer. Click to turn it off.")
        if network_value(own.get("network")) == "off":
            return shown, "off", "Network is set to off here. Click to turn it on."
        return shown, "default", ("Network is off by the %s's default. Click to turn it on for this %s."
                                  % (where, level))
    shown = FILE_SHORT[effective["files"]]
    if mine:
        back = resolve(list(outer) + [(level, toggled(own, "files"))])["files"]
        return shown, "off", "File access is switched off here. Click to restore %s." % FILE_WORDS[back].lower()
    if effective["files"] != "none":
        words = {"all": "No file restrictions", "read_write": "The project folder, read and write",
                 "read_only": "The project folder, read-only"}[effective["files"]]
        return shown, "on", "%s. Click to turn file access off." % words
    if own.get("files") == "none":
        if outer and parent["files"] != "none":
            return shown, "off", "File access is set to none here. Click to use the %s's: %s." % (
                where, FILE_WORDS[parent["files"]].lower())
        return shown, "locked", "File access is set to none. Choose what to allow in Restrictions."
    restore = parent["configured"]["files"]
    if outer and restore != "none":
        return shown, "default", ("File access is off by the %s's default. Click to allow %s for this %s."
                                  % (where, FILE_WORDS[restore].lower(), level))
    return shown, "locked", "File access is off. Choose what to allow in Restrictions."


def click(outer, own, level, kind):
    """What one click on Network or Files writes at ``level``, or None when
    the window has to ask instead."""
    _shown, state, _why = switch_state(outer, own, level, kind)
    result = clean(own)
    if state == "locked":
        return None
    if state == "on":
        return toggled(result, kind)
    if kind in (result.get("switched_off") or []):
        return toggled(result, kind)
    parent = resolve(list(outer))
    if kind == "network":
        # Back to the default when it lets the network through - at the top
        # level it always does; otherwise what the default was configured to.
        if not outer or parent["network"] != "off":
            result.pop("network", None)
        else:
            configured = parent["configured"]["network"]
            result["network"] = configured if configured != "off" else "on"
        return result
    if result.get("files") == "none":
        result.pop("files", None)
        return result
    result["files"] = parent["configured"]["files"]
    return result


def pending(saved, current):
    """Which indicators a change touches, saved mapping against current one."""
    saved, current = saved or {}, current or {}
    found = []
    def network_of(raw):
        return (network_value(raw.get("network")), "network" in (raw.get("switched_off") or []),
                raw.get("outbound"), raw.get("host_loopback"), raw.get("publish"))
    if network_of(saved) != network_of(current):
        found.append("network")
    if (saved.get("files"), saved.get("folders"), "files" in (saved.get("switched_off") or [])) != (
            current.get("files"), current.get("folders"), "files" in (current.get("switched_off") or [])):
        found.append("files")
    if saved.get("limits") != current.get("limits"):
        found.append("limits")
    return tuple(found)


def problems(raw, level):
    """What is wrong with one stored level, before the core is asked."""
    found = []
    raw = raw or {}
    if not isinstance(raw, dict):
        return ["Restrictions must be a mapping."]
    if network_value(raw.get("network", "on")) not in NETWORK:
        found.append("Network is on, isolated or off.")
    hosts = set()
    for one in raw.get("publish") or []:
        inner, host = (one or {}).get("internal_port"), (one or {}).get("host_port", "auto")
        if type(inner) is not int or not 1 <= inner <= 65535:
            found.append("A published port needs the service's port number, 1 to 65535.")
        if host != "auto" and (type(host) is not int or not 1 <= host <= 65535):
            found.append("The port on this computer is a number from 1 to 65535, or auto.")
        address = (one or {}).get("host_ip", "127.0.0.1")
        if address not in LOOPBACK and not (one or {}).get("public"):
            found.append("Publishing on %s needs a yes to being reachable from other computers." % address)
        if (one or {}).get("public") and not raw.get("outbound"):
            found.append("Publishing to other computers needs Internet on for this isolated network.")
        if host != "auto":
            if (address, host) in hosts:
                found.append("Port %s on this computer is published twice." % host)
            hosts.add((address, host))
    if raw.get("files", "all") not in FILES:
        found.append("Files is one of %s." % ", ".join(FILES))
    for key, value in (raw.get("limits") or {}).items():
        if key not in LIMITS or type(value) not in (int, float) or value <= 0:
            found.append("%s must be a positive number." % LIMIT_WORDS.get(key, key))
    for one in raw.get("folders") or []:
        path = (one or {}).get("path", "")
        if not isinstance(path, str) or not (path.startswith("/") or path.startswith("~")):
            found.append("Folder %r must be an absolute path." % path)
    return found


# -- asking the core ----------------------------------------------------------------
_capabilities = {}


def _command(core, *arguments):
    from .runtime_services import command
    return command(core, *arguments)


def capabilities(core):
    """What this computer can enforce, asked once per core."""
    key = tuple(getattr(core, "prefix", ()) or ())
    if key in _capabilities:
        return _capabilities[key]
    done = subprocess.run(_command(core, "--restrictions-check"), capture_output=True, text=True,
                          timeout=30, cwd=core.spawn_dir(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        answer = json.loads(done.stdout)["capabilities"]
    except (ValueError, KeyError, TypeError):
        raise ValueError("This core cannot report restrictions. Select a current core in Settings.") from None
    _capabilities[key] = answer
    return answer


def preview(core, request):
    """The core's answer for one program: resolved restrictions and every problem."""
    done = subprocess.run(_command(core, "--restrictions-preview"), input=json.dumps(request),
                          capture_output=True, text=True, timeout=45, cwd=core.spawn_dir(),
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        answer = json.loads(done.stdout)
        if not isinstance(answer, dict) or "ok" not in answer:
            raise ValueError()
        return answer
    except ValueError:
        raise ValueError("This core cannot check restrictions. Select a current core in Settings.") from None


def exec_argv(core, request, argv):
    """The command that starts ``argv`` held to ``request``, through the core."""
    return _command(core, "--restricted-exec=" + json.dumps(request, separators=(",", ":")), "--", *argv)


def request(folder, layers, working_directory="", command=(), network_state=""):
    return {"project_folder": folder, "layers": [[name, clean(raw)] for name, raw in layers],
            **({"working_directory": working_directory} if working_directory else {}),
            **({"command": list(command)} if command else {}),
            **({"network_state": network_state} if network_state else {})}


def network_state_path(project, name):
    """Where a service's isolated network says what it publishes while it runs.
    In the user's own cache folder, one file per service of a project."""
    import hashlib
    from PySide6.QtCore import QStandardPaths
    root = QStandardPaths.writableLocation(QStandardPaths.GenericCacheLocation) or os.path.expanduser("~/.cache")
    key = hashlib.sha256(("%s\0%s" % (project, name)).encode("utf-8")).hexdigest()[:20]
    return os.path.join(root, "qavector", "network", key + ".json")


def read_publications(path):
    """What a running isolated service publishes, as its state file says; []."""
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
        os.kill(int(state["pid"]), 0)
        return [one for one in state.get("publications") or [] if isinstance(one, dict)]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def change_publication(core, path, operation, publication):
    """``add``/``remove`` one publication of a running service through the core."""
    done = subprocess.run(_command(core, "--network-ports"),
                          input=json.dumps({"state": path, "op": operation, "publication": publication}),
                          capture_output=True, text=True, timeout=30, cwd=core.spawn_dir(),
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        answer = json.loads(done.stdout)
    except ValueError:
        raise ValueError("This core cannot publish ports. Select a current core in Settings.") from None
    if not answer.get("ok"):
        raise ValueError("; ".join(one.get("message", "") for one in answer.get("problems") or []) or "Not done.")
    return answer


# -- widgets ------------------------------------------------------------------------
def _repolish(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class FolderButton(QPushButton):
    """Beside a folder field: what the folder holds its programs to, one click away."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("variant", "restrictions")
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName("Restrictions")
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.set_state({}, enabled=False)

    def set_state(self, effective, enabled=True, why="", pending=False):
        restricted = bool((effective or {}).get("restricted"))
        icons.button(self, "lock" if restricted else "unlock",
                     (short_summary(effective) if enabled else "Restrictions") + (" *" if pending else ""))
        self.setProperty("restricted", "true" if restricted else "false")
        self.setEnabled(enabled)
        text = summary(effective) if enabled else why
        self.setToolTip(text + ("\nNot saved yet." if pending and enabled else ""))
        _repolish(self)


#: The gap between two quick toggles, in px.
TOGGLE_GAP = 4


class QuickToggles(QWidget):
    """Network / Files / Limits for one service or step, as they are now.

    Each toggle as wide as what it says and no wider, one gap between them:
    widths kept for the widest word each could show put the marks at uneven
    distances, lit a box twice the word's width on hover and left a gap after
    the last one. A table keeps its later columns in line by other means - see
    the services page's row strip.
    """

    toggle = Signal(str)            # "network" or "files"
    limits_requested = Signal(object)  # the button, to anchor a popover
    #: The ports button, to anchor the menu of what a running isolated
    #: service publishes - and a way to publish one more.
    ports_requested = Signal(object)
    open_window = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(TOGGLE_GAP)
        self.network = self._button("Network", "network")
        self.files = self._button("Files", "folder")
        self.limits = self._button("Limits", "limits")
        self.ports = self._button("Published ports", "popout")
        for button in (self.network, self.files, self.limits, self.ports):
            layout.addWidget(button)
        self.ports.hide()
        self.ports.clicked.connect(lambda: self.ports_requested.emit(self.ports))
        self.network.clicked.connect(lambda: self._clicked("network"))
        self.files.clicked.connect(lambda: self._clicked("files"))
        self.limits.clicked.connect(lambda: self.limits_requested.emit(self.limits))
        self._states = {}
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda _point: self.open_window.emit())

    def _button(self, name, glyph):
        button = QToolButton(self)
        button.setProperty("variant", "restriction")
        button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        button.setAccessibleName(name)
        button.setCursor(Qt.PointingHandCursor)
        button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        icons.button(button, glyph, "")
        return button

    def set_ports(self, publications, live):
        """What a running isolated service publishes: ``:18069`` (+ how many more).
        Hidden while it is not running isolated. True when that changed how
        wide the toggles are, so a table holding them can measure again."""
        before = (self.ports.isHidden(), self.ports.text())
        self.ports.setVisible(bool(live))
        if not live:
            return before[0] is False
        if publications:
            first = publications[0]
            text = ":%s" % first["host_port"] + (" +%d" % (len(publications) - 1) if len(publications) > 1 else "")
            tip = "\n".join(publication_words(one) for one in publications)
        else:
            text, tip = "Ports", "Nothing published yet."
        icons.button(self.ports, "popout", text)
        self.ports.setProperty("state", "on" if publications else "locked")
        self.ports.setToolTip("Published from its isolated network:\n" + tip + "\nClick to open, copy or publish.")
        _repolish(self.ports)
        return before != (False, text)

    def _clicked(self, kind):
        if self._states.get(kind) == "locked":
            self.open_window.emit()
        else:
            self.toggle.emit(kind)

    def set_model(self, outer, own, level, pending=(), limit_note=""):
        effective = resolve(list(outer) + [(level, own)])
        for kind, button in (("network", self.network), ("files", self.files)):
            shown, state, why = switch_state(outer, own, level, kind)
            self._states[kind] = state
            glyph = network_icon(shown) if kind == "network" else "folder" + ("-off" if shown == "Off" else "")
            icons.button(button, glyph, shown)
            # Off here is drawn as off; off by the outer default, or with
            # nothing to go back to, is muted - this level did not decide it.
            button.setProperty("state", {"off": "off", "on": "on"}.get(state, "locked"))
            button.setProperty("pending", "true" if kind in pending else "false")
            button.setToolTip(why + ("\nNot saved yet." if kind in pending else ""))
            button.setAccessibleDescription(why)
            _repolish(button)
        limits = effective["limits"]
        icons.button(self.limits, "limits", limits_words(limits, short=True) if limits else "None")
        self.limits.setProperty("state", "on")
        self.limits.setProperty("pending", "true" if "limits" in pending else "false")
        self.limits.setToolTip(("Limits: " + limits_words(limits) if limits else "No limits.")
                               + "\nClick to change." + ("\n" + limit_note if limit_note else ""))
        _repolish(self.limits)
        return effective


class LimitsEditor(QWidget):
    """CPU cores, memory and processes; empty means no limit at this level."""

    changed = Signal()

    def __init__(self, own=None, outer_limits=None, available=None, parent=None):
        super().__init__(parent)
        own = own or {}
        outer_limits = outer_limits or {}
        available = available or {}
        grid = QVBoxLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)
        self.fields = {}
        for key in LIMITS:
            edit = QLineEdit()
            edit.setAccessibleName(LIMIT_WORDS[key])
            edit.setFixedWidth(96)
            unit = None
            value = own.get(key)
            if key == "memory_mb":
                unit = QComboBox()
                unit.addItem("MB", 1)
                unit.addItem("GB", 1024)
                unit.setFixedWidth(64)
                if value and value % 1024 == 0:
                    unit.setCurrentIndex(1)
                    edit.setText(str(value // 1024))
                elif value:
                    edit.setText(str(value))
                else:
                    unit.setCurrentIndex(1)
                unit.currentIndexChanged.connect(self.changed.emit)
            elif value is not None:
                edit.setText(("%g" % value) if key == "cpu_cores" else str(int(value)))
            ceiling = outer_limits.get(key)
            edit.setPlaceholderText(("Default %s" % (memory_words(ceiling) if key == "memory_mb" else
                                    ("%g" % ceiling))) if ceiling else "No limit")
            state = available.get(key) or {"available": True}
            note = QLabel(state.get("reason", "") if not state.get("available", True) else "")
            note.setProperty("role", "hint")
            note.setWordWrap(True)
            if not state.get("available", True) and not value:
                edit.setEnabled(False)
                if unit is not None:
                    unit.setEnabled(False)
            edit.textChanged.connect(self.changed.emit)
            row = QHBoxLayout()
            row.setSpacing(6)
            label = QLabel(LIMIT_WORDS[key])
            label.setFixedWidth(84)
            row.addWidget(label)
            row.addWidget(edit)
            if unit is not None:
                row.addWidget(unit)
            row.addStretch(1)
            holder = QWidget()
            holder_layout = QVBoxLayout(holder)
            holder_layout.setContentsMargins(0, 0, 0, 0)
            holder_layout.setSpacing(2)
            holder_layout.addLayout(row)
            if note.text():
                holder_layout.addWidget(note)
            grid.addWidget(holder)
            self.fields[key] = (edit, unit)

    def value(self):
        """``{key: number}`` for the limits set; raises ValueError with a readable reason."""
        result = {}
        for key, (edit, unit) in self.fields.items():
            text = edit.text().strip().replace(",", ".")
            if not text:
                continue
            try:
                number = float(text)
            except ValueError:
                raise ValueError("%s must be a number." % LIMIT_WORDS[key]) from None
            if number <= 0:
                raise ValueError("%s must be more than zero." % LIMIT_WORDS[key])
            if key == "memory_mb":
                number = int(round(number * unit.currentData()))
                if number < 16:
                    raise ValueError("Memory must be at least 16 MB.")
            elif key == "processes":
                if number != int(number) or number < 8:
                    raise ValueError("Processes must be a whole number, at least 8.")
                number = int(number)
            result[key] = number
        return result


def limits_menu(anchor, outer, own, level, available, apply, open_window, where=None):
    """The small popover behind the Limits toggle - under ``anchor``, or at ``where``."""
    menu = QMenu(anchor)
    body = QWidget()
    column = QVBoxLayout(body)
    column.setContentsMargins(12, 10, 12, 10)
    column.setSpacing(8)
    outer_limits = resolve(list(outer))["limits"] if outer else {}
    editor = LimitsEditor((own or {}).get("limits"), outer_limits, available)
    column.addWidget(editor)
    error = QLabel("")
    error.setProperty("role", "error")
    error.setWordWrap(True)
    column.addWidget(error)
    buttons = QHBoxLayout()
    more = QPushButton("Restrictions…")
    more.setProperty("variant", "ghost")
    use = QPushButton("Apply")
    use.setProperty("variant", "primary")
    buttons.addWidget(more)
    buttons.addStretch(1)
    buttons.addWidget(use)
    column.addLayout(buttons)

    def commit():
        try:
            values = editor.value()
        except ValueError as exc:
            error.setText(str(exc))
            return
        result = clean(own)
        if values:
            result["limits"] = values
        else:
            result.pop("limits", None)
        menu.close()
        apply(result)

    use.clicked.connect(commit)
    more.clicked.connect(lambda: (menu.close(), open_window()))
    action = QWidgetAction(menu)
    action.setDefaultWidget(body)
    menu.addAction(action)
    menu.popup(where if where is not None else anchor.mapToGlobal(anchor.rect().bottomLeft()))
    return menu


class RestrictionsWindow(QDialog):
    """One level's restrictions, in full: network, files and folders, limits.

    ``outer`` is the levels above (``[]`` for a project or a cycle); a choice
    wider than they allow is offered greyed out, with the reason. ``probe`` is
    the program this level would start, if there is one to check - the core
    then also says which tool folders it adds and whether it could start.
    """

    def __init__(self, title, folder, level, own=None, outer=(), outer_name="", core=None,
                 probe=None, parent=None, writable=True, steps=None):
        super().__init__(parent)
        self.setWindowTitle("Restrictions · " + title)
        self.frame = widgets.dress(self)
        self.setMinimumWidth(620)
        self.level, self.outer, self.outer_name = level, list(outer), outer_name
        self.core, self.probe, self.folder = core, probe or {}, folder
        self._own = clean(own)
        self._writable = writable
        self._generation = 0
        self.saved = None

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(20, 18, 20, 16)
        outer_layout.setSpacing(10)
        # The form scrolls, the result and the buttons do not: on a short
        # screen what is wrong and how to leave stay in view.
        from PySide6.QtWidgets import QFrame, QScrollArea
        self._body = QWidget()
        column = QVBoxLayout(self._body)
        column.setContentsMargins(0, 0, 8, 0)
        column.setSpacing(10)
        self._area = QScrollArea()
        self._area.setWidget(self._body)
        self._area.setWidgetResizable(True)
        self._area.setFrameShape(QFrame.NoFrame)
        outer_layout.addWidget(widgets.heading("Restrictions"))
        where = widgets.elided_mono(folder)
        where.setToolTip(folder)
        outer_layout.addWidget(where)
        outer_layout.addWidget(self._area, 1)
        if self.outer:
            parent_effective = resolve(self.outer)
            note = QLabel("Defaults from the %s: %s. Anything set here replaces them for this %s only." % (
                outer_name, summary(parent_effective), level))
            note.setProperty("role", "lede")
            note.setWordWrap(True)
            column.addWidget(note)
        else:
            parent_effective = resolve([])

        # What a quick button switched off shows as off, like anything else
        # that is off. The value configured under it stays stored beside it
        # and is kept unless that choice is changed here.
        self._switched = set(self._own.get("switched_off") or [])
        self._fitted = []           # tables sized to their rows once shown
        same = "Same as %s" % outer_name if self.outer else None
        network_options = ([same] if same else []) + ["On", "Isolated", "Off"]
        wanted = network_value(self._own.get("network"))
        current = "Off" if "network" in self._switched else \
            {"off": "Off", "on": "On", "isolated": "Isolated"}.get(wanted, same or "On")
        self.network = widgets.Segmented(network_options, current)
        column.addWidget(widgets.field("Network", self.network,
            "On is the host's own network: ports, local databases, the internet. Isolated is a network of its "
            "own: its ports cannot clash with anything, and it reaches only what is allowed below. "
            "Off is nothing at all - not even this computer."))
        column.addWidget(self._isolation_panel())

        file_options = ([same] if same else []) + [FILE_WORDS[one] for one in FILES]
        wanted = self._own.get("files")
        self.files = widgets.Segmented(file_options, FILE_WORDS["none"] if "files" in self._switched else
                                       FILE_WORDS[wanted] if wanted in FILES else (same or FILE_WORDS["all"]))
        self._shown = {"network": self.network.current(), "files": self.files.current()}
        column.addWidget(widgets.field("Files", self.files,
            "Read and write, Read-only and No access hold the program to the project folder and the folders below. "
            "System folders stay readable so programs start; the rest of your home folder is hidden."))

        self.folders = widgets.Table(0, 2)
        self.folders.setHorizontalHeaderLabels(["Folder", "Access"])
        self.folders.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.folders.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self.folders.setColumnWidth(1, 150)
        self.folders.verticalHeader().hide()
        self._fitted.append((self.folders, 3))
        widgets.empty_note(self.folders, "No other folders. Add one the program needs outside the project folder.")
        for one in self._own.get("folders") or []:
            self._add_folder(one.get("path", ""), one.get("access", "read_only"))
        add = QPushButton("Add folder…")
        add.setProperty("variant", "ghost")
        add.clicked.connect(self._pick_folder)
        remove = QPushButton("Remove")
        remove.setProperty("variant", "ghost")
        remove.clicked.connect(self._remove_folder)
        folder_box = QWidget()
        folder_layout = QVBoxLayout(folder_box)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        folder_layout.setSpacing(4)
        folder_layout.addWidget(self.folders)
        folder_layout.addWidget(widgets.row(add, remove, None))
        self.auto = QLabel("")
        self.auto.setProperty("role", "hint")
        self.auto.setWordWrap(True)
        folder_layout.addWidget(self.auto)
        self.folder_field = widgets.field("Other folders", folder_box,
            "Outside the project folder, nothing is visible unless it is listed here." +
            (" Leave empty to use the %s's folders." % outer_name if self.outer else ""))
        column.addWidget(self.folder_field)

        self.capabilities = {}
        self.limits_box = QVBoxLayout()
        self.limits_holder = QWidget()
        self.limits_holder.setLayout(self.limits_box)
        self.limits = LimitsEditor(self._own.get("limits"), parent_effective["limits"])
        self.limits_box.addWidget(self.limits)
        column.addWidget(widgets.field("Limits", self.limits_holder,
            "Empty means %s." % ("the %s's limit" % outer_name if self.outer else "no limit")))

        if steps is not None:
            # A cycle's window also says which steps narrow it further and which
            # are system steps, so nothing runs unrestricted without being shown.
            table = widgets.Table(len(steps), 2)
            table.setHorizontalHeaderLabels(["Step", "Holds to"])
            table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
            table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
            table.setColumnWidth(0, 200)
            table.verticalHeader().hide()
            table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            for index, (label, words) in enumerate(steps):
                table.setItem(index, 0, QTableWidgetItem(label))
                table.setItem(index, 1, QTableWidgetItem(words))
            self._fitted.append((table, 6))
            widgets.empty_note(table, "No steps.")
            column.addWidget(widgets.field("Steps", table,
                "Each step gets the cycle's restrictions unless it narrows them in its own window. "
                "System steps run without them."))

        self.summary = QLabel("")
        self.summary.setProperty("role", "summary")
        self.summary.setWordWrap(True)
        outer_layout.addWidget(self.summary)
        self.problem = QLabel("")
        self.problem.setProperty("role", "error")
        self.problem.setWordWrap(True)
        outer_layout.addWidget(self.problem)
        column.addStretch(1)

        from PySide6.QtWidgets import QDialogButtonBox
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Apply")
        self.buttons.button(QDialogButtonBox.Ok).setProperty("variant", "primary")
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        outer_layout.addWidget(self.buttons)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._ask_core)
        for signal in (self.network.changed, self.files.changed, self.limits.changed,
                       self.internet.toggled, self.this_computer.toggled):
            signal.connect(self._changed)
        self.folders.itemChanged.connect(self._changed)
        self.published.itemChanged.connect(self._changed)
        if not writable:
            for widget in (self.network, self.files, self.folders, add, remove, self.limits,
                           self.internet, self.this_computer, self.published, self.publish_add, self.publish_remove):
                widget.setEnabled(False)
            self.buttons.button(QDialogButtonBox.Ok).setEnabled(False)
        self._load_capabilities()
        self._changed()
        self.resize(680, 720)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._fit_tables)

    def _fit_tables(self):
        """Each table as tall as its own header and rows, up to a few rows.

        Measured once the stylesheet has given them their real sizes: a height
        guessed before that clipped the last row under the hint below it.
        """
        for table, most in self._fitted:
            row = table.rowHeight(0) if table.rowCount() else table.verticalHeader().defaultSectionSize()
            shown = min(most, max(2, table.rowCount()))
            table.setFixedHeight(table.horizontalHeader().height() + shown * row + 2 * table.frameWidth() + 2)
        # Then the window: as tall as the form, never taller than the screen.
        screen = self.screen()
        available = screen.availableGeometry().height() if screen else 900
        wanted = self._body.sizeHint().height() + self.height() - self._area.viewport().height() + 4
        self.resize(self.width(), min(max(wanted, 420), int(available * 0.9)))

    # -- folders ------------------------------------------------------------------
    def _add_folder(self, path, access):
        row = self.folders.rowCount()
        self.folders.insertRow(row)
        self.folders.setItem(row, 0, QTableWidgetItem(path))
        combo = QComboBox()
        combo.addItem("Read-only", "read_only")
        combo.addItem("Read & write", "read_write")
        combo.setCurrentIndex(max(0, combo.findData(access)))
        combo.currentIndexChanged.connect(self._changed)
        self.folders.setCellWidget(row, 1, combo)

    def _pick_folder(self):
        path = widgets.pick_path(self, "Folder the program may use", self.folder or "~", directory=True)
        if path:
            home = os.path.expanduser("~")
            if path == home or path.startswith(home + os.sep):
                path = "~" + path[len(home):]
            self._add_folder(path, "read_only")
            self._changed()

    def _remove_folder(self):
        row = self.folders.currentRow()
        if row >= 0:
            self.folders.removeRow(row)
            self._changed()

    def _folder_rows(self):
        rows = []
        for index in range(self.folders.rowCount()):
            item = self.folders.item(index, 0)
            path = (item.text() if item else "").strip()
            if path:
                rows.append({"path": path, "access": self.folders.cellWidget(index, 1).currentData()})
        return rows

    # -- an isolated network ---------------------------------------------------------
    def _isolation_panel(self):
        """What an isolated network lets through: the internet, this computer's
        services, and the ports it publishes here. Shown while it is isolated."""
        parent = resolve(self.outer)
        mine = resolve(self.outer + [(self.level, self._own)])
        configured = mine["configured"]["network"] == "isolated" or parent["network"] == "isolated"
        self.internet = QCheckBox("Internet - names resolve and connections go out")
        self.internet.setChecked(bool(self._own.get("outbound", parent["outbound"] if configured else False)))
        self.this_computer = QCheckBox("Services on this computer - a database, another service's published port")
        self.this_computer.setChecked(bool(self._own.get("host_loopback", parent["host_loopback"] if configured else False)))
        self.published = widgets.Table(0, 3)
        self.published.setHorizontalHeaderLabels(["Service port", "On this computer", "Reachable from"])
        self.published.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.published.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.published.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)
        self.published.setColumnWidth(2, 170)
        self.published.verticalHeader().hide()
        self._fitted.append((self.published, 4))
        widgets.empty_note(self.published, "Nothing published: the service's ports stay inside its network.")
        for one in self._own.get("publish") or []:
            self._add_publication(one)
        self.publish_add = QPushButton("Publish a port…")
        self.publish_add.setProperty("variant", "ghost")
        self.publish_add.clicked.connect(lambda: (self._add_publication({"internal_port": 8069}), self._changed()))
        self.publish_remove = QPushButton("Remove")
        self.publish_remove.setProperty("variant", "ghost")
        self.publish_remove.clicked.connect(self._remove_publication)
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.internet)
        layout.addWidget(self.this_computer)
        ports = QWidget()
        ports_layout = QVBoxLayout(ports)
        ports_layout.setContentsMargins(0, 6, 0, 0)
        ports_layout.setSpacing(4)
        ports_layout.addWidget(self.published)
        ports_layout.addWidget(widgets.row(self.publish_add, self.publish_remove, None))
        # Ports are a service's own: a project publishing one port for all its
        # services would publish it twice.
        self.ports_field = widgets.field("Published ports", ports,
            "Auto takes a free port on this computer when the service starts; the address is on its row. "
            "This computer means 127.0.0.1 only.")
        layout.addWidget(self.ports_field)
        self.ports_field.setVisible(self.level == "service")
        self.isolation_field = widgets.field("Isolated network lets through", box, "")
        return self.isolation_field

    def _add_publication(self, one):
        row = self.published.rowCount()
        self.published.insertRow(row)
        self.published.setItem(row, 0, QTableWidgetItem(str(one.get("internal_port", ""))))
        self.published.setItem(row, 1, QTableWidgetItem(str(one.get("host_port", "auto"))))
        reach = QComboBox()
        reach.addItem("This computer", "local")
        reach.addItem("Other computers", "public")
        reach.setCurrentIndex(1 if one.get("public") else 0)
        reach.setProperty("address", one.get("host_ip", "127.0.0.1"))
        reach.activated.connect(lambda index, combo=reach: self._reach_changed(combo, index))
        self.published.setCellWidget(row, 2, reach)

    def _reach_changed(self, combo, index):
        if combo.itemData(index) == "public":
            answer = QMessageBox.question(self, "Reachable from other computers",
                "Anyone on your network will be able to connect to this port. Publishing does not add a "
                "password or encryption. Publish it to other computers?")
            if answer != QMessageBox.Yes:
                combo.setCurrentIndex(0)
        self._changed()

    def _remove_publication(self):
        row = self.published.currentRow()
        if row >= 0:
            self.published.removeRow(row)
            self._changed()

    def _publications(self):
        rows = []
        for index in range(self.published.rowCount()):
            inner = (self.published.item(index, 0).text() if self.published.item(index, 0) else "").strip()
            host = (self.published.item(index, 1).text() if self.published.item(index, 1) else "").strip() or "auto"
            if not inner:
                continue
            if not inner.isdigit() or not (host == "auto" or host.isdigit()):
                raise ValueError("A published port is a number; on this computer, a number or auto.")
            entry = {"internal_port": int(inner), "host_port": host if host == "auto" else int(host)}
            reach = self.published.cellWidget(index, 2)
            if reach.currentData() == "public":
                entry.update(host_ip="0.0.0.0", public=True)
            elif reach.property("address") not in (None, "127.0.0.1"):
                entry["host_ip"] = reach.property("address")
            rows.append(entry)
        return rows

    # -- the value ----------------------------------------------------------------
    def value(self):
        """The stored mapping for this level; raises ValueError when a field is wrong."""
        result = {}
        # Switched off and left as shown: the configured value under it stays.
        kept = {kind for kind in self._switched if getattr(self, kind).current() == self._shown[kind]}
        chosen = self.network.current()
        if "network" in kept:
            if self._own.get("network") is not None:
                result["network"] = network_value(self._own["network"])
        elif chosen == "Off":
            result["network"] = "off"
        elif chosen == "Isolated":
            result["network"] = "isolated"
        elif chosen == "On" and self.outer:
            result["network"] = "on"
        # What isolation lets through, written where this level differs from
        # the one around it, and kept while the network is something else.
        parent = resolve(self.outer)
        for key, box in (("outbound", self.internet), ("host_loopback", self.this_computer)):
            if box.isChecked() != parent[key]:
                result[key] = box.isChecked()
        if self.level == "service":
            publications = self._publications()
            if publications:
                result["publish"] = publications
        chosen = self.files.current()
        if "files" in kept:
            if self._own.get("files") in FILES:
                result["files"] = self._own["files"]
        else:
            for key, words in FILE_WORDS.items():
                if words == chosen and (key != "all" or self.outer):
                    result["files"] = key
        folders = self._folder_rows()
        if folders:
            result["folders"] = folders
        limits = self.limits.value()
        if limits:
            result["limits"] = limits
        if kept:
            result["switched_off"] = [one for one in SWITCHES if one in kept]
        return result

    def _changed(self, *_):
        try:
            value = self.value()
            local = problems(value, self.level)
        except ValueError as exc:
            value, local = None, [str(exc)]
        effective = resolve(self.outer + [(self.level, value or {})])
        self.folder_field.setVisible(effective["configured"]["files"] != "all")
        self.isolation_field.setVisible(effective["network"] == "isolated")
        self.summary.setText("Result: " + summary(effective))
        self._local = local
        self._show(local)
        if value is not None and self.core is not None:
            self._timer.start(250)

    def _show(self, found):
        from PySide6.QtWidgets import QDialogButtonBox
        self.problem.setText("\n".join(found[:4]))
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(self._writable and not found)

    def _load_capabilities(self):
        if self.core is None:
            return
        core = self.core

        def done(result, error):
            if error or not result:
                return
            self.capabilities = result
            isolation = result.get("isolated_network") or {}
            if not isolation.get("available", False):
                button = self.network.button("Isolated")
                if button is not None and self.network.current() != "Isolated":
                    button.setEnabled(False)
                    button.setToolTip(isolation.get("reason") or "An isolated network is not available here.")
            if not result.get("available"):
                self.auto.setText("This computer cannot enforce file or network restrictions: %s" % result.get("reason", ""))
            old = self.limits
            try:
                kept = old.value()
            except ValueError:
                kept = (self._own.get("limits") or {})
            self.limits = LimitsEditor(kept, resolve(self.outer)["limits"], result.get("limits") or {})
            self.limits.changed.connect(self._changed)
            self.limits_box.replaceWidget(old, self.limits)
            old.deleteLater()
            self._changed()
        background.run(lambda: capabilities(core), done, owner=self)

    def _ask_core(self):
        try:
            value = self.value()
        except ValueError:
            return
        self._generation += 1
        generation, core = self._generation, self.core
        asked = request(self.probe.get("folder", self.folder), self.outer + [(self.level, value)],
                        self.probe.get("cwd", ""), self.probe.get("command", ()))

        def done(answer, error):
            if generation != self._generation:
                return
            if error:
                self._show(self._local + [str(error)])
                return
            tools = answer.get("tool_folders") or []
            if tools and answer.get("effective", {}).get("files") not in ("all", "none"):
                home = os.path.expanduser("~")
                lines = ["Added automatically for the program it runs:"] + [
                    "%s - %s" % (one["path"].replace(home, "~", 1), one.get("reason", "")) for one in tools]
                self.auto.setText("\n".join(lines))
            elif self.capabilities.get("available", True):
                self.auto.setText("")
            found = [one.get("message", "") for one in answer.get("problems") or []]
            self._show(self._local + found)
        background.run(lambda: preview(core, asked), done, owner=self)

    def _accept(self):
        try:
            self.saved = clean(self.value())
        except ValueError as exc:
            self._show([str(exc)])
            return
        self.accept()
