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

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))


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
    + ["yaml", "playwright", "playwright.sync_api",
       "cryptography.hazmat.primitives.ciphers",
       "cryptography.hazmat.primitives.serialization",
       "cryptography.hazmat.primitives.asymmetric.rsa"]
)

datas = [
    (os.path.join(ROOT, "flows"), "flows"),
    (os.path.join(ROOT, "cycles"), "cycles"),
    # Executed by the user's framework environment, not the frozen core.
    (os.path.join(ROOT, "cycle", "plugins", "agent_worker.py"), "cycle/plugins"),
    (os.path.join(ROOT, "extensions"), "extensions"),
    (os.path.join(ROOT, "engine", "hud.js"), "engine"),
    (os.path.join(ROOT, "engine", "recorder.js"), "engine"),
    (os.path.join(ROOT, "users.example.json"), "."),
    (os.path.join(ROOT, "build", "VERSION"), "."),
] + playwright_driver_datas()

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
