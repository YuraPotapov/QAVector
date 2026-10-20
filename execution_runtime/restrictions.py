"""Restrictions set on a project folder: network, files and resource limits.

This is the model a person edits. A services project or a cycle names its
project folder and may carry one restrictions object - the defaults for what
runs there; a service in that project, or a step in that cycle, may carry
another that sets its own values instead. Nothing has
to be created first - no environment, no image - and a folder without
restrictions runs exactly as it always has.

Three independent dimensions, each with a plain vocabulary:

- ``network``: ``on`` (the host's own network: ports, local databases),
  ``isolated`` (a network of its own: its ports cannot clash with anything,
  ``publish`` puts chosen ones on this computer, ``outbound`` lets it reach the
  internet and ``host_loopback`` the services on this computer) or ``off``.
- ``files``: ``all`` (no file restrictions), or the program is confined to the
  project folder - ``read_write``, ``read_only`` or ``none`` - plus the
  ``folders`` explicitly listed, while the system folders stay readable so
  programs still start.
- ``limits``: ``cpu_cores``, ``memory_mb`` and ``processes``; absent means no
  limit.

``switched_off`` is what a quick toggle writes. Turning network or files off
does not throw away what was configured: the configured value stays where it
is and ``switched_off`` overrides it, so the next click restores exactly that.
(Not ``off``: YAML 1.1 reads an unquoted ``off`` as false, key or value - and
for the same reason an unquoted ``network: off`` is accepted as meaning off.)

Only resolution happens here. Nothing touches the host: the Linux launcher
(``backends/linux/restricted.py``) turns a resolved result into a sandbox, and a
missing capability there refuses the launch rather than running unrestricted.
"""

import math
import os
from pathlib import PurePosixPath

from .errors import RuntimePolicyError

KEYS = ("network", "outbound", "host_loopback", "publish", "files", "folders", "limits", "switched_off")
NETWORK = ("on", "isolated", "off")
#: The addresses a publication may bind without saying it means to be public.
LOOPBACK = ("127.0.0.1", "::1")
MAX_PUBLISH = 32
#: Widest first: narrowing moves to the right.
FILES = ("all", "read_write", "read_only", "none")
ACCESS = ("read_write", "read_only")
LIMITS = ("cpu_cores", "memory_mb", "processes")
SWITCHES = ("network", "files")
MAX_FOLDERS = 64

_FILE_WORDS = {"read_write": "Project read and write", "read_only": "Project read-only",
               "none": "No file access"}


def _fail(message, field, layer=""):
    raise RuntimePolicyError("invalid_restrictions", message, field, layer)


def folder_path(value, home=None, field="restrictions.folders.path"):
    """An absolute, normalised folder path; ``~`` is the user's home."""
    if not isinstance(value, str) or not value.strip() or "\0" in value or len(value) > 4096:
        _fail("A folder needs a path.", field)
    text = value.strip()
    if text == "~" or text.startswith("~/"):
        text = (home or os.path.expanduser("~")) + text[1:]
    path = PurePosixPath(text)
    if not path.is_absolute() or ".." in path.parts:
        _fail("Folder paths must be absolute (or start with ~/).", field)
    path = str(PurePosixPath(os.path.normpath(text)))
    if path == "/":
        _fail("The whole disk cannot be listed as one folder.", field)
    return path


def within(parent, child):
    """``child`` is ``parent`` or somewhere below it."""
    a, b = PurePosixPath(parent), PurePosixPath(child)
    return a == b or a in b.parents


