# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the PySide6 front-end.

Produces ``qavector-gui`` as a onedir bundle. It ships no core: the
GUI finds the core executable next to it at runtime (cms_gui.core.frozen_core),
which keeps the process boundary the front-end is built around.

Most of this file is the exclusion list. A full PySide6 install is ~650 MB
because it carries WebEngine, QML, 3D and multimedia; this GUI uses QtCore,
QtGui and QtWidgets and nothing else, so all of that is dropped.
"""

import os

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
GUI = os.path.join(ROOT, "gui")

# Verified against every `from PySide6.X import` in gui/cms_gui: the front-end
# touches QtCore, QtGui and QtWidgets only. QtDBus, QtNetwork, QtSvg and QtOpenGL
# are deliberately *not* here - the xcb platform plugin loads them, and dropping
# them leaves an app that builds cleanly and then cannot open a window.
excludes = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtWebView",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2", "PySide6.QtQuickTest",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtSpatialAudio",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtUiTools", "PySide6.QtBluetooth", "PySide6.QtNfc",
    "PySide6.QtPositioning", "PySide6.QtLocation", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtSerialBus", "PySide6.QtRemoteObjects",
    "PySide6.QtScxml", "PySide6.QtStateMachine", "PySide6.QtTextToSpeech",
    "PySide6.QtNetworkAuth", "PySide6.QtHttpServer", "PySide6.QtConcurrent",
    # Not a Qt module, just never used - and it pulls in a whole Tk runtime.
    "tkinter", "pytest",
]

# Anything the GUI opens by path rather than by import. PyInstaller follows
# imports and nothing else, so a file read at runtime is simply absent from the
# bundle - which nearly shipped a build with no splash at all: it looked for the
# artwork, found nothing, and started straight into the main window. The target
# is "cms_gui/assets" so it lands beside the package, where app._splash_file
# looks for it (dirname(__file__) + "/assets") in the bundle exactly as it does
# in a checkout.
datas = [(os.path.join(GUI, "cms_gui", "assets"), os.path.join("cms_gui", "assets"))]

# The Windows executable's own icon. Without this PyInstaller embeds its
# default, which is the Python logo - and that is what Windows then shows in the
# taskbar, in Explorer, and on every shortcut the installer creates. Rendered by
# `python -m cms_gui.icon` before the freeze; absent on Linux, where an ELF
# carries no icon and the .desktop file names the PNG instead.
WINDOWS_ICON = os.path.join(ROOT, "build", "icons", "icon.ico")
if not os.path.isfile(WINDOWS_ICON):
    WINDOWS_ICON = None


analysis = Analysis(
    [os.path.join(SPECPATH, "gui_entry.py")],
    pathex=[GUI],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="qavector-gui",
    debug=False,
    strip=False,
    upx=False,
    # A desktop launcher must not open a terminal behind the window.
    console=False,
    icon=WINDOWS_ICON,
)

COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="gui",
)
