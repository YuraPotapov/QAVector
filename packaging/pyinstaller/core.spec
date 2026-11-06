# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the core launcher.

Produces ``qavector-core`` as a *onedir* bundle. Onedir rather than
onefile on purpose: the GUI spawns this executable for every --describe and every
run, and a onefile build would unpack a 200 MB archive into /tmp each time.

Two things here are not automatic and are the reason this file exists at all:
the core imports almost everything lazily, so PyInstaller's analyser sees none of
it; and playwright needs its Node driver on disk in a specific place.

Built by packaging/build_deb.sh, which is also what writes build/VERSION.
"""

import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

sys.path.insert(0, SPECPATH)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "packaging"))
from tracked_files import tracked_datas  # noqa: E402
from check_licenses import verify_licenses
verify_licenses(os.path.join(ROOT, "licenses"))


def playwright_driver_datas():
    """The Node driver, at the one path playwright will look for it.

    ``playwright._impl._driver.compute_driver_executable`` builds its path from
    ``inspect.getfile(playwright)``, so the driver has to sit inside the bundled
    ``playwright/`` package directory - anywhere else and connect_over_cdp fails
    at runtime with "Executable doesn't exist". The bundled browsers are *not*
    needed: this app only ever attaches to an already-running Chrome over CDP.
    """
    import playwright
    driver = os.path.join(os.path.dirname(playwright.__file__), "driver")
    if not os.path.isdir(driver):
        raise SystemExit("playwright's driver is missing from %s - reinstall "
                         "playwright in the build environment." % driver)
    return [(driver, os.path.join("playwright", "driver"))]


# Every one of these is imported inside a function, so that a plain launch never
# pays for the engine, playwright or pyyaml. The analyser follows module-level
# imports only, which means it sees none of them.
hiddenimports = (
    collect_submodules("engine")
    + collect_submodules("adapters")
    + collect_submodules("domain")
    + collect_submodules("cycle")
    + collect_submodules("integrations")
    + collect_submodules("execution_runtime")
    + collect_submodules("qavector_sdk")
    + collect_submodules("plugin_host")
    + collect_submodules("qavector_agent_claude")
    + ["yaml", "playwright", "playwright.sync_api",
       "cryptography.hazmat.primitives.ciphers",
       "cryptography.hazmat.primitives.serialization",
       "cryptography.hazmat.primitives.asymmetric.rsa"]
)

# Scenarios and cycles file by file, only the ones git tracks: a checkout's
# flows/ and cycles/ also hold whatever its owner recorded or wrote for their
# own projects, and that is not for everyone who installs the package. See
# tracked_files.py.
datas = tracked_datas(ROOT, "flows") + tracked_datas(ROOT, "cycles") + [
    # Executed by the user's framework environment, not the frozen core.
    (os.path.join(ROOT, "cycle", "plugins", "agent_worker.py"), "cycle/plugins"),
    (os.path.join(ROOT, "execution_runtime", "workers", "git_checkout_linux.py"), "execution_runtime/workers"),
    (os.path.join(ROOT, "execution_runtime", "workers", "agent_review_linux.py"), "execution_runtime/workers"),
    (os.path.join(ROOT, "execution_runtime", "workers", "claude_review_linux.py"), "execution_runtime/workers"),
    (os.path.join(ROOT, "extensions"), "extensions"),
    (os.path.join(ROOT, "engine", "hud.js"), "engine"),
    (os.path.join(ROOT, "engine", "recorder.js"), "engine"),
    (os.path.join(ROOT, "users.example.json"), "."),
    (os.path.join(ROOT, "build", "VERSION"), "."),
    (os.path.join(ROOT, "licenses"), "licenses"),
    (os.path.join(ROOT, "execution_runtime", "backends", "linux", "bubblewrap.lock.json"),
     "execution_runtime/backends/linux"),
    (os.path.join(ROOT, "execution_runtime", "backends", "linux", "passt.lock.json"),
     "execution_runtime/backends/linux"),
] + playwright_driver_datas() + collect_data_files("qavector_sdk", includes=["schemas/*.json"]) + collect_data_files("qavector_agent_claude", includes=["schemas/*.json", "manifest.json", "requirements.lock"])

# Source inventory for the bundled backend digest. These are build inputs,
# shipped alongside the frozen modules so exact run pins remain verifiable.
for package in ("qavector_sdk", "qavector_agent_claude"):
    for directory, _dirs, names in os.walk(os.path.join(ROOT, package)):
        for name in names:
            if name.endswith(".py"):
                datas.append((os.path.join(directory, name), os.path.relpath(directory, ROOT)))

# Trusted bootstrap source is mounted in managed worker images.
datas.append((os.path.join(ROOT, "plugin_host", "managed_bootstrap.py"), "plugin_host"))

# Where "Sign in with Atlassian" goes: the sign-in service a team deploys
# (signin-service/). Given at build time and written into the package only -
# never into the public code. Without it the build signs in only with a
# project's own OAuth app (developer mode) or an API token.
signin_service = os.environ.get("QAVECTOR_SIGNIN_SERVICE", "").strip().rstrip("/")
if signin_service:
    if not signin_service.startswith("https://"):
        raise SystemExit("QAVECTOR_SIGNIN_SERVICE must be an https:// address")
    import json
    signin_file = os.path.join(ROOT, "build", "signin-service.json")
    os.makedirs(os.path.dirname(signin_file), exist_ok=True)
    with open(signin_file, "w", encoding="utf-8") as handle:
        json.dump({"url": signin_service}, handle)
    datas.append((signin_file, "."))
# Release tooling supplies a relocatable Python runtime; never substitute the
# frozen executable or system site-packages when it is absent.
plugin_runtime = os.environ.get("QAVECTOR_PLUGIN_RUNTIME", "")
if plugin_runtime:
    descriptor = os.path.join(plugin_runtime, "plugin-python.json")
    if not os.path.isfile(descriptor):
        raise SystemExit("QAVECTOR_PLUGIN_RUNTIME must contain plugin-python.json")
    for directory, _dirs, names in os.walk(plugin_runtime):
        for name in names:
            datas.append((os.path.join(directory, name), os.path.join("runtime", os.path.relpath(directory, plugin_runtime))))

# The Windows executable's own icon. Without this PyInstaller embeds its
# default, which is the Python logo - and that is what Windows then shows in the
# taskbar, in Explorer, and on every shortcut the installer creates. Rendered by
# `python -m cms_gui.icon` before the freeze; absent on Linux, where an ELF
# carries no icon and the .desktop file names the PNG instead.
WINDOWS_ICON = os.path.join(ROOT, "build", "icons", "icon.ico")
if not os.path.isfile(WINDOWS_ICON):
    WINDOWS_ICON = None


analysis = Analysis(
    [os.path.join(ROOT, "session_launcher.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # The core has no user interface and never will: it talks JSONL on stdout.
    excludes=["PySide6", "shiboken6", "tkinter", "pytest", "IPython", "matplotlib",
              # These imports belong to agent_worker.py in the external venv.
              "crewai", "autogen_agentchat", "autogen_core", "autogen_ext", "pydantic"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="qavector-core",
    debug=False,
    strip=False,
    upx=False,
    console=True,
    icon=WINDOWS_ICON,
)

COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="core",
)

# Preserve the reviewed worker image byte-for-byte after freezer processing.
# Its own descriptor, rather than PyInstaller's runtime library set, pins it.
if plugin_runtime:
    import hashlib
    import json
    import shutil
    destination_root = os.path.join(DISTPATH, "core", "_internal", "runtime")
    for directory, _dirs, names in os.walk(plugin_runtime):
        destination = os.path.join(destination_root, os.path.relpath(directory, plugin_runtime))
        os.makedirs(destination, exist_ok=True)
        for name in names:
            target = os.path.join(destination, name)
            if os.path.exists(target):
                os.chmod(target, 0o600)
            shutil.copy2(os.path.join(directory, name), target)
            # Build inputs are private (0400/0500). DEB owns shipped files as
            # root, so preserve executable intent but allow ordinary users to
            # read the public runtime. Never make it writable by those users.
            os.chmod(target, 0o755 if os.stat(target).st_mode & 0o111 else 0o644)
    with open(os.path.join(destination_root, "plugin-python.json"), encoding="utf-8") as source:
        descriptor = json.load(source)
    for entry in descriptor["image"]["files"]:
        relative = entry["source"]
        if os.path.isabs(relative) or ".." in relative.replace("\\", "/").split("/"):
            raise SystemExit("Managed Python bundle must use local relative source paths")
        with open(os.path.join(destination_root, relative), "rb") as source:
            if hashlib.sha256(source.read()).hexdigest() != entry["sha256"]:
                raise SystemExit("Managed Python payload changed during freezing")