def parse(raw, field="restrictions", layer=""):
    """Check one stored restrictions object and return a normalised copy.

    ``None`` and ``{}`` both mean "nothing set here". Folder paths are kept as
    written (``~/...`` included) so a file stays portable between machines;
    :func:`resolve` expands them.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        _fail("Restrictions must be a mapping.", field, layer)
    unknown = sorted(set(raw) - set(KEYS))
    if unknown:
        _fail("Unknown restriction %r; use %s." % (unknown[0], ", ".join(KEYS)), field, layer)
    result = {}
    if "network" in raw:
        network = {True: "on", False: "off"}.get(raw["network"], raw["network"]) \
            if isinstance(raw["network"], bool) else raw["network"]
        if network not in NETWORK:
            _fail("Network is on, isolated or off.", field + ".network", layer)
        result["network"] = network
    for key in ("outbound", "host_loopback"):
        if key in raw:
            if not isinstance(raw[key], bool):
                _fail("%s is true or false." % key, field + "." + key, layer)
            result[key] = raw[key]
    if "publish" in raw:
        result["publish"] = publications(raw["publish"], field + ".publish", layer)
    if "files" in raw:
        if raw["files"] not in FILES:
            _fail("Files is one of %s." % ", ".join(FILES), field + ".files", layer)
        result["files"] = raw["files"]
    if "folders" in raw:
        folders = raw["folders"]
        if not isinstance(folders, list) or len(folders) > MAX_FOLDERS:
            _fail("Folders is a list of at most %d entries." % MAX_FOLDERS, field + ".folders", layer)
        seen, cleaned = set(), []
        for one in folders:
            if not isinstance(one, dict) or set(one) - {"path", "access"} or "path" not in one:
                _fail("Each folder has a path and an access.", field + ".folders", layer)
            access = one.get("access", "read_only")
            if access not in ACCESS:
                _fail("Folder access is read_write or read_only.", field + ".folders.access", layer)
            path = folder_path(one["path"], home="/home/check", field=field + ".folders.path")
            if path in seen:
                _fail("A folder is listed twice.", field + ".folders", layer)
            seen.add(path)
            cleaned.append({"path": one["path"].strip(), "access": access})
        result["folders"] = cleaned
    if "limits" in raw:
        limits = raw["limits"]
        if not isinstance(limits, dict) or set(limits) - set(LIMITS):
            _fail("Limits are %s." % ", ".join(LIMITS), field + ".limits", layer)
        cleaned = {}
        for key, value in limits.items():
            number = value
            if type(number) not in (int, float) or not math.isfinite(number):
                _fail("A limit is a number.", field + ".limits." + key, layer)
            if key == "cpu_cores":
                if not 0.01 <= number <= 1024:
                    _fail("CPU cores is between 0.01 and 1024.", field + ".limits.cpu_cores", layer)
                cleaned[key] = float(number)
            elif key == "memory_mb":
                if number != int(number) or not 16 <= number <= 16 * 1024 * 1024:
                    _fail("Memory is a whole number of MB, at least 16.", field + ".limits.memory_mb", layer)
                cleaned[key] = int(number)
            else:
                if number != int(number) or not 8 <= number <= 4194304:
                    _fail("Processes is a whole number, at least 8.", field + ".limits.processes", layer)
                cleaned[key] = int(number)
        result["limits"] = cleaned
    if "switched_off" in raw:
        off = raw["switched_off"]
        if not isinstance(off, list) or any(one not in SWITCHES for one in off) or len(set(off)) != len(off):
            _fail("switched_off lists network and/or files.", field + ".switched_off", layer)
        result["switched_off"] = [one for one in SWITCHES if one in off]
    return result


def publications(raw, field="restrictions.publish", layer=""):
    """Check a list of publications - ``{internal_port, host_port, host_ip,
    public}`` - and return it normalised. ``host_port`` is a number or
    ``"auto"``; ``host_ip`` defaults to 127.0.0.1, and any address that is not
    loopback needs ``public: true``: an explicit yes to being reachable from
    other computers."""
    if not isinstance(raw, list) or len(raw) > MAX_PUBLISH:
        _fail("Publish is a list of at most %d ports." % MAX_PUBLISH, field, layer)
    cleaned, taken = [], set()
    for one in raw:
        if not isinstance(one, dict) or set(one) - {"internal_port", "host_port", "host_ip", "public"} \
                or "internal_port" not in one:
            _fail("Each publication has an internal_port, and may have host_port, host_ip and public.", field, layer)
        inner = one["internal_port"]
        if type(inner) is not int or not 1 <= inner <= 65535:
            _fail("internal_port is a port number, 1 to 65535.", field + ".internal_port", layer)
        host = one.get("host_port", "auto")
        if host != "auto" and (type(host) is not int or not 1 <= host <= 65535):
            _fail("host_port is a port number or auto.", field + ".host_port", layer)
        address = one.get("host_ip", "127.0.0.1")
        if not isinstance(address, str) or not address.strip() or len(address) > 64:
            _fail("host_ip is an address such as 127.0.0.1.", field + ".host_ip", layer)
        public = one.get("public", False)
        if not isinstance(public, bool):
            _fail("public is true or false.", field + ".public", layer)
        if address not in LOOPBACK and not public:
            _fail("Publishing on %s makes the port reachable from other computers; say public: true to mean it."
                  % address, field + ".public", layer)
        key = (address, host) if host != "auto" else (address, "auto", inner)
        if key in taken:
            _fail("A host port is published twice.", field + ".host_port", layer)
        taken.add(key)
        entry = {"internal_port": inner, "host_port": host, "host_ip": address}
        if public:
            entry["public"] = True
        cleaned.append(entry)
    return cleaned


def resolve(layers, project_folder="", home=None):
    """What a program actually gets, from the outermost layer inwards.

    ``layers`` is ``[(name, raw), ...]`` - ``("project", ...)`` or
    ``("cycle", ...)`` first, then ``("service", ...)`` or ``("step", ...)``.
    The outer layer sets the defaults; an inner one takes them unless it sets
    its own, dimension by dimension and in either direction - a project can
    keep its services offline while the one service that needs a database
    port says so for itself. Limits are replaced one by one, folders as a list.

    A layer's ``switched_off`` turns that access off for the layer and for
    what inherits from it, without throwing its own setting away: an inner
    layer that sets its own value still decides for itself.

    The result says, for each dimension, which layer decided it, so a window
    can say "Off by the project's default" rather than just "Off".
    """
    home = home or os.path.expanduser("~")
    project = folder_path(project_folder, home, "project_folder") if project_folder else ""
    network, files, folders, limits = "on", "all", [], {}
    outbound, host_loopback, publish = False, False, []
    configured = {"network": "on", "files": "all"}
    switched = []
    provenance = {"network": "", "files": "", "folders": "", "switched_off": {}}
    for name, raw in layers:
        layer = parse(raw, layer=name)
        if "network" in layer:
            network = configured["network"] = layer["network"]
            provenance["network"] = name
        if "outbound" in layer:
            outbound, provenance["outbound"] = layer["outbound"], name
        if "host_loopback" in layer:
            host_loopback, provenance["host_loopback"] = layer["host_loopback"], name
        if "publish" in layer:
            publish, provenance["publish"] = layer["publish"], name
        if "files" in layer:
            files = configured["files"] = layer["files"]
            provenance["files"] = name
        if "folders" in layer:
            listed = []
            for one in layer["folders"]:
                path = folder_path(one["path"], home)
                if project and within(project, path):
                    _fail("%s is inside the project folder; its access is set by Files." % path,
                          "restrictions.folders", name)
                listed.append({"path": path, "access": one["access"]})
            folders, provenance["folders"] = listed, name
        for key, value in layer.get("limits", {}).items():
            limits[key] = value
            provenance["limits." + key] = name
        for kind in layer.get("switched_off", []):
            if kind == "network":
                network = "off"
            else:
                files = "none"
            provenance[kind] = provenance["switched_off"][kind] = name
            if kind not in switched:
                switched.append(kind)
    if files in ("all", "none"):
        folders = []
    folders = sorted(folders, key=lambda one: (len(PurePosixPath(one["path"]).parts), one["path"]))
    isolated = network == "isolated"
    return {"restricted": network != "on" or files != "all" or bool(limits),
            "network": network, "files": files, "folders": folders, "limits": limits,
            # What an isolated network lets through; nothing for any other.
            "outbound": isolated and outbound, "host_loopback": isolated and host_loopback,
            "publish": publish if isolated else [],
            "switched_off": [one for one in SWITCHES if one in switched], "configured": configured,
            "project_folder": project, "provenance": provenance}


def unrestricted(effective):
    return not (effective or {}).get("restricted")


def memory_words(megabytes):
    if megabytes >= 1024 and megabytes % 1024 == 0:
        return "%d GB" % (megabytes // 1024)
    if megabytes >= 1024:
        return "%.1f GB" % (megabytes / 1024.0)
    return "%d MB" % megabytes


def summary(effective):
    """One short line for a button or a tooltip: what this program is held to."""
    if unrestricted(effective):
        return "Network on · No restrictions"
    parts = [{"off": "Network off", "isolated": "Network isolated"}.get(effective["network"], "Network on")]
    if effective["network"] == "isolated":
        if effective.get("publish"):
            parts.append("publishes " + ", ".join(
                "%d" % one["internal_port"] if one["host_port"] == "auto" else
                "%d as %d" % (one["internal_port"], one["host_port"]) for one in effective["publish"]))
        if effective.get("outbound"):
            parts.append("internet")
        if effective.get("host_loopback"):
            parts.append("this computer")
    if effective["files"] != "all":
        parts.append(_FILE_WORDS[effective["files"]])
        if effective["folders"]:
            parts.append("+%d folder%s" % (len(effective["folders"]), "" if len(effective["folders"]) == 1 else "s"))
    limits = effective["limits"]
    if "memory_mb" in limits:
        parts.append(memory_words(limits["memory_mb"]))
    if "cpu_cores" in limits:
        cores = limits["cpu_cores"]
        parts.append("%s CPU" % (int(cores) if cores == int(cores) else cores))
    if "processes" in limits:
        parts.append("%d processes" % limits["processes"])
    return " · ".join(parts)
